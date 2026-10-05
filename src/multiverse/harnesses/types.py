"""Adapter boundary: specifications and observations, never tmux or state writes."""
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Generator, Literal, Mapping, Protocol
from uuid import UUID


class BridgeUnavailable(ValueError):
    """Preparation failed before a native child or conversation was started."""


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
    """`unix-lines` is a local inbox that reads JSON lines and sends no reply."""
    transport: Literal['unix-websocket', 'unix-json', 'unix-lines']
    address: str


@dataclass(frozen=True)
class ServiceIdentity:
    """Provider-selected account/storage scope, independent of a socket name."""
    provider: str
    scope: Mapping[str, str]


@dataclass(frozen=True)
class NativeServiceHandle:
    """An existing externally owned service. Core has no stop/restart authority."""
    identity: ServiceIdentity
    endpoint: NativeEndpoint
    lifetime: Literal['external'] = 'external'

    @classmethod
    def from_record(cls, value):
        return cls(ServiceIdentity(**value['identity']), NativeEndpoint(**value['endpoint']),
                   value.get('lifetime', 'external')) if value else None


@dataclass(frozen=True)
class AttachmentSpec:
    """Terminal policy; a relay alone does not declare independent native work.

    Binding readiness means the adapter validated native identity, workspace and
    effective permissions before its launch protocol reports a binding.
    """
    work_owner: Literal['terminal', 'service', 'unknown'] = 'terminal'
    readiness: Literal['process', 'binding', 'legacy-binding'] = 'process'
    recovery: Literal['unavailable', 'idle-existing-service'] = 'unavailable'
    observe_status: bool = False
    mouse: bool = False


@dataclass(frozen=True)
class NativeActivity:
    """Normalized exact-session safety inventory. Unknown fields fail closed."""
    native_id: str
    workspace: str
    service: NativeServiceHandle
    status: Literal['idle', 'working', 'waiting', 'unknown'] = 'unknown'
    active_work: bool | None = None
    waiting: bool | None = None
    queued_input: bool | None = None
    background_work: bool | None = None


@dataclass(frozen=True)
class SharedServiceSpec:
    """Idempotent native startup command returning a JSON Unix socket address.

    Core may start/connect, but never owns, stops or restarts this service.
    """
    argv: tuple[str, ...]
    socket_field: str
    version_fields: tuple[str, str]
    environment: Mapping[str, str] = field(default_factory=dict)
    required_cli_flags: tuple[str, ...] = ()
    cwd: str | None = None
    require_matching_versions: bool = True
    identity: ServiceIdentity | None = None
    lifetime: Literal['external'] = 'external'


@dataclass(frozen=True)
class RpcProxySpec:
    """Launch-local relay; adapter settings describe pure protocol transforms.

    binds_session promises incoming() returns an ID only after validating the
    exact native binding, workspace and effective requested permissions.
    """
    socket_path: str
    upstream_path: str
    settings: Mapping[str, Any]
    binds_session: bool = False


@dataclass(frozen=True)
class CommandSpec:
    """`environment` is set for the launched process; core identity variables win."""
    argv: tuple[str, ...]
    unset_env: tuple[str, ...] = ('CODEX_THREAD_ID', 'CLAUDECODE')
    control_endpoint: NativeEndpoint | None = None
    shared_service: SharedServiceSpec | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    rpc_proxy: 'RpcProxySpec | None' = None
    native_mode: Mapping[str, Any] | None = None
    fallback: 'CommandSpec | None' = None
    attachment: AttachmentSpec | None = None


@dataclass(frozen=True)
class RpcNotification:
    method: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class RpcRequest:
    """One request, with provider notifications after any successful response."""
    method: str
    params: Mapping[str, Any]
    on_success: tuple[RpcNotification, ...] = ()


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
    resume_prompt: str | None = None


@dataclass(frozen=True)
class NativeResult:
    state: Literal['ok', 'unavailable', 'error', 'native_queued', 'pending', 'unknown']
    reason: str = ''
    value: Any = None

    def to_dict(self):
        return asdict(self)


DeliveryMode = Literal['steer', 'follow-up']
# session_name describes naming during spawn/resume, not a live rename operation.
CAPABILITIES = ('spawn', 'resume', 'wake', 'session_name', 'stop', 'send_steer', 'send_follow_up', 'ask', 'get_usage', 'get_delivery',
                'compaction')


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
    status: str = 'unknown'
    native_mode: Mapping[str, Any] | None = None
    native_versions: Mapping[str, str] | None = None
    native_relay: Mapping[str, Any] | None = None
    service: NativeServiceHandle | None = None
    attachment: AttachmentSpec | None = None

    @classmethod
    def from_records(cls, properties, runtime, directory):
        endpoint = runtime.get('control_endpoint')
        return cls(properties['id'], properties['harness'], properties['workspace'],
                   runtime.get('launch_id'), properties.get('native_session_id'),
                   str(directory / 'session.jsonl'), runtime.get('state', 'unknown'),
                   NativeEndpoint(**endpoint) if endpoint else None,
                   runtime.get('native_bridge'), runtime.get('status', 'unknown'), runtime.get('native_mode'),
                   runtime.get('native_versions'), runtime.get('native_relay'),
                   NativeServiceHandle.from_record(runtime.get('native_service')),
                   AttachmentSpec(**runtime['attachment']) if runtime.get('attachment') else None)

@dataclass(frozen=True)
class Message:
    """`framed` is recipient text; `id` is private receipt correlation metadata."""
    id: str
    text: str
    sender: Mapping[str, Any] = field(default_factory=dict)

    delivery: DeliveryMode = 'steer'
    framed: str = ''


class Executor(Protocol):
    """Core owns execution, deadlines and terminal ownership checks."""
    def request(self, session: SessionHandle, payload: dict, timeout: float = 10) -> NativeResult: ...
    def check_session(self) -> None: ...
    def command(self, specification: CommandSpec, timeout: float = 10): ...
    def rpc(self, specification: RpcSpec, timeout: float = 10) -> NativeResult: ...
    def write_lines(self, session: SessionHandle, lines: list[str], timeout: float = 10) -> NativeResult: ...


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


class AttachmentProvider(Protocol):
    """Optional adapter extension; the original HarnessAdapter stays sufficient.

    Plans and retained observations only: no tmux or runtime mutations. Core
    executes recovery plans against the saved endpoint without service startup.
    """
    def attachment_recovery(self, session: SessionHandle) -> RpcSpec | None: ...
    def attachment_ready(self, session: SessionHandle) -> bool: ...
    def retained_usage(self, session: SessionHandle, runtime: Mapping[str, Any]) -> Mapping[str, Any] | None: ...
