"""Shared one-shot launch and recorded-storage resolution, without plugin API objects."""
import os

HOME_VARIABLES = {'claude': ('CLAUDE_CONFIG_DIR', '.claude'), 'codex': ('CODEX_HOME', '.codex'),
                  'pi': ('PI_CODING_AGENT_DIR', None)}


def launch(adapter):
    spec = getattr(adapter, 'spec', {}) or {}
    environment = {key: value for key, value in os.environ.items() if not key.startswith('MV2_')}
    environment.update(dict.fromkeys(spec.get('unset_env') or (), None))
    environment.update(spec.get('env') or {})
    return environment


def absolute_storage(adapter, environment, base):
    for key in getattr(adapter, 'storage_env', ()) or ():
        value = environment.get(key)
        if isinstance(value, str) and value and not os.path.isabs(value):
            if base is None:
                return None
            environment[key] = os.path.normpath(os.path.join(base, value))
    return environment


def replayable(family, recorded, keys, workspace):
    variable = HOME_VARIABLES.get(family, (None, None))[0]
    result = {}
    for key in keys:
        value = recorded[key]
        if isinstance(value, str) and value and not os.path.isabs(value):
            if not (isinstance(workspace, str) and os.path.isabs(workspace)):
                return None
            value = os.path.normpath(os.path.join(workspace, value))
        result[key] = value
    if variable in result and not result[variable]:
        if not recorded.get('HOME'):
            return None
        result['HOME'] = recorded['HOME']
    return result
