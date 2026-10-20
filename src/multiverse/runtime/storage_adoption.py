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
        output = backend.run('show-environment', '-g')
        if not isinstance(output, str):
            raise ValueError('terminal server did not return its environment')
        values = dict(line.split('=', 1) for line in output.splitlines()
                      if '=' in line and line.split('=', 1)[0] in (*keys, 'HOME'))
        if values:
            candidates.append(('terminal server', _environment(values, keys), properties['workspace']))
    except Exception as error:
        reason = str(error)
        if not ('no server running' in reason or 'No such file or directory' in reason and 'connecting to' in reason):
            problems.append('cannot read the recorded terminal server environment: ' + reason)
    candidates.append(('calling shell', _environment(ambient, keys), Path.cwd()))
    session = SessionHandle.from_records(properties, previous, directory)
    hook = adapter_hook(adapter, 'session_storage_evidence')
    homes = {}
    for label, environment, base in candidates:
        try:
            home, selected = _candidate_home(adapter, environment, base)
            if home is None:
                # A partial old request with no home is not a new location to
                # scan. It cannot supply a default from this checking process.
                if label != 'launch request':
                    problems.append(f'{label} does not identify a storage home')
                continue
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
    if problems or len(found) != 1:
        choices = ', '.join(f'{home!r} ({"present" if present is True else "absent" if present is False else "unknown"})'
                            for home, (present, _) in homes.items()) or 'no identifiable home'
        variable = keys[0] if keys else 'the harness storage variable'
        raise ValueError(f'Cannot prove unique conversation storage for {directory / "properties.json"}. '
            f'Candidates: {choices}. ' + ('; '.join(problems) + '. ' if problems else '') +
            f'Resume from a shell whose {variable} points at the home holding this conversation. '
            'Restore access to unreadable candidates and resolve duplicate copies before retrying; no storage was adopted')
    home = found[0]
    selected = homes[home][1]
    return selected, f'Adopted the existing conversation storage {home!r} after finding this session there'
