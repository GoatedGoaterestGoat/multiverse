"""Agent records: identity and names, the runtime record of the current launch, and reported progress."""
from ..harnesses.types import normalize_session_id
from ..store import exists_replaced, retry_missing, existing_lock, lock, now, open_regular, open_replaced, read, uid, write
from . import focus, sessions, records
from contextvars import ContextVar
from dataclasses import dataclass
import fcntl
import json
from pathlib import Path
import logging
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
            retry_missing((directory / name).stat)
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
        retry_missing((directory / 'properties.json').stat)
    except FileNotFoundError:
        return True
    return False


class AgentNotFound(ValueError):
    """No saved agent matches a reference; no launch was attempted."""

    def __init__(self, message, *, hint=''):
        self.related_hint = hint
        super().__init__(message)


class AmbiguousAgent(ValueError):
    """A reference names several agents; retain names and full IDs for selection."""


def directory(root, reference, *, allow_damaged=False, universe_option='-u'):
    matches, exact_names, exact_ids, damaged, names = [], [], [], [], {}
    addresses, prefix_addresses = {}, set()
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
        names[path.parent] = name if isinstance(name, str) and name else path.parent.name
        for address in {identity, path.parent.name}:
            addresses.setdefault(address, []).append(path.parent)
            if usable and address.startswith(reference):
                prefix_addresses.add(address)
        if reference == name:
            exact_names.append(path.parent)
        if reference in (identity, path.parent.name):
            exact_ids.append(path.parent)
        if (reference in (identity, name, path.parent.name)
                or usable and (identity.startswith(reference) or path.parent.name.startswith(reference))):
            matches.append(path.parent)
    if exact_ids:
        # Legacy UUID-shaped names must not hide an agent's recovery address.
        matches = exact_ids
    elif exact_names:
        matches = exact_names
    else:
        # A prefix may not hide a collision in the full address it matches.
        # Distinct damaged addresses still need their exact recovery address.
        matches = sorted({path for address in prefix_addresses for path in addresses[address]})
    if not matches:
        from ..display import clean
        from .agent_hints import hint
        related = hint(root, reference, universe_option)
        detail = (" Unusable records: " + "; ".join(f"{error.file}: {error.reason}" for error in damaged)) if damaged else ""
        advice = related or "Run mv2 list to see this universe's agents."
        label = clean(reference) if related else reference
        raise AgentNotFound(f"No agent named {label} here. {advice}" + detail, hint=related)
    if len(matches) != 1:
        detail = ", ".join(f"{names[path]} ({path.name})" for path in sorted(matches))
        advice = ("This exact recovery address is ambiguous; inspect the listed record folders"
                  if exact_ids else "Use a full UUID")
        raise AmbiguousAgent(f"Expected one agent matching {reference!r}; found {len(matches)}: {detail}. {advice}")
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
            remedy = (f'Repair the records, or use mv2 remove {path.parent.name} to move this leftover folder '
                      'to the trash before retrying' if properties_missing(path.parent) else
                      'Repair this damaged properties file before retrying')
            raise records.RecordError(path, f'Cannot check {owner}: a surviving damaged record contains '
                                      f'{name!r}, so mv2 cannot prove the name is free. {remedy}') from error
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


def runtime(agent_dir, *, required=False):
    path = agent_dir / "runtime.json"
    return records.runtime(path, required=required)


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
    return any(exists_replaced(launch / name) for name in ('request.json', 'registration.json', 'cleanup.json'))


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


def update_runtime(agent_dir, launch_id, *, status_observed=False, **changes):
    return mutate_runtime(agent_dir, launch_id, lambda data: changes, create_parent=True,
                          status_observed=status_observed)


def _status_spill_path(agent_dir, index):
    return agent_dir / f'journal-status-{index}.json'


def _has_status_intents(data):
    spill = data.get('journal_status_spill')
    return bool(data.get('journal_status') or spill and spill['head'] < spill['tail'])


@dataclass
class _NativeFence:
    directory: Path
    key: str | None = None
    owner: object = None
    admitted: tuple | None = None
    preserve: object = None
    launch: str | None = None
    prepared: dict | None = None
    binding: dict | None = None
    scope: dict | None = None
    at: str | None = None
    lineage_tail: tuple[str, ...] | None = None
    fresh_companions: set[bool] | None = None


_status_fence = ContextVar('ordered_status_fence', default=None)
_SESSION_BOUNDARY = '_session.boundary'
_SESSION_CHECK = '_session.check'
_BINDING_UNREAD = object()


def _intent(facts, kind, data):
    return {'key': uid(), 'kind': kind, 'launch': facts['launch_id'], 'at': now(),
            'data': data, 'required': True}


def _append_status_intent(agent_dir, data, intent, *, before=None, spill_batch=False):
    """Spill older intents before updating their pointer. Keep a failed spill inline."""
    from ..plugins import agent_files

    older = list(data.get('journal_status', ()))
    if before is not None:
        # Only the callback fencing this exact status may add a predecessor.
        reservation = older.pop()
        if reservation['key'] != before:
            raise ValueError('The ordered status reservation has changed')
        older.append(intent)
        intent = reservation
    elif spill_batch:
        older.append(intent)
        intent = None
    inline = [intent] if intent is not None else []
    if older:
        spill = dict(data.get('journal_status_spill', {'head': 0, 'tail': 0}))
        try:
            for item in older:
                agent_files.write_json(_status_spill_path(agent_dir, spill['tail']), item)
                spill['tail'] += 1
        except OSError:
            # The old pointer still owns every committed segment. The prepared
            # writes may be replaced on retry, and this update can still commit.
            reserve = {**data, 'journal_status': [*older, *inline]}
            if len(json.dumps(reserve, indent=2, ensure_ascii=False).encode()) + 1 > records.MAX_BYTES:
                raise
            data.update(reserve)
            return
        data['journal_status_spill'] = spill
    if inline:
        data['journal_status'] = inline
    else:
        data.pop('journal_status', None)


def _status_fence_path(agent_dir, key, *, backup=False):
    if not re.fullmatch(r'[0-9a-f-]{36}', key):
        raise ValueError('Invalid ordered fence key')
    prefix = 'journal-relay' if backup else 'journal-fence'
    return agent_dir / f'{prefix}-{key}.json'


def _read_fence(path, key, launch, *, fresh=False):
    opener = open_regular if fresh else open_replaced
    with opener(path) as stream:
        saved = json.load(stream)
    if (saved['key'], saved['launch']) != (key, launch) or not isinstance(saved['batches'], list):
        raise ValueError('Invalid ordered fence receipt')
    return saved['batches']


def _stage_status_intents(agent_dir, data, *, blocking=True, maximum=None, before=None):
    from ..runtime import prompt_events

    staged = []
    count = 0
    while _has_status_intents(data) and (maximum is None or count < maximum):
        spill = data.get('journal_status_spill')
        segment = None
        if spill and spill['head'] < spill['tail']:
            segment = _status_spill_path(agent_dir, spill['head'])
            try:
                with open_replaced(segment) as stream:
                    intent = json.load(stream)
            except (OSError, ValueError):
                break  # A failed read leaves the saved head in place.
        else:
            intent = data['journal_status'][0]
        if intent['key'] == before:
            break
        predecessors = ()
        fence_path = backup = None
        if intent.get('fenced') is True:
            try:
                key, launch = intent['key'], intent['launch']
                fence_path = _status_fence_path(agent_dir, key)
                backup = _status_fence_path(agent_dir, key, backup=True)
                try:
                    predecessors = _read_fence(fence_path, key, launch)
                except FileNotFoundError:
                    # A matching live launch still owns the unfilled position.
                    if data['launch_id'] == launch and data.get('state') not in ('stopped', 'failed', 'dormant'):
                        break
                    try:
                        predecessors = _read_fence(backup, key, launch)
                    except FileNotFoundError:
                        pass  # The old callback stopped before any input was durably captured.
            except (OSError, ValueError, TypeError, KeyError):
                break
        kind = intent.get('kind', 'agent.status')
        session_row = None
        if kind == _SESSION_CHECK:
            binding = _read_ordered_binding(agent_dir)
            if binding is None:
                break
            marker = _missing_session_event(agent_dir, data, binding)
            if marker is not None:
                session_row = _session_lineage_row(agent_dir, marker)
                if session_row is None:
                    break
        elif kind == _SESSION_BOUNDARY:
            session_row = _session_lineage_row(agent_dir, intent['key'])
            if session_row is None:
                break
        if session_row is not None:
            admission = prompt_events.stage_ordered(
                agent_dir, 'agent.session', session_row['launch'],
                {name: session_row[name] for name in ('from', 'to', 'reason')},
                at=session_row['at'], key=session_row['id'], required=True, current_launch=data['launch_id'],
                close_scope={'native_id': session_row['from'], 'cause': 'switched'}, blocking=blocking)
        elif kind == _SESSION_CHECK:
            admission = True
        else:
            admission = prompt_events.stage_ordered(
                agent_dir, kind, intent['launch'], intent['data'], at=intent['at'],
                terminal=intent.get('terminal', False), key=intent['key'], required=intent['required'],
                current_launch=data['launch_id'], close_scope=intent.get('close_scope'), blocking=blocking,
                **({'predecessors': predecessors} if predecessors else {}))
        if admission is False:
            break
        remaining = runtime(agent_dir, required=True)
        if (remaining['launch_id'], remaining.get('journal_status'), remaining.get('journal_status_spill')) != (
                data['launch_id'], data.get('journal_status'), data.get('journal_status_spill')):
            raise ValueError('Runtime changed while recovering its ordered event')
        if segment is None:
            remaining['journal_status'] = data['journal_status'][1:]
            if not remaining['journal_status']:
                del remaining['journal_status']
        else:
            remaining['journal_status_spill'] = {**spill, 'head': spill['head'] + 1}
        if kind == _SESSION_CHECK:
            if session_row is not None:
                remaining['ordered_session_transition'] = session_row['id']
            if remaining.get('ordered_binding_check') == intent['key']:
                remaining.pop('ordered_binding_check')
        if remaining.get('ordered_binding_fence') == intent['key']:
            remaining.pop('ordered_binding_fence')
        try:
            write(agent_dir / 'runtime.json', remaining)
        except OSError:
            break  # The intent key lets recovery acknowledge the same append.
        data.clear()
        data.update(remaining)
        for saved in (segment, fence_path, backup):
            if saved is not None:
                try:
                    saved.unlink(missing_ok=True)
                except OSError:
                    pass  # Its successor pointer is already durable.
        count += 1
        if isinstance(admission, dict):
            staged.append(admission)
    return staged


def _stage_after_status_locked(agent_dir, kind, launch, data, *, at=None, close_scope=None, required=False,
                               session_key=None):
    from ..runtime import prompt_events

    facts = runtime(agent_dir)
    if session_key is not None and facts.get('ordered_session_transition') == session_key:
        return False
    _stage_status_intents(agent_dir, facts, blocking=False, maximum=8)
    if not _has_status_intents(facts):
        staged = prompt_events.stage_ordered(agent_dir, kind, launch, data, at=at, close_scope=close_scope,
                                              current_launch=facts.get('launch_id'), blocking=False,
                                              required=required, key=session_key)
        if staged is not False:
            if session_key is not None:
                facts['ordered_session_transition'] = session_key
                write(agent_dir / 'runtime.json', facts)
            return staged
    if facts:
        if session_key is not None:
            facts['ordered_session_transition'] = session_key
        _append_status_intent(agent_dir, facts, {
            'key': session_key or uid(), 'kind': kind, 'launch': launch, 'at': at or now(), 'data': data,
            'required': True, 'close_scope': close_scope,
        })
        write(agent_dir / 'runtime.json', facts)
    return False


def stage_after_status(agent_dir, kind, launch, data, *, at=None, close_scope=None):
    """A later ordered producer cannot pass a committed runtime intent."""
    agent_dir = Path(agent_dir)
    try:
        with lock(agent_dir / '.runtime.lock', create_parent=False):
            facts = _recover_session_event_locked(agent_dir, runtime(agent_dir))
            if _reserve_missing_session(agent_dir, facts):
                write(agent_dir / 'runtime.json', facts)
            return _stage_after_status_locked(agent_dir, kind, launch, data, at=at, close_scope=close_scope)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def runtime_status_stamp(agent_dir):
    """A cheap recovery hint for runtime and the published conversation pointer."""
    def stamp(path):
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    runtime_stamp = stamp(agent_dir / 'runtime.json')
    if runtime_stamp is not None:
        return runtime_stamp, stamp(agent_dir / 'properties.json')


def _read_ordered_binding(agent_dir):
    for _ in range(3):
        try:
            return records.properties(agent_dir / 'properties.json')
        except (OSError, ValueError):
            continue
    return None


def _missing_session_event(agent_dir, facts, binding=_BINDING_UNREAD):
    if binding is _BINDING_UNREAD:
        binding = _read_ordered_binding(agent_dir)
    marker = binding.get('ordered_session_transition') if binding is not None else None
    return marker if marker != facts.get('ordered_session_transition') else None


def _read_session_lineage(agent_dir):
    try:
        return sessions.journal(agent_dir)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _session_lineage_row(agent_dir, marker):
    try:
        return next((item for item in reversed(sessions.journal(agent_dir)) if item['id'] == marker), None)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _recover_session_event_locked(agent_dir, facts, binding=_BINDING_UNREAD):
    marker = _missing_session_event(agent_dir, facts, binding)
    if marker is not None and (row := _session_lineage_row(agent_dir, marker)) is not None:
        _stage_session_locked(agent_dir, row)
        return runtime(agent_dir, required=True)
    return facts


def _reserve_missing_session(agent_dir, facts, binding=_BINDING_UNREAD, *, before=None,
                             lineage=_BINDING_UNREAD):
    """Retain the prior lineage position even when its record cannot be read yet."""
    if binding is _BINDING_UNREAD:
        binding = _read_ordered_binding(agent_dir)
    if facts.get('ordered_binding_check'):
        return False  # The first unresolved check is already ahead of newer work.
    if binding is None:
        # The lineage is append-before-pointer. Its last committed ordinary
        # transition fixes the cutoff even if properties cannot be read now.
        if lineage is _BINDING_UNREAD:
            lineage = _read_session_lineage(agent_dir)
        if lineage is None:
            intent = _intent(facts, _SESSION_CHECK, {})
            _append_status_intent(agent_dir, facts, intent, before=before)
            facts['ordered_binding_check'] = intent['key']
            return True
        marker = next((row['id'] for row in reversed(lineage) if row['schema'] == 1), None)
        if marker == facts.get('ordered_session_transition'):
            marker = None
    else:
        marker = _missing_session_event(agent_dir, facts, binding)
    if marker is None:
        return False
    intent = {**_intent(facts, _SESSION_BOUNDARY, {}), 'key': marker}
    _append_status_intent(agent_dir, facts, intent, before=before)
    facts['ordered_session_transition'] = marker
    return True


def recover_status_events(agent_dir, *, maximum=None):
    """Replay committed intents when no live report owns runtime."""
    try:
        facts = runtime(agent_dir, required=True)
        if not _has_status_intents(facts) and _missing_session_event(agent_dir, facts) is None:
            return True
        with lock(agent_dir / '.runtime.lock', create_parent=False, blocking=False):
            facts = _recover_session_event_locked(agent_dir, runtime(agent_dir, required=True))
            _stage_status_intents(agent_dir, facts, blocking=False, maximum=maximum)
            return not _has_status_intents(facts) and _missing_session_event(agent_dir, facts) is None
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def _admit_native_batch(agent_dir, facts, kind, data, *, before=None, blocking=True):
    """Use the outbox only when all earlier intents have been admitted."""
    from ..runtime import prompt_events

    older = _has_status_intents(facts)
    if before is not None:
        spill = facts.get('journal_status_spill')
        older = bool(spill and spill['head'] < spill['tail'] or len(facts.get('journal_status', ())) > 1)
    intent = _intent(facts, kind, data)
    if not older and prompt_events.stage_ordered(
            agent_dir, kind, intent['launch'], data, at=intent['at'], key=intent['key'],
            current_launch=intent['launch'], required=True, blocking=blocking) is not False:
        return False
    _append_status_intent(agent_dir, facts, intent, before=before, spill_batch=True)
    return True


def stage_native_batch(agent_dir, launch, native_id, transitions, *, generation=None, root=None, task_launch=None):
    """Route relay metadata through the runtime order, or ask the relay to retry."""
    from ..plugins import events, projection_wake
    from ..runtime import prompt_events

    agent_dir = Path(agent_dir)

    def stage(*, before=None):
        facts = runtime(agent_dir, required=True)
        if (facts.get('launch_id') != launch
                or generation is not None and facts.get('session_generation', 0) != generation):
            return
        inline = facts.get('journal_status')
        if before is not None and (not inline or inline[-1]['key'] != before):
            return False
        binding = _read_ordered_binding(agent_dir)
        if binding is None:
            return False
        if before is None:
            facts = _recover_session_event_locked(agent_dir, facts, binding)
            if _missing_session_event(agent_dir, facts, binding) is not None:
                return False
        prepared = prompt_events.prepare_native_batch(agent_dir, facts, native_id, transitions, root=root,
                                                     task_launch=task_launch, relay=generation is not None,
                                                     properties=binding)
        if prepared is None:
            return
        _stage_status_intents(agent_dir, facts, blocking=False, maximum=8, before=before)
        if _admit_native_batch(agent_dir, facts, *prepared, before=before, blocking=False):
            write(agent_dir / 'runtime.json', facts)
        if (universe := events.root_of(agent_dir)) is not None:
            projection_wake.wake(universe, tree=True)

    try:
        fence = _status_fence.get()
        if fence is not None and fence.directory == agent_dir:
            return stage(before=fence.key)
        # The watcher can hold runtime while it fences the relay queue. The
        # background relay must return control and keep its batch if that happens.
        with lock(agent_dir / '.runtime.lock', create_parent=False, blocking=generation is None):
            return stage()
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        return False


def retain_fenced_batch(native_id, generation, transitions, *, turns=False):
    """Retry a refused predecessor using the watcher reservation's own durable lane."""
    from ..plugins import events

    fence = _status_fence.get()
    if fence is None or fence.key is None:
        return False
    directory = fence.directory
    try:
        root = events.root_of(directory) if turns else None
        if turns and root is None:
            return False
        facts = runtime(directory, required=True)
        return stage_native_batch(directory, facts['launch_id'], native_id, transitions,
                                  generation=generation, root=root) is not False
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        return False


def capture_fenced_batches(owner, admitted, preserve):
    """Bind the dispatcher's initial snapshot to this exact runtime reservation."""
    fence = _status_fence.get()
    if fence is None:
        return admitted
    if fence.admitted is None:
        fence.owner, fence.admitted, fence.preserve = owner, admitted, preserve
        if fence.key is not None and any(admitted):
            _prepare_fence(fence)
    return fence.admitted if fence.owner is owner else ((), ())


def _prepare_fenced_batches(directory, facts, prompts, turns, *, binding=_BINDING_UNREAD):
    from ..plugins import events
    from ..runtime import prompt_events

    if binding is _BINDING_UNREAD:
        binding = _read_ordered_binding(directory)
    if binding is None:
        raise ValueError('Unavailable binding for held relay events')
    root = events.root_of(directory) if turns else None
    if turns and root is None:
        raise ValueError('Missing universe of an admitted turn batch')
    for batches, scope in ((prompts, None), (turns, root)):
        for item in batches:
            _, native_id, generation, edges = item
            if facts.get('session_generation', 0) != generation:
                continue
            prepared = prompt_events.prepare_native_batch(directory, facts, native_id, edges, root=scope,
                                                         relay=True, properties=binding)
            if prepared is not None:
                yield id(item), prepared


def _recover_fence_binding(fence):
    facts = runtime(fence.directory, required=True)
    if facts['launch_id'] != fence.launch:
        return None
    binding = _read_ordered_binding(fence.directory)
    if binding is None:
        return None
    generation = fence.scope.get('session_generation', 0)
    origins = {item[1] for group in fence.admitted for item in group if item[2] == generation}
    if binding.get('native_session_id') in origins and facts.get('session_generation', 0) == generation:
        return binding
    # A direct rebind may change the conversation without changing the
    # launch or the relay generation. The first departure following the
    # held status proves the earlier storage, while this launch's workspace
    # remains the same.
    if fence.lineage_tail is None:
        return None
    lineage = iter(sessions.journal(fence.directory))
    if fence.lineage_tail:
        anchor = fence.lineage_tail[0]
        if not any(row['id'] == anchor for row in lineage):
            return None
    transition = next(lineage, None)
    if (transition is not None and transition['launch'] == fence.launch and transition['from'] in origins
            and transition['harness'] == binding.get('harness')):
        return {**binding, 'native_session_id': transition['from'], 'native_session_home': transition['home']}
    return None


def _prepare_fence(fence):
    try:
        if fence.binding is None:
            fence.binding = _recover_fence_binding(fence)
            if fence.binding is None:
                return
        fence.prepared = dict(_prepare_fenced_batches(fence.directory, fence.scope, *fence.admitted,
                                                     binding=fence.binding))
        _write_fence_file(fence, list(fence.prepared.values()), backup=True)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass  # The held intent still has its exact relay prefix for another attempt.


def _write_fence_file(fence, batches, *, backup=False):
    from ..plugins import agent_files

    try:
        path = _status_fence_path(fence.directory, fence.key, backup=backup)
        fresh = fence.fresh_companions is not None and backup in fence.fresh_companions
        if fresh:
            fence.fresh_companions.remove(backup)
        try:
            _read_fence(path, fence.key, fence.launch, fresh=fresh)
            return True  # A committed companion remains authoritative through retries.
        except FileNotFoundError:
            pass
        agent_files.write_json(path, {'key': fence.key, 'launch': fence.launch, 'batches': batches})
        return True
    except (OSError, ValueError):
        return False


def _release_fence(fence):
    if not fence.admitted or not any(fence.admitted):
        return _write_fence_file(fence, [])
    if fence.prepared is None:
        _prepare_fence(fence)
        if fence.prepared is None:
            return False

    def save(prompts, turns):
        batches = [fence.prepared[id(item)] for item in (*prompts, *turns) if id(item) in fence.prepared]
        return _write_fence_file(fence, batches)

    return fence.preserve(fence.admitted, save)


def resume_native_fence(fence):
    """Retry a held status in its own place, even if later statuses arrived."""
    def release():
        if _release_fence(fence) is False:
            return False
        facts = runtime(fence.directory, required=True)
        _stage_status_intents(fence.directory, facts, blocking=False, maximum=8)
        return True

    try:
        current = _status_fence.get()
        if current is not None and current.directory == fence.directory:
            return release()
        with lock(fence.directory / '.runtime.lock', create_parent=False, blocking=False):
            return release()
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def reserve_fenced_batches(prompts, turns):
    """Prepare the relay prefix before its reserved status or launch boundary."""
    return _keep_fence_batches(prompts, turns)


def _keep_fence_batches(prompts, turns):
    """Persist admitted metadata in the runtime queue before releasing the boundary."""
    fence = _status_fence.get()
    if fence is None:
        return False
    directory, reservation = fence.directory, fence.key
    try:
        facts = runtime(directory, required=True)
        inline = facts.get('journal_status')
        if reservation is not None and (not inline or inline[-1]['key'] != reservation):
            return False
        binding = _read_ordered_binding(directory)
        if binding is None:
            return False
        saved = _reserve_missing_session(directory, facts, binding, before=reservation)
        for _, prepared in _prepare_fenced_batches(directory, facts, prompts, turns, binding=binding):
            intent = _intent(facts, *prepared)
            _append_status_intent(directory, facts, intent, before=reservation, spill_batch=True)
            saved = True
        if saved:
            write(directory / 'runtime.json', facts)
        return True
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        return False


def _run_native_fence(fence, stage, *args):
    token = _status_fence.set(fence)
    released = None
    try:
        staged = stage(*args)
        if staged is False:
            staged = stage(*args)
        if fence.key is not None:
            released = _release_fence(fence)
            if released is False and fence.owner is not None:
                fence.owner.defer_native_fence(fence)
            return released
        if staged is False and fence.preserve is not None:
            # Both ordinary attempts left this exact prefix in the relay.
            # Retain its typed inputs as ordered intents before releasing the
            # runtime lock, without assuming the terminal scope stays live.
            staged = fence.preserve(fence.admitted, _keep_fence_batches)
        return staged
    finally:
        if fence.key is not None and released is None and fence.admitted is None:
            # A callback that never claimed any relay input has no predecessor.
            _write_fence_file(fence, [])
        _status_fence.reset(token)


def stage_before_boundary(agent_dir, stage):
    """Fence accepted relay metadata under the same runtime order before a switch or exit."""
    agent_dir = Path(agent_dir)
    with lock(agent_dir / '.runtime.lock', create_parent=False):
        return _run_native_fence(_NativeFence(agent_dir), stage)


def _admit_report_batches(agent_dir, facts, prompt_batch, turn_batch, binding):
    from ..runtime import prompt_events

    prepared = []
    if prompt_batch is not None:
        native_id, transitions = prompt_batch
        prepared.append(prompt_events.prepare_native_batch(agent_dir, facts, native_id, transitions,
                                                           warn_prompt_scope=True, properties=binding))
    if turn_batch is not None:
        root, native_id, transitions = turn_batch
        prepared.append(prompt_events.prepare_native_batch(agent_dir, facts, native_id, transitions, root=root,
                                                           properties=binding))
    deferred = False
    for batch in prepared:
        if batch is not None:
            deferred = _admit_native_batch(agent_dir, facts, *batch, blocking=False) or deferred
    return deferred


def mutate_runtime(agent_dir, launch_id, changes_for, *, create_parent=False, blocking=True, status_observed=False,
                   prompt_batch=None, turn_batch=None, before_status_event=None):
    """Reduce current facts under the runtime lock; None leaves the record alone.

    Reducers are core bookkeeping only: no native calls or plugin callbacks.
    Keep launch guards, refusal precedence and status events shared with writes.
    """
    from ..runtime import prompt_events

    ended = status_event = None
    journal_status = False
    with lock(agent_dir / ".runtime.lock", create_parent=create_parent, blocking=blocking):
        data = runtime(agent_dir)
        if data.get("launch_id") != launch_id:
            raise ValueError("Outdated launch; refusing to update current runtime")
        binding = _read_ordered_binding(agent_dir)
        lineage = _read_session_lineage(agent_dir) if binding is None else None
        data = _recover_session_event_locked(agent_dir, data, binding)
        staged_statuses = _stage_status_intents(agent_dir, data, blocking=False, maximum=8)
        deferred_session = _reserve_missing_session(agent_dir, data, binding, lineage=lineage)
        deferred = _admit_report_batches(agent_dir, data, prompt_batch, turn_batch, binding) or deferred_session
        changes = changes_for(data)
        if changes is not None:
            before = data.get("status")
            last_reported = data.get('last_reported_status', data.get('native_status_value', before))
            primary = data.get('native_refusal')
            data.update(changes, observed_at=now())
            if primary or data.get('native_refusal'):
                data['native_refusal'] = primary or data['native_refusal']
                data['reason'] = data['error'] = data['native_refusal']['message']
                data['startup_delivery'] = 'failed'
                data['delivery_error'] = data['reason']
                data['status'] = 'unknown'
            if status_observed and 'status' in changes and data.get('status') in ('working', 'waiting', 'idle'):
                # Track the harness report when received, even if stop defers its effect on the displayed status.
                data['last_reported_status'] = data['status']
                if data['status'] != last_reported:
                    data['status_changed_at'] = data['observed_at']
            if before == 'waiting' and data.get('state') == 'ending':
                # A stopping harness can report activity before cancellation
                # succeeds. Keep its wait until a terminal state confirms exit.
                if 'status' in changes:
                    if data.get('status') in ('working', 'idle'):
                        data['deferred_stop_status'] = data['status']
                    else:
                        data.pop('deferred_stop_status', None)
                data['status'] = 'waiting'
            elif data.get('state') != 'ending':
                data.pop('deferred_stop_status', None)
            # A turn ends when the harness goes idle from working, or from waiting on the person inside a turn: a
            # command's dialog the person opens while the agent is idle (Claude's /model picker) waits outside any turn.
            # `in_turn` (exactly true) is set while working, and when a wait starts from working (a record from before the
            # flag still proves its turn that way); it is cleared when the agent goes idle.
            turn_end = data.get('status') == 'idle' and (before == 'working' or (before == 'waiting' and data.get('in_turn') is True))
            if data.get('status') == 'working' or (data.get('status') == 'waiting' and before == 'working'):
                data['in_turn'] = True
            elif data.get('status') == 'idle':
                data.pop('in_turn', None)
            if data.get('status') != before:
                status_event = {"from": before, "to": data.get('status')}
                required = _has_status_intents(data) or prompt_events.wants_status_intent(agent_dir)
                journal_status = required or before_status_event is not None
                if journal_status:
                    reservation = _intent(data, 'agent.status', status_event)
                    reservation.update(at=data['observed_at'], required=required,
                                       terminal=data.get('state') in ('failed', 'stopped', 'dormant'))
                    if before_status_event is not None:
                        reservation['fenced'] = True
                        if binding is None and lineage is None:
                            # Keep the last unanchored fence's position. The binding check
                            # can clear before the relay has secured this fence's prefix.
                            data['ordered_binding_fence'] = reservation['key']
                    _append_status_intent(agent_dir, data, reservation)
            write(agent_dir / "runtime.json", data)
            if status_event is not None:
                if before_status_event is not None:
                    scope = {key: data[key] for key in ('launch_id', 'state', 'session_generation') if key in data}
                    lineage_tail = tuple(row['id'] for row in lineage[-1:]) if lineage is not None else None
                    fence = _NativeFence(Path(agent_dir), reservation['key'], launch=launch_id,
                                         binding=binding, scope=scope, at=reservation['at'], lineage_tail=lineage_tail,
                                         fresh_companions={False, True})
                    try:
                        fenced = _run_native_fence(fence, before_status_event, status_event['to'])
                    finally:
                        fence.fresh_companions.clear()
                    data = runtime(agent_dir, required=True)
                else:
                    fenced = None
                if fenced is not False and not deferred_session:
                    staged_statuses.extend(_stage_status_intents(agent_dir, data, blocking=False, maximum=8))
            elif deferred and not deferred_session:
                staged_statuses.extend(_stage_status_intents(agent_dir, data, blocking=False, maximum=8))
            # Under the lock, so a focus --turn, which reads the status under it too, is ordered with this turn end.
            if turn_end:
                ended = focus.turn_ended(agent_dir)
        elif deferred:
            write(agent_dir / "runtime.json", data)
    if journal_status or deferred or prompt_batch and prompt_batch[1] or turn_batch and turn_batch[2]:
        from ..plugins import events, projection_wake

        if (root := events.root_of(agent_dir)) is not None:
            projection_wake.wake(root, tree=True)
    if ended:
        # The turn ended a focus: tell the agent about messages it held, after the lock.
        focus.after_turn(agent_dir, ended)
    if changes is not None and 'status' in changes:
        prompt_events.finish(agent_dir, launch_id, previous=True)
    for staged in staged_statuses:
        prompt_events.resolve_wait_status(agent_dir, staged)
    if (staged_statuses or prompt_batch is not None or turn_batch is not None
            or changes is not None and 'status' in changes):
        prompt_events.publish(agent_dir)
    return data if changes is not None else None


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
    with lock(agent_dir / '.runtime.lock', create_parent=False):
        with sessions.namespace(root), lock(agent_dir / '.properties.lock'):
            data, repaired = sessions.recover(agent_dir, read(agent_dir / 'properties.json'))
        if repaired:
            ordered = _stage_session_locked(agent_dir, repaired)
        elif data.get('ordered_session_transition') is not None:
            _recover_session_event_locked(agent_dir, runtime(agent_dir, required=True))
    if repaired:
        _session_event(agent_dir, repaired, ordered=ordered)
    return data


_SESSION_UNSTAGED = object()


def _stage_session_locked(directory, row):
    """Reserve the observed switch while later native callbacks still wait on runtime."""
    from ..plugins import events
    from ..runtime import prompt_events

    try:
        data = {key: row[key] for key in ('from', 'to', 'reason')}
        root = events.root_of(directory)
        required = (prompt_events.wants_status_intent(directory)
                    or root is not None and bool(events.subscribers(root, 'agent.session')))
        return _stage_after_status_locked(directory, 'agent.session', row['launch'], data, at=row.get('at'),
                                           close_scope={'native_id': row['from'], 'cause': 'switched'},
                                           required=required, session_key=row.get('id'))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def _session_event(directory, row, *, ordered=_SESSION_UNSTAGED):
    from ..runtime import prompt_events

    data = {key: row[key] for key in ('from', 'to', 'reason')}
    if ordered is _SESSION_UNSTAGED:
        if prompt_events.publish_ordered(directory, 'agent.session', row['launch'], data, at=row.get('at'),
                                         close_scope={'native_id': row['from'], 'cause': 'switched'}):
            return
    elif ordered is not None:
        if ordered:
            prompt_events.publish(directory)
        return
    _event(directory, 'agent.session', row['launch'], data)


def _rebind_session(root, agent_dir, previous, session_id, reason, launch_id, *, home=None):
    """Internal form: caller already holds this agent's launch lock."""
    try:
        with lock(agent_dir / '.runtime.lock', create_parent=False):
            facts = runtime(agent_dir, required=True)
            _stage_status_intents(agent_dir, facts, blocking=False, maximum=8)
            if facts.get('ordered_binding_check') or facts.get('ordered_binding_fence'):
                raise ValueError('Cannot switch conversations until the earlier binding check can be proved')
            # A previous append can be durable while properties still point
            # to its predecessor. Secure the repaired switch before another
            # transition can replace the visible marker.
            with sessions.namespace(root), lock(agent_dir / '.properties.lock'):
                binding, _ = sessions.recover(agent_dir, read(agent_dir / 'properties.json'))
            facts = _recover_session_event_locked(agent_dir, facts, binding)
            if _reserve_missing_session(agent_dir, facts, binding):
                write(agent_dir / 'runtime.json', facts)
            data, row = sessions.transition(root, agent_dir, previous, session_id, reason, launch_id, home)
            if row:
                ordered = _stage_session_locked(agent_dir, row)
    except sessions.SessionConflict as error:
        if runtime(agent_dir).get('binding_conflict') != str(error):
            update_runtime(agent_dir, launch_id, binding_conflict=str(error))
        raise
    if row:
        _session_event(agent_dir, row, ordered=ordered)
    if runtime(agent_dir).get('binding_conflict'):
        update_runtime(agent_dir, launch_id, binding_conflict=None)
    return data


def rebind_session(root, reference, previous, session_id, reason, launch_id, *, home=None):
    """CAS a native session for an exact launch; retain its earlier generations."""
    agent_dir = directory(root, reference)
    with lock(agent_dir / '.launch.lock'):
        return _rebind_session(root, agent_dir, previous, session_id, reason, launch_id, home=home)


def report(root, reference, phase=None, summary=None, *, blocked_on=None):
    agent_dir = directory(root, reference, allow_damaged=True)
    with lock(agent_dir / ".properties.lock"):
        data = records.properties(agent_dir / "properties.json", harness_optional=True)
        if phase is not None:
            data["phase"] = phase
        if phase is not None or data.get("phase") != "blocked":
            data.pop("blocked_on", None)
        if blocked_on is not None:
            if data.get("phase") != "blocked":
                raise ValueError("--blocked-on requires --phase blocked")
            data["blocked_on"] = blocked_on
        if summary is not None:
            data["summary"] = summary
        data["reported_at"] = data["updated_at"] = now()
        write(agent_dir / "properties.json", data)
    _reported(agent_dir, data)
    return data


def _reported(agent_dir, data):
    """agent.reported, with the current launch when the runtime record can be read; never fails a report."""
    try:
        launch = runtime(agent_dir).get("launch_id")
    except Exception:
        launch = None
    _event(agent_dir, "agent.reported", launch, {"phase": data.get("phase"), "summary": data.get("summary"),
                                                     **({"blocked_on": data["blocked_on"]} if data.get("blocked_on") else {})}, data)


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
