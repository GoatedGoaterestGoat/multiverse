"""Read launch storage evidence without consulting a reader's environment or changing records."""
import os
from pathlib import Path
from uuid import UUID

from . import records


def launch_request(directory, runtime):
    identifier = (runtime or {}).get('launch_id')
    try:
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            return {}
        return records.properties(Path(directory) / 'launches' / identifier / 'request.json')
    except (ValueError, AttributeError):
        return {}


def request_environment(request, keys):
    env, unset = request.get('env', {}), request.get('unset_env', [])
    if not isinstance(env, dict) or not isinstance(unset, list):
        return {}
    return {key: None if key in unset else env[key] for key in keys
            if key in unset or isinstance(env.get(key), str)}


def snapshot_environment(properties, request, keys):
    saved = properties.get('native_storage_environment')
    if saved is None:
        return None
    if not isinstance(saved, dict) or any(value is not None and
            (not isinstance(value, str) or '\0' in value) for value in saved.values()):
        raise ValueError('Invalid native_storage_environment in properties.json; restore the launch storage values')
    historical = request_environment(request, keys)
    return {key: saved[key] if key in saved else historical.get(key) for key in keys}


def legacy_codex_environment(properties, request):
    """Adopt historical launch evidence, never today's caller or tmux environment."""
    keys = ('CODEX_HOME', 'CODEX_SQLITE_HOME')
    identity = properties.get('native_service_identity') or {}
    scope = identity.get('scope') if isinstance(identity, dict) and identity.get('provider') == 'codex' else None
    if isinstance(scope, dict) and scope.get('CODEX_HOME'):
        saved = {key: scope.get(key) for key in keys}
    else:
        saved = request_environment(request, keys)
        # An unset default is proven only together with the launching HOME.
        if 'CODEX_HOME' in saved and not saved['CODEX_HOME']:
            home = request_environment(request, ('HOME',)).get('HOME')
            if isinstance(home, str) and Path(home).is_absolute():
                saved['CODEX_HOME'] = str(Path(home) / '.codex')
    if (set(saved) != set(keys) or not saved.get('CODEX_HOME')
            or any(value is not None and (not isinstance(value, str) or not value or '\0' in value
                   or not Path(value).is_absolute() or os.path.normpath(value) != value) for value in saved.values())):
        return None
    return saved


def recorded_environment(directory, properties, family, keys, runtime=None):
    """Return recorded storage (and proven HOME), or None when the launch's home is unknown.

    No alias resolution: saved canonical paths must not follow a later retarget.
    The caller decides whether an agent has never launched and may use defaults.
    """
    try:
        if runtime is None:
            runtime = records.runtime(Path(directory) / 'runtime.json')
        request = launch_request(directory, runtime)
        saved = snapshot_environment(properties, request, keys)
        if saved is None:
            saved = (legacy_codex_environment(properties, request) if family == 'codex'
                     else request_environment(request, keys))
            if saved is None or any(key not in saved for key in keys):
                return None
        home = request_environment(request, ('HOME',)).get('HOME')
        return {**saved, **({'HOME': home} if isinstance(home, str) and Path(home).is_absolute() else {})}
    except (OSError, ValueError, TypeError, AttributeError):
        return None
