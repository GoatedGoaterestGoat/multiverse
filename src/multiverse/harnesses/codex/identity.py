"""The conversation's stable tool identity, separate from its terminal launch."""
from pathlib import Path
import json

from ..types import AgentIdentitySpec


POLICY = AgentIdentitySpec('codex-agent-v1', 'CODEX_THREAD_ID', ('CODEX_HOME', 'CODEX_SQLITE_HOME'))
# These are core's generation-specific keys. Do not treat every MV2_ name as
# launch identity: validated plugins own settings under their MV2_<NAME> prefix.
# Child launches find their tmux server in the verified agent runtime. Keeping
# a socket here would freeze one launch's server choice in shared tools.
VOLATILE = frozenset({'MV2_LAUNCH_ID', 'MV2_CONTROL_SOCKET', 'MV2_TMUX_SOCKET'})
STABLE = frozenset({'MV2_IDENTITY_MODE', 'MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR',
                    'MV2_CLI', 'MV2_PYTHON', 'PATH'})


def resume_notice(caller_environment, effective_environment):
    """Explain a caller override discarded by the saved native environment."""
    notices = []
    for key, label in (('CODEX_HOME', 'Codex home'), ('CODEX_SQLITE_HOME', 'Codex database home')):
        requested, saved = caller_environment.get(key), effective_environment.get(key)
        if not isinstance(requested, str) or not requested:
            continue
        if key == 'CODEX_SQLITE_HOME' and saved is None:
            notices.append(f'Kept its saved Codex database home unset; ignored {key}={json.dumps(requested)}.')
            continue
        if not isinstance(saved, str) or not saved:
            continue
        try:
            kept = Path(saved).expanduser().resolve()
            if Path(requested).expanduser().resolve() == kept:
                continue
        except (OSError, ValueError, RuntimeError):
            continue  # A diagnostic cannot claim an unverified storage choice.
        notices.append(f'Kept its saved {label} {json.dumps(str(kept))}; ignored {key}={json.dumps(requested)}.')
    return ' '.join(notices) or None


def storage_environment(context):
    from .storage import session_home
    # Core supplies HOME. Keep rendering partial contexts and relative explicit
    # paths compatible; candidate proof never falls back to this process's HOME.
    home = session_home(context.environment)
    if home is None:
        home = str(Path(context.environment.get('CODEX_HOME') or '~/.codex').expanduser().resolve())
    storage = {'CODEX_HOME': home}
    if context.environment.get('CODEX_SQLITE_HOME'):
        storage['CODEX_SQLITE_HOME'] = str(Path(context.environment['CODEX_SQLITE_HOME']).expanduser().resolve())
    return storage


def _storage_keys():
    # Resolve after adapter imports finish; keep the same source-owned union as
    # core's launch snapshot without treating foreign storage as Codex authority.
    from .. import BUILTINS
    return tuple(dict.fromkeys(key for adapter in BUILTINS.values()
                               for key in getattr(adapter, 'storage_env', ())))


def _session_storage_keys(selected):
    declared = _storage_keys()
    if selected is None:
        return declared  # Existing callers describe a complete selection.
    if not isinstance(selected, (tuple, list)) or any(key not in declared for key in selected):
        raise ValueError('Native storage keys must name declared harness storage variables')
    # Foreign-key uncertainty never weakens this conversation's own identity.
    return tuple(dict.fromkeys([*POLICY.storage_env, *selected]))


def tool_environment(context):
    # Core resolves these paths once for the agent; never derive them from a
    # parent's ambient identity or duplicate core's CLI/interpreter discovery.
    env = {key: value for key, value in context.agent_environment.items() if key in STABLE}
    # Even an incomplete caller-supplied context must not look like an operator.
    env['MV2_IDENTITY_MODE'] = POLICY.mode
    env.update({key: context.environment[key] for key in _session_storage_keys(context.storage_keys)
                if key not in POLICY.storage_env and key in context.environment})
    env.update(storage_environment(context))
    return env


def session_environment(config, stable, storage_keys=None):
    """Native/user defaults cannot put launch identity back into shared tools."""
    policy = config.setdefault('shell_environment_policy', {})
    if not isinstance(policy, dict) or not isinstance(policy.get('set', {}), dict):
        raise ValueError('Native shell_environment_policy and set must be objects')
    excludes = policy.get('exclude') or []
    if not isinstance(excludes, list) or any(not isinstance(key, str) for key in excludes):
        raise ValueError('Native shell environment exclusions must be an array of strings')
    values = dict(policy.get('set') or {})
    storage = _session_storage_keys(storage_keys)
    for key in (*VOLATILE, *STABLE, *storage, 'CODEX_THREAD_ID'):
        values.pop(key, None)
    values.update(stable)
    policy['set'] = values
    policy['exclude'] = list(dict.fromkeys([*excludes, 'MV2_*', *storage]))
    return config
