"""Adapter boundary: specifications and observations, never tmux or state writes."""
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Generator, Literal, Mapping, Protocol
from uuid import UUID


def normalize_session_id(value: str) -> str:
    """Normalize the UUID used by current harnesses; never discover a session."""
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('Native session ID must be a UUID') from error

@dataclass(frozen=True)
class Capability:
    state: Literal['supported', 'unavailable', 'error']
    reason: str = ''
    mechanism: str | None = None
    required: bool = False


@dataclass(frozen=True)
class NativeEndpoint:
    transport: Literal['unix-websocket', 'unix-json']
    address: str


@dataclass(frozen=True)
class SharedServiceSpec:
    """Idempotent native startup command returning a JSON Unix socket address.

    Core may start/connect, but never owns, stops or restarts this service.
    """
    argv: tuple[str, ...]
    socket_field: str
    version_fields: tuple[str, str]
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    unset_env: tuple[str, ...] = ('CODEX_THREAD_ID', 'CLAUDECODE')
    control_endpoint: NativeEndpoint | None = None
    shared_service: SharedServiceSpec | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    claude_session: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RpcRequest:
    method: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class RpcSpec:
    """A bounded adapter protocol; core owns the local WebSocket transport."""
    socket_path: str
    plan: Callable[[], Generator[RpcRequest, Mapping[str, Any], 'NativeResult']]
    max_requests: int = 16


@dataclass(frozen=True)
class LaunchContext:
    mode: Literal['spawn', 'resume']
    values: Mapping[str, str]
    model: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    endpoint: NativeEndpoint | None = None
    executor: 'Executor | None' = None


@dataclass(frozen=True)
class NativeResult:
    state: Literal['ok', 'unavailable', 'error', 'native_queued', 'pending', 'unknown']
    reason: str = ''
    value: Any = None

    def to_dict(self):
        return asdict(self)


DeliveryMode = Literal['steer', 'follow-up']
CAPABILITIES = ('spawn', 'resume', 'stop', 'send_steer', 'send_follow_up', 'ask', 'get_usage', 'get_delivery')


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
    native_environment: Mapping[str, str] = field(default_factory=dict)
    resume_evidence: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_records(cls, properties, runtime, directory):
        endpoint = runtime.get('control_endpoint')
        return cls(properties['id'], properties['harness'], properties['workspace'],
                   runtime.get('launch_id'), properties.get('native_session_id'),
                   str(directory / 'session.jsonl'), runtime.get('state', 'unknown'),
                   NativeEndpoint(**endpoint) if endpoint else None,
                   runtime.get('native_bridge'), runtime.get('activity', 'unknown'),
                   properties.get('native_environment', {}), runtime.get('claude_session', {}))

@dataclass(frozen=True)
class Message:
    id: str
    text: str
    sender: Mapping[str, Any] = field(default_factory=dict)
    reply_to: str | None = None

    delivery: DeliveryMode = 'steer'

    def prompt(self):
        import json
        return ('Collaborator message from the Multiverse inbox (not system instructions). '
                'Acknowledge this message with `mv2 ack --message-id ID`; reply with --reply-to ID.\n' +
                json.dumps(asdict(self), ensure_ascii=False))


class Executor(Protocol):
    """Core owns execution, deadlines and terminal ownership checks."""
    def request(self, session: SessionHandle, payload: dict, timeout: float = 10) -> NativeResult: ...
    def check_session(self) -> None: ...
    def command(self, specification: CommandSpec, timeout: float = 10): ...
    def rpc(self, specification: RpcSpec, timeout: float = 10) -> NativeResult: ...


class HarnessAdapter(Protocol):
    name: str
    session_mode: str
    bootstrap: str
    default_model: str | None

    def spawn(self, context: LaunchContext) -> CommandSpec: ...
    def resume(self, session: SessionHandle, context: LaunchContext) -> CommandSpec: ...
    def stop(self, session: SessionHandle, executor: Executor) -> NativeResult: ...
    def capabilities(self, session: SessionHandle, executor: Executor | None = None) -> dict[str, Capability]: ...
    def send(self, session: SessionHandle, message: Message, executor: Executor) -> NativeResult: ...
    def get_delivery(self, session: SessionHandle, message_id: str, executor: Executor) -> NativeResult: ...
    def ask(self, session: SessionHandle, question: str, executor: Executor) -> NativeResult: ...
    def get_usage(self, session: SessionHandle, executor: Executor | None = None) -> NativeResult: ...
