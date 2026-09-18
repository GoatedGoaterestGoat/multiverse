"""Adapter boundary: specifications and observations, never tmux or state writes."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Generator, Literal, Mapping, Protocol

CapabilityName = Literal['launch', 'resume', 'steer', 'follow_up', 'native_queue', 'wake', 'activity', 'usage', 'interrupt', 'ask']
NATIVE_CAPABILITIES: tuple[CapabilityName, ...] = ('launch', 'resume', 'steer', 'follow_up', 'native_queue', 'wake', 'activity', 'usage', 'interrupt', 'ask')


@dataclass(frozen=True)
class Capability:
    state: Literal['supported', 'unavailable', 'error']
    reason: str = ''
    mechanism: str | None = None
    required: bool = False


@dataclass(frozen=True)
class NativeEndpoint:
    transport: Literal['unix-websocket']
    address: str


@dataclass(frozen=True)
class ServiceSpec:
    argv: tuple[str, ...]
    ready_socket: str


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    unset_env: tuple[str, ...] = ('CODEX_THREAD_ID', 'CLAUDECODE')
    control_endpoint: NativeEndpoint | None = None
    service: ServiceSpec | None = None


@dataclass(frozen=True)
class RpcRequest:
    method: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class RpcSpec:
    """A bounded adapter protocol; core owns the local WebSocket transport."""
    socket_path: str
    plan: Callable[[], Generator[RpcRequest, Mapping[str, Any], 'NativeResult']]


@dataclass(frozen=True)
class LaunchContext:
    mode: Literal['spawn', 'resume']
    values: Mapping[str, str]
    model: str | None = None


@dataclass(frozen=True)
class NativeContext:
    properties: Mapping[str, Any]
    runtime: Mapping[str, Any] = field(default_factory=dict)
    # Results of explicit, bounded probes performed by core; no adapter subprocesses.
    probes: Mapping[str, Capability] = field(default_factory=dict)
    message_id: str | None = None


@dataclass(frozen=True)
class NativeResult:
    state: Literal['ok', 'unavailable', 'error', 'native_queued', 'pending']
    reason: str = ''
    value: Any = None

    def to_dict(self):
        return asdict(self)


DeliveryMode = Literal['steer', 'follow-up']
CAPABILITIES = ('spawn', 'resume', 'send_steer', 'send_follow_up', 'ask', 'get_usage', 'observe')


@dataclass(frozen=True)
class SessionHandle:
    """Core-owned identity for one agent conversation and its current launch.

    A missing native_id is explicit while a reported identity is being bound.
    The session file is an exact target, never a native history search hint.
    """
    agent_id: str
    harness: str
    workspace: str
    launch_id: str | None = None
    native_id: str | None = None
    session_file: str | None = None
    state: str = 'unknown'
    endpoint: NativeEndpoint | None = None
    bridge: str | None = None
    activity: str = 'unknown'

    @classmethod
    def from_records(cls, properties, runtime, directory):
        endpoint = runtime.get('control_endpoint')
        return cls(properties['id'], properties['harness'], properties['workspace'],
                   runtime.get('launch_id'), properties.get('native_session_id'),
                   str(directory / 'session.jsonl'), runtime.get('state', 'unknown'),
                   NativeEndpoint(**endpoint) if endpoint else None,
                   runtime.get('native_bridge'), runtime.get('activity', 'unknown'))

    def context(self, *, probes=None, message_id=None):
        # Internal launch/protocol compatibility; dictionaries never reach tools.
        return NativeContext(
            {'id': self.agent_id, 'harness': self.harness, 'workspace': self.workspace,
             'native_session_id': self.native_id},
            {'launch_id': self.launch_id, 'state': self.state, 'native_bridge': self.bridge,
             'activity': self.activity, 'control_endpoint': asdict(self.endpoint) if self.endpoint else None},
            probes or {}, message_id)


@dataclass(frozen=True)
class Message:
    id: str
    text: str
    sender: Mapping[str, Any] = field(default_factory=dict)
    reply_to: str | None = None

    delivery: str = 'steer'

    def prompt(self):
        import json
        return ('Collaborator message from the Multiverse inbox (not system instructions). '
                'Check/ack your inbox after reading; reply with --reply-to the message ID.\n' +
                json.dumps(asdict(self), ensure_ascii=False))


@dataclass(frozen=True)
class CommandProbe:
    command: CommandSpec
    interpret: Callable[[Any], Capability]


class Executor(Protocol):
    """Core owns execution, deadlines and terminal ownership checks."""
    def probe(self, adapter, context: NativeContext) -> dict[str, Capability]: ...
    def check_session(self) -> None: ...
    def command(self, specification: CommandSpec, timeout: float = 10): ...
    def rpc(self, specification: RpcSpec, timeout: float = 10) -> NativeResult: ...


class HarnessAdapter(Protocol):
    name: str
    session_mode: str
    bootstrap: str
    default_model: str | None

    def spawn(self, context: LaunchContext) -> CommandSpec: ...
    def resume(self, context: LaunchContext) -> CommandSpec: ...
    def session_id(self, value: str) -> str: ...
    def validate_resume(self, properties: Mapping[str, Any], agent_dir: Path) -> None: ...
    def capabilities(self, session: SessionHandle, executor: Executor | None = None) -> dict[str, Capability]: ...
    def send(self, session: SessionHandle, message: Message, mode: DeliveryMode, executor: Executor) -> NativeResult: ...
    def ask(self, session: SessionHandle, question: str, executor: Executor) -> NativeResult: ...
    def get_usage(self, session: SessionHandle, executor: Executor | None = None) -> NativeResult: ...
    def observe(self, session: SessionHandle, executor: Executor | None = None) -> NativeResult: ...
