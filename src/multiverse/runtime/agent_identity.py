"""Explicit agent authority for tools in an adapter's shared conversation.

This is local identity validation, not a security boundary against another
process running as the same OS user. It never grants attachment control or
substitutes a current launch ID into an old caller's environment.
"""
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re

from ..harnesses.types import AgentIdentitySpec, normalize_session_id
from ..store import read


def specification(value):
    if (not isinstance(value, AgentIdentitySpec) or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', value.mode)
            or not re.fullmatch(r'[A-Z][A-Z0-9_]*', value.native_id_env)
            or any(not isinstance(key, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) for key in value.storage_env)):
        raise ValueError('Invalid agent identity policy')
    return value


def environment(target, spec, saved=None):
    if spec is None:
        return {}
    specification(spec)
    values = {key: target[key] for key in ('MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR',
                                          'MV2_CLI', 'MV2_PYTHON', 'PATH')}
    # Request the launching installation's tools each time. A shared native
    # conversation may retain its previous environment until an exclusive reload;
    # saved paths are diagnostics, not authority and never override this request.
    return {**values, 'MV2_IDENTITY_MODE': spec.mode}


def record(spec, properties, service):
    specification(spec)
    if (not service or service.lifetime != 'external' or not service.identity.provider
            or not service.identity.scope or set(service.identity.scope) - set(spec.storage_env)):
        raise ValueError('Agent identity requires an exact external native storage scope')
    scope = dict(service.identity.scope)
    if any(not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value).resolve()) != value
           for value in scope.values()):
        raise ValueError('Agent identity storage scope must be canonical')
    return {'schema': 1, 'spec': asdict(spec), 'service': asdict(service.identity),
            'native_id': properties.get('native_session_id'), 'workspace': str(Path(properties['workspace']).resolve())}


@dataclass(frozen=True)
class AgentActor:
    root: Path
    directory: Path
    identity: str
    name: str

    def sender(self):
        return {'agent_id': self.identity, 'universe': str(self.root), 'name': self.name}


def sender_actor(environment=None):
    """Stable conversation authority, or the exact current legacy launch.

    A launch-strict sender cannot fall through to an operator merely because it
    predates saved identity policies. No process observation or adoption occurs.
    """
    env = os.environ if environment is None else environment
    caller = actor(env)
    if caller is not None:
        return caller
    if not any(env.get(key) for key in ('MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID')):
        return None
    if any(not isinstance(env.get(key), str) or not env[key]
           for key in ('MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID')):
        raise ValueError('Calling agent identity is incomplete; cannot send as this agent')
    from ..universe import agents, records
    root = Path(env['MV2_UNIVERSE']).resolve()
    identity = normalize_session_id(env['MV2_AGENT_ID'])
    directory = root / 'state' / 'agents' / identity
    properties = records.properties(directory / 'properties.json', harness_optional=True)
    if (identity != env['MV2_AGENT_ID'] or properties.get('id') != identity
            or properties.get('universe_id') != read(root / 'universe.json')['id']
            or Path(env['MV2_AGENT_DIR']).resolve() != directory.resolve()):
        raise ValueError('Calling agent identity does not match its saved record')
    if agents.runtime(directory).get('launch_id') != env['MV2_LAUNCH_ID']:
        raise ValueError('Outdated launch; refusing to send as this agent')
    return AgentActor(root, directory, identity, properties['name'])


def calling_socket(environment=None):
    """Resolve an agent's current terminal server without trusting cached native socket values."""
    env = os.environ if environment is None else environment
    caller = actor(env)
    if caller is not None:
        directory = caller.directory
    elif env.get('MV2_AGENT_ID'):
        if not env.get('MV2_UNIVERSE') or not env.get('MV2_LAUNCH_ID'):
            raise ValueError('Calling agent identity is incomplete; cannot select its tmux server')
        root = Path(env['MV2_UNIVERSE']).resolve()
        identity = normalize_session_id(env['MV2_AGENT_ID'])
        directory = root / 'state' / 'agents' / identity
        properties = read(directory / 'properties.json')
        if (properties.get('id') != identity or properties.get('universe_id') != read(root / 'universe.json')['id']
                or (env.get('MV2_AGENT_DIR') and Path(env['MV2_AGENT_DIR']).resolve() != directory.resolve())):
            raise ValueError('Calling agent identity does not match its saved record')
    else:
        return env.get('MV2_TMUX_SOCKET')
    facts = read(directory / 'runtime.json')
    if caller is None and facts.get('launch_id') != env['MV2_LAUNCH_ID']:
        raise ValueError('Outdated launch; cannot select the calling agent\'s tmux server')
    terminal = facts.get('tmux')
    socket = terminal.get('socket') if isinstance(terminal, dict) else None
    if not isinstance(socket, str) or not socket or '\0' in socket:
        raise ValueError('Calling agent has no recorded tmux server')
    return socket


def actor(environment=None):
    """Validate explicit stable mode. Old launch contexts are never adopted.

    An unscoped human CLI remains an operator. A declared but incomplete agent
    context can never fall through to that case.
    """
    env = os.environ if environment is None else environment
    mode = env.get('MV2_IDENTITY_MODE')
    if mode is None and env.get('MV2_LAUNCH_ID'):
        return None  # Existing launch-strict callbacks; never agent authority.
    unfenced = bool(not env.get('MV2_LAUNCH_ID') and (env.get('MV2_AGENT_ID') or env.get('MV2_AGENT_DIR')))
    policy = None
    directory = None
    try:
        if env.get('MV2_UNIVERSE') and env.get('MV2_AGENT_ID'):
            root = Path(env['MV2_UNIVERSE']).resolve()
            identity = normalize_session_id(env['MV2_AGENT_ID'])
            if identity != env['MV2_AGENT_ID']:
                raise ValueError('Agent identity requires its full canonical ID')
            directory = root / 'state' / 'agents' / identity
            properties = read(directory / 'properties.json')
            policy = properties.get('agent_identity')
    except (OSError, ValueError, KeyError):
        if mode is not None or unfenced:
            raise ValueError('Agent identity could not be verified; restart this agent') from None
        return None  # Non-adopted launches keep their existing generation guards.
    if mode is None and policy is None and not unfenced:
        return None
    try:
        if not isinstance(policy, dict) or policy.get('schema') != 1:
            raise ValueError('No saved agent identity policy')
        spec = specification(AgentIdentitySpec(**policy['spec']))
        if mode != spec.mode:
            raise ValueError('Missing or incorrect agent identity mode')
        if not env.get('MV2_AGENT_DIR') or Path(env['MV2_AGENT_DIR']).resolve() != directory.resolve():
            raise ValueError('Agent directory does not match the declared identity')
        universe = read(root / 'universe.json')
        if properties['id'] != identity or properties.get('universe_id') != universe['id']:
            raise ValueError('Agent or universe identity changed')
        native_id = normalize_session_id(env.get(spec.native_id_env))
        if native_id != policy.get('native_id') or native_id != properties.get('native_session_id'):
            raise ValueError('This conversation does not match the saved agent identity')
        if str(Path(properties['workspace']).resolve()) != policy['workspace']:
            raise ValueError('Agent workspace changed')
        service = policy['service']
        if properties.get('native_service_identity') != service:
            raise ValueError('Agent native storage scope changed')
        for key in spec.storage_env:
            expected, actual = service['scope'].get(key), env.get(key)
            if expected is None:
                if actual:
                    raise ValueError('Conversation storage does not match the agent')
            elif not actual or str(Path(actual).expanduser().resolve()) != expected:
                raise ValueError('Conversation storage does not match the agent')
        return AgentActor(root, directory, identity, properties['name'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f'Agent identity could not be verified: {error}') from None
