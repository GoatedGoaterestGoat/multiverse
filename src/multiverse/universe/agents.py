"""Agent records: identity and names, the runtime record of the current launch, and reported progress."""
from ..harnesses.types import normalize_session_id
from ..store import existing_lock, lock, now, read, write
from . import focus, sessions, records
import fcntl
import re
import stat


def record_paths(root):
    """An existing agent folder remains an identity even if its file vanished."""
    folder = root / 'state' / 'agents'
    try:
        return sorted(path / 'properties.json' for path in folder.iterdir() if path.is_dir())
    except FileNotFoundError:
        return []


def orphaned(directory):
    """Leftovers have no claim without agent records or native startup evidence.

    Do not use exists(): permission and other inspection errors must propagate.
    Main's lost-spawn race could leave only bootstrap/task files in launches.
    """
    for name in ('properties.json', 'runtime.json', 'sessions.jsonl'):
        try:
            (directory / name).stat()
        except FileNotFoundError:
            continue
        return False
    try:
        folder = directory / 'launches'
        if stat.S_ISREG(folder.stat().st_mode):
            return True  # A regular file cannot contain launch or session evidence.
        launches = list(folder.iterdir())
    except FileNotFoundError:
        return True
    for launch in launches:
        if not launch.is_dir():
            return False
        # Unknown evidence stays conservative, including registration and roles.
        for path in launch.iterdir():
            if path.name in ('bootstrap.md', 'task.md'):
                continue
            if path.name == '.connections.lock':
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_size == 0:
                    continue  # Older cleanup scans could leave this empty lock.
            return False
    return True


def properties_missing(directory):
    """Only proven absence permits removal using the folder's recovery identity."""
    try:
        (directory / 'properties.json').stat()
    except FileNotFoundError:
        return True
    return False


class AgentNotFound(ValueError):
    """No saved agent matches a reference; no launch was attempted."""


class AmbiguousAgent(ValueError):
    """A reference names several agents; retain names and full IDs for selection."""


def directory(root, reference, *, allow_damaged=False):
    matches, exact_names, exact_ids, damaged, names = [], [], [], [], {}
    for path in record_paths(root):
        usable = True
        try:
            data = records.properties(path)
        except records.RecordError as error:
            usable = False
            damaged.append(error)
            data = error.data or {}
        identity = data.get('id') if isinstance(data.get('id'), str) else path.parent.name
        name = data.get('name')
        if reference == name:
            exact_names.append(path.parent)
        if reference in (identity, path.parent.name):
            exact_ids.append(path.parent)
        if (reference in (identity, name, path.parent.name)
                or usable and (identity.startswith(reference) or path.parent.name.startswith(reference))):
            matches.append(path.parent)
            names[path.parent] = name if isinstance(name, str) and name else path.parent.name
    if exact_ids:
        # Legacy UUID-shaped names must not hide an agent's recovery address.
        matches = exact_ids
    elif exact_names:
        matches = exact_names
    if not matches:
        detail = (" Unusable records: " + "; ".join(f"{error.file}: {error.reason}" for error in damaged)) if damaged else ""
        raise AgentNotFound(f"No agent named {reference} here. Run mv2 list to see this universe's agents." + detail)
    if len(matches) != 1:
        detail = ", ".join(f"{names[path]} ({path.name})" for path in sorted(matches))
        raise AmbiguousAgent(f"Expected one agent matching {reference!r}; found {len(matches)}: {detail}. Use a full UUID")
    if not allow_damaged:
        # Recheck the selected record, never a peer's runtime. Actions cannot
        # treat unreadable identity or launch facts as an empty/stopped agent.
        records.properties(matches[0] / 'properties.json')
        records.runtime(matches[0] / 'runtime.json')
    return matches[0]


def name_matches(path, name):
    """A peer's name, or conservative reservation evidence from a damaged file."""
    try:
        return records.properties(path).get('name') == name
    except records.RecordError as error:
        owner = f'agent {path.parent.name}'
        try:
            if orphaned(path.parent):
                return False  # No current or historical record can reserve a name.
            mentioned = (error.data or {}).get('name') == name or sessions.damaged_mentions(path.parent, name)
        except (OSError, ValueError) as unreadable:
            remedy = ('restore or remove the record' if isinstance(unreadable, FileNotFoundError)
                      else 'fix its permissions or repair the record')
            raise records.RecordError(path, f'Cannot check {owner}: cannot read safely ({unreadable}); '
                                      f'{remedy} before retrying') from error
        if mentioned:
            raise records.RecordError(path.parent, f'Cannot check {owner}: a surviving damaged record contains '
                                      f'{name!r}, so mv2 cannot prove the name is free. Repair the records, or use '
                                      f'mv2 remove {path.parent.name} to move this leftover agent to the trash before retrying') from error
        return False


def check_available_name(root, name, *, locked=False, exclude=None):
    if re.fullmatch(r'(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})', name, re.I):
        raise ValueError("Agent names can't look like an agent ID; choose a name instead of a UUID")
    # The initial check is read-only. Publication repeats it under the
    # namespace/name locks before the atomic rename reserves the identity.
    for path in record_paths(root):
        if path.parent == exclude:
            continue
        if name_matches(path, name):
            raise ValueError(f"This universe already has an agent named {name}; "
                             f"pick another name or resume it with mv2 resume {name}")


def runtime(agent_dir):
    path = agent_dir / "runtime.json"
    return records.runtime(path)


def preparing(facts):
    """Only the minimal record published before any terminal or child can exist."""
    try:
        canonical = normalize_session_id(facts.get('launch_id')) == facts.get('launch_id')
    except ValueError:
        canonical = False
    return (canonical and facts.get('preparing') is True and facts.get('state') == 'starting'
            and set(facts) <= {'launch_id', 'state', 'status', 'started_at', 'preparing'})


def _child_evidence(agent_dir, facts):
    launch = agent_dir / 'launches' / facts['launch_id']
    return any((launch / name).exists() for name in ('request.json', 'registration.json', 'cleanup.json'))


def interrupted_preparation(facts, agent_dir):
    """Caller holds the launch lock: the old preparer has ended without a child."""
    if not preparing(facts):
        return facts
    if _child_evidence(agent_dir, facts):
        return {**facts, 'preparing': False}  # This is not proven to precede child startup.
    reason = 'Launch preparation ended before native startup; fix the cause and resume or remove this agent'
    return {**facts, 'preparing': False, 'state': 'failed', 'error': reason, 'reason': reason}


def launch_in_progress(agent_dir, facts):
    """Publication can still be in progress after its terminal record appears."""
    if facts.get('state') != 'starting':
        return False
    try:
        identifier = normalize_session_id(facts.get('launch_id'))
    except ValueError:
        return False
    if identifier != facts.get('launch_id') or not (agent_dir / 'launches' / identifier).is_dir():
        return False
    try:
        with (agent_dir / '.launch.lock').open('r+') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                current = runtime(agent_dir)
                return current.get('launch_id') == identifier and current.get('state') == 'starting'
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def preparation_snapshot(agent_dir, facts):
    """Check only a rare minimal stage, without creating files or querying PIDs."""
    if not preparing(facts):
        active = launch_in_progress(agent_dir, facts)
        return {**facts, 'preparing': active} if active or facts.get('preparing') is True else facts
    if _child_evidence(agent_dir, facts):
        raise records.RecordError(agent_dir / 'runtime.json',
            'preparation record has native startup evidence; repair the runtime record before acting on this agent')
    try:
        with (agent_dir / '.launch.lock').open('r+') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return facts  # The publishing parent still holds its launch lock.
            try:
                # The parent might have finished between our read and this lock.
                return interrupted_preparation(runtime(agent_dir), agent_dir)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        return interrupted_preparation(facts, agent_dir)
    except OSError:
        return {**facts, 'preparing': False}  # Unknown ownership remains unknown.


def update_runtime(agent_dir, launch_id, **changes):
    return mutate_runtime(agent_dir, launch_id, lambda data: changes, create_parent=True)


def mutate_runtime(agent_dir, launch_id, changes_for, *, create_parent=False):
    """Reduce current facts under the runtime lock; None leaves the record alone.

    Reducers are core bookkeeping only: no native calls or plugin callbacks.
    Keep launch guards, refusal precedence and status events shared with writes.
    """
    with lock(agent_dir / ".runtime.lock", create_parent=create_parent):
        data = runtime(agent_dir)
        if data.get("launch_id") != launch_id:
            raise ValueError("Outdated launch; refusing to update current runtime")
        changes = changes_for(data)
        if changes is None:
            return None
        before = data.get("status")
        primary = data.get('native_refusal')
        data.update(changes, observed_at=now())
        if primary or data.get('native_refusal'):
            data['native_refusal'] = primary or data['native_refusal']
            data['reason'] = data['error'] = data['native_refusal']['message']
            data['startup_delivery'] = 'failed'
            data['delivery_error'] = data['reason']
            data['status'] = 'unknown'
        write(agent_dir / "runtime.json", data)
        # A turn ends when the harness goes idle from working or waiting on the person. Under the lock,
        # so a focus --turn, which reads the status under it too, is ordered with this turn end.
        if data.get('status') == 'idle' and before in ('working', 'waiting'):
            focus.turn_ended(agent_dir)
    if data.get('status') != before:
        # After the lock: recording an event never holds up the runtime record.
        _event(agent_dir, "agent.status", launch_id, {"from": before, "to": data.get('status')})
    return data


def _event(agent_dir, kind, launch=None, data=None, properties=None):
    """Record an event for plugins that subscribe to it; best effort, never raises."""
    try:
        from ..plugins import events
        events.for_agent(agent_dir, kind, launch, data, properties)
    except Exception:
        pass


def bind_session(root, reference, session_id, launch_id=None):
    """Record a harness-reported session without guessing from global history."""
    agent_dir = directory(root, reference)
    with lock(agent_dir / ".launch.lock"):
        return _bind_session_locked(root, agent_dir, session_id, launch_id)


def _bind_session_locked(root, agent_dir, session_id, launch_id):
    """Caller holds the existing launch lock; claims retain the universe lock."""
    with sessions.namespace(root), lock(agent_dir / ".properties.lock"):
        if launch_id and runtime(agent_dir).get("launch_id") != launch_id:
            raise ValueError("Outdated launch")
        data, _ = sessions.recover(agent_dir, read(agent_dir / "properties.json"))
        session_id = normalize_session_id(session_id)
        sessions.require_unretired(agent_dir, session_id)
        if data.get("native_session_id") not in (None, session_id):
            raise ValueError("This agent is already bound to a different native session")
        sessions.claim(root, agent_dir, data, session_id, operation="bind")
        sessions.record_initial_binding(agent_dir, data, session_id, launch_id)
        data.update(native_session_id=session_id, updated_at=now())
        if data.get('native_fresh_pending'):
            data['native_fresh_pending'] = False
        if data.get('agent_identity'):
            data['agent_identity'] = {**data['agent_identity'], 'native_id': session_id}
        write(agent_dir / "properties.json", data)
    return data


def session_properties(root, agent_dir, *, launch_locked=False):
    """Finish a durable session transition before using the current native ID."""
    if not launch_locked:
        with lock(agent_dir / '.launch.lock'):
            return session_properties(root, agent_dir, launch_locked=True)
    with sessions.namespace(root), lock(agent_dir / '.properties.lock'):
        data, repaired = sessions.recover(agent_dir, read(agent_dir / 'properties.json'))
    if repaired:
        _session_event(agent_dir, repaired)
    return data


def _session_event(directory, row):
    _event(directory, 'agent.session', row['launch'], {key: row[key] for key in ('from', 'to', 'reason')})


def _rebind_session(root, agent_dir, previous, session_id, reason, launch_id, *, home=None):
    """Internal form: caller already holds this agent's launch lock."""
    try:
        data, row = sessions.transition(root, agent_dir, previous, session_id, reason, launch_id, home)
    except sessions.SessionConflict as error:
        if runtime(agent_dir).get('binding_conflict') != str(error):
            update_runtime(agent_dir, launch_id, binding_conflict=str(error))
        raise
    if row:
        _session_event(agent_dir, row)
    if runtime(agent_dir).get('binding_conflict'):
        update_runtime(agent_dir, launch_id, binding_conflict=None)
    return data


def rebind_session(root, reference, previous, session_id, reason, launch_id, *, home=None):
    """CAS a native session for an exact launch; retain its earlier generations."""
    agent_dir = directory(root, reference)
    with lock(agent_dir / '.launch.lock'):
        return _rebind_session(root, agent_dir, previous, session_id, reason, launch_id, home=home)


def report(root, reference, phase=None, summary=None):
    agent_dir = directory(root, reference, allow_damaged=True)
    with lock(agent_dir / ".properties.lock"):
        data = records.properties(agent_dir / "properties.json", harness_optional=True)
        if phase is not None:
            data["phase"] = phase
        if summary is not None:
            data["summary"] = summary
        data["updated_at"] = now()
        write(agent_dir / "properties.json", data)
    _reported(agent_dir, data)
    return data


def _reported(agent_dir, data):
    """agent.reported, with the current launch when the runtime record can be read; never fails a report."""
    try:
        launch = runtime(agent_dir).get("launch_id")
    except Exception:
        launch = None
    _event(agent_dir, "agent.reported", launch, {"phase": data.get("phase"), "summary": data.get("summary")}, data)


def record_recovery(directory, launch_id, state, reason, **extra):
    with existing_lock(directory / '.runtime.lock'):
        facts = runtime(directory)
        if facts.get('launch_id') != launch_id:
            return
        previous = facts.get('auto_recovery') or {}
        if previous.get('state') == 'cancelled':
            return
        facts.update(auto_recovery={**previous, **extra, 'state': state, 'reason': reason, 'updated_at': now()},
                     observed_at=now())
        relay = facts.get('native_relay') or {}
        if relay.get('state') == 'lost':
            facts['native_relay'] = {**relay, 'reason': 'Native relay lost. ' + reason +
                                     ' Kill then resume manually if needed; no native messages were replayed.'}
        write(directory / 'runtime.json', facts)
