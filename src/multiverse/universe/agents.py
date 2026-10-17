"""Agent records: identity and names, the runtime record of the current launch, and reported progress."""
from ..harnesses.types import normalize_session_id
from ..store import existing_lock, lock, now, read, write
from . import sessions


def directory(root, reference):
    matches = []
    for path in (root / "state" / "agents").glob("*/properties.json"):
        data = read(path)
        if reference in (data["id"], data["name"]) or data["id"].startswith(reference):
            matches.append(path.parent)
    if not matches:
        raise ValueError(f"No agent named {reference} here. Run mv2 list to see this universe's agents.")
    if len(matches) != 1:
        detail = ". Use a full UUID from these matches: " + ", ".join(sorted(path.name for path in matches))
        raise ValueError(f"Expected one agent matching {reference!r}; found {len(matches)}{detail}")
    return matches[0]


def check_available_name(root, name):
    for path in (root / "state" / "agents").glob("*/properties.json"):
        if read(path).get("name") == name:
            raise ValueError(f"This universe already has an agent named {name}; "
                             f"pick another name or resume it with mv2 resume {name}")


def runtime(agent_dir):
    path = agent_dir / "runtime.json"
    return read(path) if path.exists() else {}


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
    with lock(agent_dir / ".launch.lock"), sessions.namespace(root), lock(agent_dir / ".properties.lock"):
        if launch_id and runtime(agent_dir).get("launch_id") != launch_id:
            raise ValueError("Outdated launch")
        data, _ = sessions.recover(agent_dir, read(agent_dir / "properties.json"))
        session_id = normalize_session_id(session_id)
        if data.get("native_session_id") not in (None, session_id):
            raise ValueError("This agent is already bound to a different native session")
        sessions.claim(root, agent_dir, data, session_id)
        data.update(native_session_id=session_id, updated_at=now())
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
    agent_dir = directory(root, reference)
    with lock(agent_dir / ".properties.lock"):
        data = read(agent_dir / "properties.json")
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
