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
    requested = caller_environment.get('CODEX_HOME')
    saved = effective_environment.get('CODEX_HOME')
    if not isinstance(requested, str) or not requested or not isinstance(saved, str) or not saved:
        return None
    try:
        kept = Path(saved).expanduser().resolve()
        if Path(requested).expanduser().resolve() == kept:
            return None
    except (OSError, ValueError, RuntimeError):
        return None  # A diagnostic cannot claim an unverified storage choice.
    return (f'Kept its saved Codex home {json.dumps(str(kept))}; '
            f'ignored CODEX_HOME={json.dumps(requested)}.')


def storage_environment(context):
    storage = {'CODEX_HOME': str(Path(context.environment.get('CODEX_HOME', '~/.codex')).expanduser().resolve())}
    if context.environment.get('CODEX_SQLITE_HOME'):
        storage['CODEX_SQLITE_HOME'] = str(Path(context.environment['CODEX_SQLITE_HOME']).expanduser().resolve())
    return storage


def tool_environment(context):
    # Core resolves these paths once for the agent; never derive them from a
    # parent's ambient identity or duplicate core's CLI/interpreter discovery.
    env = {key: value for key, value in context.agent_environment.items() if key in STABLE}
    # Even an incomplete caller-supplied context must not look like an operator.
    env['MV2_IDENTITY_MODE'] = POLICY.mode
    env.update(storage_environment(context))
    return env


def session_environment(config, stable):
    """Native/user defaults cannot put launch identity back into shared tools."""
    policy = config.setdefault('shell_environment_policy', {})
    if not isinstance(policy, dict) or not isinstance(policy.get('set', {}), dict):
        raise ValueError('Native shell_environment_policy and set must be objects')
    excludes = policy.get('exclude') or []
    if not isinstance(excludes, list) or any(not isinstance(key, str) for key in excludes):
        raise ValueError('Native shell environment exclusions must be an array of strings')
    values = dict(policy.get('set') or {})
    for key in (*VOLATILE, *STABLE, 'CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_THREAD_ID'):
        values.pop(key, None)
    values.update(stable)
    policy['set'] = values
    policy['exclude'] = list(dict.fromkeys([*excludes, 'MV2_*']))
    return config
