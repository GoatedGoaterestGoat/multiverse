"""Explicit agent authority for tools in an adapter's shared conversation.

This is local identity validation, not a security boundary against another
process running as the same OS user. It never grants attachment control or
substitutes a current launch ID into an old caller's environment.
"""
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import subprocess

from ..harnesses.types import AgentIdentitySpec, SessionHandle, normalize_session_id
from ..store import read


# Set only in an agent's launch, and kept by what it runs: any one, stale or partial included, means an agent.
AGENT_MARKERS = ('MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID', 'MV2_IDENTITY_MODE', 'MV2_CONTROL_SOCKET')
SCAN_SECONDS = 1.0  # what the pane scan may spend retrying records caught mid-replacement


def runs_in_agent(environment=None, root=None):
    """Whether this process runs for an agent, by what its launch leaves behind: one of mv2's launch markers, or,
    given `root`, a tmux pane (on its own socket) that an agent of root's universe tree records as its own, on the
    very server that created it. A person's terminal, a sibling pane on the same tmux server, the plugin host and a
    pane that only reuses a stopped agent's pane ID on a restarted server have none of these.

    A guard against mistakes, as the rest of this module: it reads no process table, argv or other environment, and
    asks a tmux server only read-only, never repairing a socket or signalling anything."""
    environment = os.environ if environment is None else environment
    if any(environment.get(key) for key in AGENT_MARKERS):
        return True
    tmux, pane = environment.get('TMUX'), environment.get('TMUX_PANE')
    # Only a literal pane ID is ever matched or put in the query below, which bypasses Tmux.command's checks.
    if not (tmux and isinstance(pane, str) and re.fullmatch(r'%[0-9]+', pane, re.ASCII) and root is not None):
        return False
    try:
        # TMUX is SOCKET,PID,SESSION: split from the right, as the socket's path may hold a comma itself.
        parts = tmux.rsplit(',', 2)
        socket, top = Path(parts[0]).resolve(), Path(root).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    pid = parts[1] if len(parts) == 3 else ''
    server_pid = int(pid) if pid.isascii() and pid.isdecimal() and len(pid) <= 10 else None
    answered = []  # what the server says of this pane, asked at most once
    import time
    from .. import universe as universes
    from ..store import retry_missing
    while universes.parent(top) is not None:
        top = universes.parent(top)
    deadline = time.monotonic() + SCAN_SECONDS  # one budget for the scan's retries, however many agents
    unsure = False  # a record that read missing once its retries were spent: it may be this pane's agent's
    for scope in universes.tree(top):
        try:
            directories = list((scope / 'state' / 'agents').iterdir())
        except OSError:
            continue
        for directory in directories:
            path = directory / 'runtime.json'
            try:
                # A record caught being replaced reads as missing for a moment: retried, never taken as no agent.
                record = retry_missing(lambda: read(path), deadline=deadline, path=path)
                recorded = record.get('tmux') or {}
            except FileNotFoundError:
                # Missing once the retries ran out of time proves nothing: it counts as an agent's. Judged now, not
                # before the read, since the deadline can pass during it.
                unsure = unsure or time.monotonic() >= deadline
                continue
            except (OSError, ValueError, AttributeError):
                continue
            try:
                mine = (isinstance(recorded, dict) and recorded.get('pane') == pane
                        and isinstance(recorded.get('socket'), str) and Path(recorded['socket']).resolve() == socket)
            except (OSError, ValueError, RuntimeError):
                continue
            # Pane IDs start again at %0 when the server restarts, and a stopped agent keeps its record: the pane is
            # its own only on the server that created it (the PID and start time it recorded), and only while it
            # carries this agent's, universe's and launch's tags, as start times are whole seconds. A record without
            # them, or a server that doesn't answer with them, proves nothing, so the pane stays a person's.
            if mine and _same_pane(record, directory, scope, server_pid, socket, pane, answered, deadline):
                return True  # only ever a refusal to treat it as the person: never a sender, an identity or authority
    return unsure


def _same_pane(record, directory, scope, server_pid, socket, pane, answered, deadline):
    identity, launch = record['tmux'].get('server_identity'), record.get('launch_id')
    if (not isinstance(identity, dict) or any(type(identity.get(key)) is not int or identity[key] <= 0
                                              for key in ('pid', 'epoch'))
            or server_pid != identity['pid'] or not isinstance(launch, str) or not launch):
        return False
    from ..store import retry_missing

    def replaced(path):  # an atomically replaced record reads as missing for a moment: retried, within the budget
        return retry_missing(lambda: read(path), deadline=deadline, path=path)
    try:
        # The tags lifecycle put on the pane are the agent's stored identity, not its folder's name, which a
        # recovered folder needn't share; and that agent must belong to the universe it sits in.
        properties, universe = replaced(directory / 'properties.json'), replaced(scope / 'universe.json').get('id')
        agent, owner = properties.get('id'), properties.get('universe_id')
    except (OSError, ValueError, AttributeError):
        return False  # missing or damaged for good: no proof, so no match
    if not (isinstance(agent, str) and agent and isinstance(universe, str) and universe and owner == universe):
        return False
    if not answered:
        answered.append(_pane(socket, pane))
    expected = (identity['pid'], identity['epoch'], pane, agent, universe, launch)
    return answered[0] is not None and answered[0] == expected


def _pane(socket, pane):
    """What the tmux server at `socket` says of `pane`: (pid, start time, pane, agent, universe, launch), or None.
    Read-only: one plain query, never the backend's recovery path, so classifying a caller can't repair a socket
    or signal a process."""
    from ..terminal.tmux import IDENTITY_FORMAT
    try:
        reply = subprocess.run(['tmux', '-S', str(socket), 'display-message', '-p', '-t', pane, IDENTITY_FORMAT],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=2)
        # Decoded here, so output that isn't text is an unproven answer rather than an error for the caller.
        fields = reply.stdout.decode().rstrip('\n').split('\t')
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    if (reply.returncode or len(fields) != 7
            or not all(field.isascii() and field.isdecimal() and len(field) <= 20 for field in fields[:2])):
        return None
    return int(fields[0]), int(fields[1]), fields[3], fields[4], fields[5], fields[6]


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
                                          'MV2_CLI', 'MV2_PYTHON', 'MV2_SOURCE', 'PATH')}
    # Request the launching installation's tools each time. A shared native
    # conversation may retain its previous environment after a frontend restart;
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


def sender_actor(environment=None, *, acting="send"):
    """Stable conversation authority, or the exact current legacy launch.

    A launch-strict sender cannot fall through to an operator merely because it
    predates saved identity policies. No process observation or adoption occurs.
    `acting` names what the caller does as this agent, for the refusals.
    """
    env = os.environ if environment is None else environment
    caller = actor(env, data=True)
    if caller is not None:
        return caller
    if not any(env.get(key) for key in ('MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID')):
        return None
    if any(not isinstance(env.get(key), str) or not env[key]
           for key in ('MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID')):
        raise ValueError(f'Calling agent identity is incomplete; cannot {acting} as this agent')
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
        raise ValueError(f'Outdated launch; refusing to {acting} as this agent')
    return AgentActor(root, directory, identity, properties['name'])


def calling_socket(environment=None):
    """Resolve the current terminal server without trusting cached native values."""
    return calling_paths(environment)[0]


def calling_paths(environment=None, *, require_recorded_run=False, acting="select the calling agent's tmux server"):
    """Resolve terminal and run directory from the same verified current runtime."""
    from . import paths
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
        return env.get('MV2_TMUX_SOCKET'), paths.run_directory(env)
    from ..universe import records
    path = directory / 'runtime.json'
    try:
        facts = records.runtime(path)
        if not facts:
            raise records.RecordError(path, 'is missing or has no launch record')
    except records.RecordError as error:
        raise ValueError(f"Calling agent's {path}: {error.reason}; restore its runtime record before spawning a child") from None
    if caller is None and facts.get('launch_id') != env['MV2_LAUNCH_ID']:
        raise ValueError(f'Outdated launch; cannot {acting}')
    terminal = facts.get('tmux')
    socket = terminal.get('socket') if isinstance(terminal, dict) else None
    if not isinstance(socket, str) or not socket or '\0' in socket:
        raise ValueError('Calling agent has no recorded tmux server')
    if require_recorded_run and 'run_directory' not in facts:
        raise ValueError('Calling agent has no recorded run directory')
    run_root = (paths.recorded_directory(facts['run_directory']) if 'run_directory' in facts else
                paths.run_directory(env))
    return socket, run_root


def _recorded_launch(env, root, directory, properties, spec):
    """An opted-in bridge's own retained request can explain a missing mode marker."""
    from .. import harnesses
    from ..universe import records
    from .native import adapter_hook

    launch_id = normalize_session_id(env.get('MV2_LAUNCH_ID'))
    if launch_id != env.get('MV2_LAUNCH_ID'):
        raise ValueError('Recorded launch identity requires its full canonical ID')
    adapter = harnesses.load(root, properties['harness'])
    allow = adapter_hook(adapter, 'allow_recorded_launch')
    policy = adapter_hook(adapter, 'agent_identity_policy')
    session = SessionHandle.from_records(properties, records.runtime(directory / 'runtime.json'), directory)
    if allow is None or allow(session) is not True or policy is None or policy() != spec:
        raise ValueError('Outdated launch; this adapter does not authorize its recorded conversation identity')
    path = directory / 'launches' / launch_id / 'request.json'
    request = records._object(path)
    saved = request.get('env')
    keys = ('MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID')
    if not isinstance(saved, dict) or any(not env.get(key) or saved.get(key) != env[key] for key in keys):
        raise ValueError(f'{path}: recorded launch does not identify this universe and agent')
    mode, relay = request.get('native_mode'), request.get('rpc_proxy')
    if (request.get('shared_service') is not True or not isinstance(mode, dict) or mode.get('mode') != 'bridge'
            or not isinstance(relay, dict) or relay.get('binds_session') is not True):
        raise ValueError(f'{path}: recorded launch is not a session-binding shared bridge')


def actor(environment=None, *, data=False):
    """Validate stable proof; only data routes can also prove a retained bridge launch.

    An unscoped human CLI remains an operator. A declared but incomplete agent
    context can never fall through to that case.
    """
    env = os.environ if environment is None else environment
    mode = env.get('MV2_IDENTITY_MODE')
    if mode is None and env.get('MV2_LAUNCH_ID') and not data:
        return None  # Runner/control and child-spawn routes keep their generation guards.
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
            from ..universe import records
            properties = records.properties(directory / 'properties.json', harness_optional=True)
            policy = properties.get('agent_identity')
    except (OSError, ValueError, KeyError):
        if mode is not None or unfenced:
            raise ValueError('Agent identity could not be verified; inspect its saved records') from None
        return None  # Non-adopted launches keep their existing generation guards.
    if mode is None and policy is None and not unfenced:
        return None
    try:
        if (mode is None and env.get('MV2_LAUNCH_ID')
                and records.runtime(directory / 'runtime.json').get('launch_id') == env['MV2_LAUNCH_ID']):
            return None  # The existing exact-current-launch path needs no compatibility authority.
        if not isinstance(policy, dict) or policy.get('schema') != 1:
            raise ValueError('No saved agent identity policy')
        spec = specification(AgentIdentitySpec(**{**policy['spec'],
            'storage_env': tuple(policy['spec'].get('storage_env', ()))}))
        if mode is None and data and env.get('MV2_LAUNCH_ID'):
            _recorded_launch(env, root, directory, properties, spec)
        elif mode != spec.mode:
            raise ValueError('Missing or incorrect agent identity mode')
        if not env.get('MV2_AGENT_DIR') or Path(env['MV2_AGENT_DIR']).resolve() != directory.resolve():
            raise ValueError('Agent directory does not match the declared identity')
        universe = read(root / 'universe.json')
        if properties['id'] != identity or properties.get('universe_id') != universe['id']:
            raise ValueError('Agent or universe identity changed')
        from .navigation import require_settled
        require_settled(records.runtime(directory / 'runtime.json'))
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
