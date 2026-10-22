"""Read-only discovery of an unrecorded legacy conversation home."""
from pathlib import Path
from dataclasses import dataclass
import os
import stat

from ..harnesses.types import SessionHandle
from ..universe import storage as records
from ..universe import records as agent_records, sessions
from .native import adapter_hook


@dataclass(frozen=True)
class Adoption:
    environment: dict
    notice: str
    fresh_start: bool = False


def _environment(source, keys):
    return {key: source.get(key) for key in (*keys, 'HOME')}


def _candidate_home(adapter, environment, base):
    """Resolve only the candidate's values, never an adapter's ambient default."""
    keys = getattr(adapter, 'storage_env', ())
    selected = dict(environment)
    home = selected.get('HOME')
    if home is not None and (not isinstance(home, str) or not Path(home).is_absolute() or '\0' in home):
        return None, selected
    if any(value is not None and (not isinstance(value, str) or '\0' in value)
           for key in keys for value in (selected.get(key),)):
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


def _launch_requests(directory):
    """Every retained request, as evidence only: it grants no launch authority."""
    launches = directory / 'launches'
    try:
        entries = sorted(launches.iterdir())
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ValueError(f'Cannot verify conversation storage: cannot read {launches}: {error}') from error
    requests = []
    for launch in entries:
        path = launch / 'request.json'
        try:
            if not stat.S_ISDIR(launch.lstat().st_mode):
                raise ValueError(f'Cannot verify conversation storage: {launch} is not a regular launch directory')
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue  # Failed preparations need not have published a request.
            if not stat.S_ISREG(mode):
                raise ValueError(f'Cannot verify conversation storage: {path} is not a regular request file')
            requests.append((path, agent_records.properties(path)))
        except (OSError, agent_records.RecordError) as error:
            raise ValueError(f'Cannot verify conversation storage: repair or restore read access to {path}: {error}') from error
    return requests


def recorded_sockets(directory):
    """The terminal servers retained requests ran under. A legacy launch's storage came from its server."""
    sockets = []
    for path, request in _launch_requests(directory):
        env = request.get('env', {})
        saved = env.get('MV2_TMUX_SOCKET') if isinstance(env, dict) else None
        if saved is None:
            continue
        if not isinstance(saved, str) or not saved or '\0' in saved or not Path(saved).is_absolute():
            raise ValueError(f'Cannot verify conversation storage: repair the recorded terminal socket in {path}')
        if saved not in sockets:
            sockets.append(saved)
    return sockets


def _launch_environments(directory, keys):
    """Read storage evidence from every retained request, without granting launch authority."""
    environments = []
    for path, request in _launch_requests(directory):
        env, unset = request.get('env', {}), request.get('unset_env', [])
        if (not isinstance(env, dict) or not isinstance(unset, list)
                or any(not isinstance(key, str) or '\0' in key for key in unset)
                or any(key in env and (not isinstance(env[key], str) or '\0' in env[key]) for key in (*keys, 'HOME'))):
            raise ValueError(f'Cannot verify conversation storage: repair the launch environment in {path}')
        selected = records.request_environment(request, (*keys, 'HOME'))
        if selected:
            environments.append((f'launch request {path}', selected))
    return environments


def _saved_environments(properties, keys):
    service = properties.get('native_service_identity', {})
    if service is None:
        service = {}
    if not isinstance(service, dict):
        raise ValueError('Cannot verify conversation storage: repair the saved native service identity')
    candidates = []
    for source in (properties.get('native_storage_environment'), properties.get('native_environment'), service.get('scope')):
        if source is None:
            continue
        if not isinstance(source, dict):
            raise ValueError('Cannot verify conversation storage: repair the saved storage record')
        if keys and keys[0] in source or 'HOME' in source:
            candidates.append(dict(source))
    if keys and properties.get('native_session_home') is not None:
        # An evidence candidate only, not the replacement launch's literal variable.
        candidates.append({keys[0]: properties['native_session_home']})
    return candidates


def prove_missing(adapter, properties, previous, directory, ambient, backends):
    """An explicit recovery discards no readable or uninspectable conversation."""
    if getattr(adapter, 'session_mode', None) == 'file':
        _launch_environments(directory, tuple(getattr(adapter, 'storage_env', ())))
        path = directory / 'session.jsonl'
        try:
            path.lstat()
        except FileNotFoundError:
            return Adoption({}, f'Conversation file {str(path)!r} is absent; starting fresh with a new conversation', True)
        except OSError as error:
            raise ValueError(f'Cannot verify missing conversation {path}: {error}; restore read access before retrying') from error
        raise ValueError(f'Conversation file {path} still exists; --start-fresh only recovers a missing conversation')
    return discover(adapter, properties, previous, directory, ambient, backends, require_absent=True)


def discover(adapter, properties, previous, directory, ambient, backends, *, recorded=None, require_absent=False):
    """Adopt exactly one proven candidate, with absence proved in every other home.

    `backends` are the terminal servers records name; each one running is a candidate.
    """
    keys = tuple(getattr(adapter, 'storage_env', ()))
    candidates = []
    if recorded is not None:
        candidates.append(('saved storage', dict(recorded), properties['workspace']))
    candidates.extend(('saved storage', source, properties['workspace']) for source in _saved_environments(properties, keys))
    candidates.extend((label, source, properties['workspace']) for label, source in _launch_environments(directory, keys))
    # A complete global environment distinguishes unset from unknown. An absent
    # server supplies no candidate, while an unreadable server prevents proof.
    problems = []
    for backend in backends:
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
    unbound = (require_absent and getattr(adapter, 'session_mode', None) == 'reported'
               and sessions.unbound_fresh(directory, properties))
    hook = adapter_hook(adapter, 'session_storage_evidence')
    homes = {}
    caller = None
    for label, environment, base in candidates:
        try:
            home, selected = _candidate_home(adapter, environment, base)
            if home is None:
                # A partial old request with no home is not a new location to
                # scan. It cannot supply a default from this checking process.
                if not label.startswith('launch request ') or environment.get('HOME') or keys and environment.get(keys[0]):
                    problems.append(f'{label} does not identify a storage home')
                continue
            if label == 'calling shell':
                caller = (home, selected)
            if home in homes:
                continue
            if unbound:
                # No UUID was bound after this retirement. The lineage proves
                # absence of a current conversation; a finder cannot query None.
                present = False
            else:
                result = hook(session, home) if hook else None
                present = result.value.get('present') if result and isinstance(result.value, dict) else None
                if result is None or result.state != 'ok' or type(present) is not bool:
                    problems.append(f'{home}: {result.reason if result else "no storage evidence hook"}')
                    present = None
            homes[home] = (present, selected)
        except Exception as error:
            problems.append(f'{label}: {type(error).__name__}: {error}')
    found = [home for home, (present, _) in homes.items() if present is True]
    if require_absent:
        if not problems and not found and homes and caller and all(value[0] is False for value in homes.values()):
            reason = ('No conversation was bound since the last fresh start' if unbound
                      else 'Conversation is absent from every candidate storage')
            return Adoption(caller[1], f'{reason}; starting fresh in '
                                      f'{caller[0]!r} with a new conversation', True)
        choices = ', '.join(f'{home!r} ({"present" if present is True else "absent" if present is False else "unknown"})'
                            for home, (present, _) in homes.items()) or 'no identifiable home'
        raise ValueError(f'Cannot start fresh: every candidate conversation storage must be readable and absent. '
                         f'Candidates: {choices}. ' + ('; '.join(problems).rstrip('.') + '. ' if problems else '') +
                         'Restore access or resume the existing conversation; no records were changed')
    if (not problems and not found and homes and caller and all(value[0] is False for value in homes.values())
            and _never_started(adapter, properties, previous, directory)):
        return Adoption(caller[1],
                        f'No conversation exists in any candidate storage and no native child was started; '
                        f'starting fresh in {caller[0]!r} with the same assigned session ID', fresh_start=True)
    if problems or len(found) != 1:
        choices = ', '.join(f'{home!r} ({"present" if present is True else "absent" if present is False else "unknown"})'
                            for home, (present, _) in homes.items()) or 'no identifiable home'
        variable = keys[0] if keys else 'the harness storage variable'
        raise ValueError(f'Cannot prove unique conversation storage for {directory / "properties.json"}. '
            f'Candidates: {choices}. ' + ('; '.join(problems) + '. ' if problems else '') +
            f'Resume from a shell whose {variable} points at the home holding this conversation. '
            'Restore access to unreadable candidates and resolve duplicate copies before retrying; no storage was adopted. '
            'If the conversation is gone from every readable candidate, use '
            f'mv2 resume {properties["id"]} --start-fresh to keep this agent with a new conversation')
    home = found[0]
    selected = homes[home][1]
    return Adoption(selected, f'Adopted the existing conversation storage {home!r} after finding this session there')
