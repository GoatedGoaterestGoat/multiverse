"""Read-only discovery of an unrecorded legacy conversation home."""
from pathlib import Path
import os

from ..harnesses.types import SessionHandle
from ..universe import storage as records
from ..universe import records as agent_records
from .native import adapter_hook


def _environment(source, keys):
    return {key: source.get(key) for key in (*keys, 'HOME')}


def _candidate_home(adapter, environment, base):
    """Resolve only the candidate's values, never an adapter's ambient default."""
    keys = getattr(adapter, 'storage_env', ())
    selected = dict(environment)
    home = selected.get('HOME')
    if home is not None and (not isinstance(home, str) or not Path(home).is_absolute() or '\0' in home):
        return None, selected
    if not home and not (keys and selected.get(keys[0])):
        return None, selected
    for key in keys:
        value = selected.get(key)
        if not value:
            continue
        if not isinstance(value, str) or '\0' in value:
            return None, selected
        if value == '~' or value.startswith('~/'):
            if not isinstance(home, str) or not Path(home).is_absolute():
                return None, selected
            value = str(Path(home) / value[2:]) if value != '~' else home
        path = Path(value)
        selected[key] = str(path if path.is_absolute() else Path(base) / path)
    hook = adapter_hook(adapter, 'session_home')
    result = hook(selected) if hook else None
    if not isinstance(result, str) or not Path(result).is_absolute():
        return None, selected
    return os.path.realpath(result), selected


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _never_started(adapter, properties, previous, directory):
    """Only an assigned, unpublished native conversation can safely start fresh.

    A reported ID already came from a native conversation. Missing history or
    an idle process cannot prove that it never ran a turn.
    """
    if (getattr(adapter, 'session_mode', None) != 'assigned'
            or properties.get('native_session_id') != properties.get('id')
            or properties.get('phase', 'starting') != 'starting'):
        return False
    try:
        # Resume may have discarded damaged runtime authority after proving exit.
        # Exit is not proof that this conversation never ran: inspect the saved facts.
        previous = agent_records.runtime(directory / 'runtime.json')
        if previous and (previous.get('state') not in ('starting', 'failed') or any(
                key in previous for key in ('pid', 'exit_code', 'usage', 'last_usage', 'native_bridge', 'native_service'))):
            return False
        for name in ('sessions.jsonl', 'session.jsonl', 'native-delivery.json'):
            path = directory / name
            try:
                if path.read_bytes().strip():
                    return False
            except FileNotFoundError:
                pass
        try:
            (directory / 'inbox-state.json').stat()
        except FileNotFoundError:
            states = {}
        else:
            states = agent_records.properties(directory / 'inbox-state.json')
        for state in states.values():
            if (not isinstance(state, dict) or state.get('submission_state') not in ('pending', 'failed')
                    or state.get('native_receipt') or not isinstance(state.get('attempts', []), list)
                    or any(not isinstance(attempt, dict) or attempt.get('state') not in ('error', 'unavailable')
                           for attempt in state.get('attempts', []))):
                return False
        receipts = directory / 'claude-receipts'
        if _present(receipts) and any(receipts.iterdir()):
            return False
        launches = directory / 'launches'
        if not _present(launches):
            return not previous
        entries = list(launches.iterdir())
        if previous and not any(path.name == previous.get('launch_id') for path in entries):
            return False
        for launch in entries:
            if not launch.is_dir():
                return False
            # A ready/request publication could have released a child even if
            # the subsequent runtime write was lost. Never infer exit from it.
            if any(_present(launch / name) for name in ('request.json', 'ready', 'cleanup.json', 'roles')):
                return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def discover(adapter, properties, previous, directory, request, ambient, backend):
    """Adopt exactly one proven candidate, with absence proved in every other home."""
    keys = tuple(getattr(adapter, 'storage_env', ()))
    identifier = previous.get('launch_id')
    path = directory / 'launches' / identifier / 'request.json' if identifier else None
    try:
        if path is not None:
            path.stat()
    except FileNotFoundError:
        pass  # A missing request supplies no candidate.
    else:
        # The general reader tolerates missing/damaged historical evidence.
        # Adoption cannot treat an unreadable candidate location as absent.
        request = agent_records.properties(path) if path is not None else {}
        env, unset = request.get('env', {}), request.get('unset_env', [])
        if (not isinstance(env, dict) or not isinstance(unset, list)
                or any(key in env and not isinstance(env[key], str) for key in (*keys, 'HOME'))):
            raise ValueError(f'Cannot verify conversation storage: repair the launch environment in {path}')
    historical = records.request_environment(request, (*keys, 'HOME'))
    candidates = []
    if historical:
        candidates.append(('launch request', historical, properties['workspace']))
    # A complete global environment distinguishes unset from unknown. An absent
    # server supplies no candidate, while an unreadable server prevents proof.
    problems = []
    try:
        values = {key: backend.environment(key) for key in (*keys, 'HOME')}
        if any(value is not None and not isinstance(value, str) for value in values.values()):
            raise ValueError('terminal server did not return its environment')
        candidates.append(('terminal server', values, properties['workspace']))
    except Exception as error:
        reason = str(error)
        if not ('no server running' in reason or 'No such file or directory' in reason and 'connecting to' in reason):
            problems.append('cannot read the recorded terminal server environment: ' + reason)
    candidates.append(('calling shell', _environment(ambient, keys), Path.cwd()))
    session = SessionHandle.from_records(properties, previous, directory)
    hook = adapter_hook(adapter, 'session_storage_evidence')
    homes = {}
    caller = None
    for label, environment, base in candidates:
        try:
            home, selected = _candidate_home(adapter, environment, base)
            if home is None:
                # A partial old request with no home is not a new location to
                # scan. It cannot supply a default from this checking process.
                if label != 'launch request':
                    problems.append(f'{label} does not identify a storage home')
                continue
            if label == 'calling shell':
                caller = (home, selected)
            if home in homes:
                continue
            result = hook(session, home) if hook else None
            present = result.value.get('present') if result and isinstance(result.value, dict) else None
            if result is None or result.state != 'ok' or type(present) is not bool:
                problems.append(f'{home}: {result.reason if result else "no storage evidence hook"}')
                present = None
            homes[home] = (present, selected)
        except Exception as error:
            problems.append(f'{label}: {type(error).__name__}: {error}')
    found = [home for home, (present, _) in homes.items() if present is True]
    if (not problems and not found and homes and caller and all(value[0] is False for value in homes.values())
            and _never_started(adapter, properties, previous, directory)):
        return caller[1], (f'No conversation exists in any candidate storage and no native child was started; '
                           f'starting fresh in {caller[0]!r} with the same assigned session ID')
    if problems or len(found) != 1:
        choices = ', '.join(f'{home!r} ({"present" if present is True else "absent" if present is False else "unknown"})'
                            for home, (present, _) in homes.items()) or 'no identifiable home'
        variable = keys[0] if keys else 'the harness storage variable'
        raise ValueError(f'Cannot prove unique conversation storage for {directory / "properties.json"}. '
            f'Candidates: {choices}. ' + ('; '.join(problems) + '. ' if problems else '') +
            f'Resume from a shell whose {variable} points at the home holding this conversation. '
            'Restore access to unreadable candidates and resolve duplicate copies before retrying; no storage was adopted. '
            'If the conversation is gone and this agent might have run a turn, remove this agent with '
            f'mv2 remove {properties["id"]}, then spawn a new agent')
    home = found[0]
    selected = homes[home][1]
    return selected, f'Adopted the existing conversation storage {home!r} after finding this session there'
