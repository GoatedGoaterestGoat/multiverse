"""Adapter boundary: specifications and observations, never tmux or state writes."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Generator, Literal, Mapping, Protocol, Sequence
from uuid import UUID


Effect = Literal['observe', 'spend', 'act']
# Effects belong to the protocol, not an adapter's implementation. Core checks
# them before dispatch; launch/send also check before publishing any records.
EFFECTS: Mapping[str, Effect] = {
    **dict.fromkeys((
        'capabilities', 'get_usage', 'get_delivery', 'observed_binding',
        'session_home', 'session_storage_evidence', 'resume_notice',
        'agent_identity_policy', 'agent_identity_problem', 'allow_recorded_launch',
        'attachment_ready', 'attachment_recovery', 'retained_usage', 'navigation_target',
        'connection_problem', 'status_watch', 'supports_launch_refusal',
        'launch_started', 'launch_refusal', 'startup_retryable',
        'startup_prompt_evidence', 'startup_prompt_evidence_reason',
        'accepted_message_evidence', 'message_id_evidence',
        'transcript_view', 'transcript_bodies', 'transcript_readable',
        'models', 'model_names', 'check_model', 'run_options', 'display_pane', 'slash_commands', 'slash_result',
        'waiting_prompts', 'prompt_outcome',
    ), 'observe'),
    'ask': 'spend',
    'run_plan': 'spend',
    **dict.fromkeys(('spawn', 'resume', 'stop', 'send', 'continue_queued', 'launch_proxy', 'run_slash', 'answer_prompt'), 'act'),
}


class BridgeUnavailable(ValueError):
    """Preparation failed before a native child or conversation was started."""


class LocalProtocolError(Exception):
    """Reject one correlated relay frame without changing launch health.

    The connection view supplies a previously correlated request ID. None drops
    the frame. Client replies reject client requests or native responses;
    upstream replies reject native server requests. No primary callbacks run.
    """
    def __init__(self, message: str, *, request_id=None, destination='client', code=-32602):
        if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            raise ValueError('Local protocol request ID must be a string or integer')
        if destination not in ('client', 'upstream'):
            raise ValueError('Local protocol reply destination must be client or upstream')
        if isinstance(code, bool) or not isinstance(code, int) or not -(2 ** 31) <= code < 2 ** 31:
            raise ValueError('Local protocol error code must be a signed 32-bit integer')
        if not isinstance(message, str):
            raise ValueError('Local protocol error message must be a string')
        super().__init__(message)
        self.request_id, self.destination, self.code = request_id, destination, code


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
class AgentIdentitySpec:
    """Adapter-declared authority of tools in one agent's shared conversation.

    This does not authorize runner callbacks or attachment control. The native
    marker and storage names are provider-owned; core validates persisted scope.
    """
    mode: str
    native_id_env: str
    storage_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class StartupRetrySpec:
    """One replacement argv after a proved, exited pre-start native refusal.

    Core retains every other selected command field. The adapter's
    startup_retryable snapshot predicate must identify the exact native error;
    only core can authorize a retry and publish these conditional notices.
    """
    argv: tuple[str, ...]
    notices: tuple[str, ...] = ()


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
    # What the plugins on in the agent's universe added to this launch, as the adapter applied them
    # (multiverse.plugins.harness.LaunchPlan.record()); lifecycle stores it as runtime.json `plugins`.
    plugins: Mapping[str, Any] | None = None
    agent_identity: AgentIdentitySpec | None = None
    # Presentation for this selected candidate, including a late plain fallback.
    notices: tuple[str, ...] = ()
    startup_retry: StartupRetrySpec | None = None


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
    max_response_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class LaunchContext:
    mode: Literal['spawn', 'resume']
    values: Mapping[str, str]
    model: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    endpoint: NativeEndpoint | None = None
    executor: 'Executor | None' = None
    resume_prompt: str | None = None
    agent_environment: Mapping[str, str] = field(default_factory=dict)
    # None preserves the complete selection used by existing callers; an empty
    # tuple proves no storage keys. Omitted legacy keys retain native inheritance.
    storage_keys: tuple[str, ...] | None = None
    effort: str | None = None


@dataclass(frozen=True)
class NativeResult:
    state: Literal['ok', 'unavailable', 'error', 'native_queued', 'pending', 'unknown']
    reason: str = ''
    value: Any = None

    def to_dict(self):
        return asdict(self)


DeliveryMode = Literal['steer', 'follow-up']
# session_name describes naming during spawn/resume, not a live rename operation.
CAPABILITIES = ('spawn', 'resume', 'wake', 'session_name', 'stop', 'send_steer', 'send_follow_up', 'ask', 'get_usage', 'get_delivery', 'slash', 'answer')


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
    home: str | None = None
    pid: int | None = None
    pid_started: str | None = None
    # Core's exact-conversation pause/restart context; no native ownership grant.
    queue_state: Mapping[str, Any] | None = None

    @classmethod
    def from_records(cls, properties, runtime, directory):
        endpoint = runtime.get('control_endpoint')
        queue = runtime.get('queue_state')
        if not isinstance(queue, dict) or queue.get('native_session_id') != properties.get('native_session_id'):
            queue = None
        elif queue:
            queue = {key: value for key, value in queue.items() if key != 'restart'}
        return cls(properties['id'], properties['harness'], properties['workspace'],
                   runtime.get('launch_id'), properties.get('native_session_id'),
                   str(directory / 'session.jsonl'), runtime.get('state', 'unknown'),
                   NativeEndpoint(**endpoint) if endpoint else None,
                   runtime.get('native_bridge'), runtime.get('status', 'unknown'), runtime.get('native_mode'),
                   runtime.get('native_versions'), runtime.get('native_relay'),
                   NativeServiceHandle.from_record(runtime.get('native_service')),
                   AttachmentSpec(**runtime['attachment']) if runtime.get('attachment') else None,
                   properties.get('native_session_home'), runtime.get('pid'), runtime.get('pid_started'), queue)


@dataclass(frozen=True, kw_only=True)
class ObservedBinding:
    """Adapter-verified identity of the exact launch process, never history inference."""
    native_id: str
    home: str
    evidence: str


class BindingObserver(Protocol):
    def observed_binding(self, session: SessionHandle) -> ObservedBinding | None: ...

@dataclass(frozen=True)
class Message:
    """`framed` is recipient text; `id` is private receipt correlation metadata.

    With delivery_attempt_ids, it names this submission, not the logical inbox message.
    """
    id: str
    text: str
    sender: Mapping[str, Any] = field(default_factory=dict)

    delivery: DeliveryMode = 'steer'
    framed: str = ''
    sent_at: str | None = None  # Original aware ISO send time; never the retry or receipt time.


class Executor(Protocol):
    """Core owns execution, deadlines and terminal ownership checks."""
    def request(self, session: SessionHandle, payload: dict, timeout: float = 10) -> NativeResult: ...
    def run_once(self, adapter, session: SessionHandle, prepare: Callable[[], 'RunRequest'],
                 *, timeout: float = 600) -> 'RunReply': ...
    def check_session(self) -> None: ...
    def command(self, specification: CommandSpec, timeout: float = 10): ...
    def rpc(self, specification: RpcSpec, timeout: float = 10) -> NativeResult: ...
    def write_lines(self, session: SessionHandle, lines: list[str], timeout: float = 10) -> NativeResult: ...
    def write_prompt_answer(self, session: SessionHandle, answer: 'PromptAnswer') -> NativeResult: ...


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


@dataclass(frozen=True, kw_only=True)
class SlashCommand:
    name: str
    description: str = ''
    source: str = 'builtin'
    plugin: str | None = None
    needs_args: bool = False
    refused: str | None = None
    changes_settings: bool = False  # also saves the person's defaults; only an operator may run it


@dataclass(frozen=True, kw_only=True)
class SlashRequest:
    id: str
    command: str
    args: str = ''
    wait_until: float = 0.0


@dataclass(frozen=True, kw_only=True)
class CommandResult:
    id: str
    command: str
    args: str
    state: str
    output: str | None = None
    prompted: bool = False
    changed: tuple[str, ...] = ()
    reason: str | None = None
    saved_default: bool = False


class SlashProvider(Protocol):
    """Live command discovery, one submission, and read-only correlated receipts."""
    def slash_commands(self, session: SessionHandle, executor: Executor) -> NativeResult: ...
    def run_slash(self, session: SessionHandle, request: SlashRequest, executor: Executor) -> NativeResult: ...
    def slash_result(self, session: SessionHandle, request: SlashRequest, executor: Executor) -> NativeResult: ...


@dataclass(frozen=True, kw_only=True)
class PromptChoice:
    id: str
    label: str
    saves_rule: bool = False
    takes_message: bool = False


@dataclass(frozen=True, kw_only=True)
class PromptQuestion:
    id: str
    question: str
    header: str = ''
    options: tuple[tuple[str, str], ...] = ()
    multiple: bool = False
    free_text: bool = True
    secret: bool = False
    required: bool = True
    takes_note: bool = False


@dataclass(frozen=True, kw_only=True)
class WaitingPrompt:
    id: str
    kind: str
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    asked_at: str | None = None
    tool: str | None = None
    choices: tuple[PromptChoice, ...] = ()
    questions: tuple[PromptQuestion, ...] = ()
    answerable: bool = True
    reason: str | None = None
    subagent: str | None = None
    requires_waiting: bool = True  # Only retained questions may be answerable outside waiting.


@dataclass(frozen=True, kw_only=True)
class PromptList:
    state: str
    coverage: str = 'unknown'
    reason: str | None = None
    prompts: tuple[WaitingPrompt, ...] = ()


@dataclass(frozen=True, kw_only=True)
class PromptAnswer:
    id: str
    choice: str | None = None
    message: str | None = None
    answers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    allow_rule: bool = False  # Set by core only for a person; checked again at the native boundary.
    notes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptInput:
    """Transient native input recipe; core reserves and submits it through ordinary delivery.

    id must be 'prompt-' + PromptAnswer.id. Text is never an audit or receipt field.
    """
    id: str
    text: str


@dataclass(frozen=True, kw_only=True)
class PromptResult:
    id: str
    state: str
    reason: str | None = None
    saved_rule: bool = False
    person_answer: Mapping[str, Any] | None = None
    cause: str | None = None


class PromptProvider(Protocol):
    """Native prompts and retained questions, one mv2 submission, and read-only outcomes.

    Core reads input receipts through get_delivery; callback prompt_outcome remains read-only.
    """
    def waiting_prompts(self, session: SessionHandle, executor: Executor) -> NativeResult: ...
    def answer_prompt(self, session: SessionHandle, answer: PromptAnswer, executor: Executor) -> NativeResult: ...
    def prompt_outcome(self, session: SessionHandle, answer: PromptAnswer, executor: Executor) -> NativeResult: ...


class AttachmentProvider(Protocol):
    """Optional adapter extension; the original HarnessAdapter stays sufficient.

    Plans and retained observations only: no tmux or runtime mutations. Core
    executes recovery plans against the saved endpoint without service startup.
    """
    def attachment_recovery(self, session: SessionHandle) -> RpcSpec | None: ...
    def attachment_ready(self, session: SessionHandle) -> bool: ...
    def retained_usage(self, session: SessionHandle, runtime: Mapping[str, Any]) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class Usage:
    """Measured cumulative usage of one native session.

    Input includes cached input; cache counts are subsets, never added twice.
    Unknown counters are None. An observed zero is distinct from unavailable.
    """
    native_session_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    source: str
    reported_at: str
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    complete: bool = True


@dataclass(frozen=True, kw_only=True)
class TranscriptSource:
    state: Literal['supported', 'unavailable', 'error']
    home: str | None = None
    reason: str = ''
    format: str | None = None
    version: str | None = None
    size: int | None = None
    updated_at: str | None = None
    stable_rows: bool = True


@dataclass(frozen=True, kw_only=True)
class RowFact:
    """Provider metadata only; native row bodies never enter the core index."""
    position: str
    ref: str | None
    kind: str
    label: str
    source: str | None = None
    call_id: str | None = None
    command: str | None = None
    args: str | None = None
    stop: str | None = None
    ends_turn: bool = False
    at: str | None = None
    size: int = 0
    preview: str = ''


@dataclass(frozen=True, kw_only=True)
class TranscriptView:
    """One native generation at one tip, with history indexes in conversation order."""
    source: TranscriptSource
    rows: tuple[RowFact, ...]
    history: tuple[int, ...]
    context_from: int
    tip: str | None
    gap: str | None
    history_version: int
    # When the history starts at the harness's own compaction summary because what came before was never stored:
    # that summary row's ref, then the only record of what came before. None otherwise.
    summary: str | None = None


class TranscriptRowsChanged(ValueError):
    """Selected bytes no longer match the metadata view; never carry row content."""
    def __init__(self):
        super().__init__('Transcript rows changed after the metadata snapshot')


class TranscriptReader(Protocol):
    deadline: float
    def check(self) -> None: ...


class TranscriptProvider(Protocol):
    def transcript_view(self, session: SessionHandle, *, tip: str | None = None,
                        reader: TranscriptReader | None = None) -> TranscriptView: ...
    def transcript_bodies(self, session: SessionHandle, view: TranscriptView,
                          indexes: Sequence[int], *, max_bytes: int,
                          reader: TranscriptReader | None = None) -> list[dict | None]: ...
    def transcript_readable(self, session: SessionHandle, *, reader: TranscriptReader | None = None) -> bool | None: ...


@dataclass(frozen=True, kw_only=True)
class ModelInfo:
    """A model a harness takes, for one provider (api.Model is copied from it field by field)."""
    id: str                          # the ID the harness sends for this provider
    name: str                        # display name
    family: str | None = None        # "opus", "sonnet", "haiku", "fable"
    aliases: tuple = ()              # names that resolve to it for this provider ("sonnet")
    efforts: tuple = ()              # the efforts it takes, low to high; () when it takes none
    default_effort: str | None = None
    inputs: tuple = ('text',)        # "text", plus "image" where it takes images
    context_window: int | None = None
    long_context_window: int | None = None   # with the "[1m]" suffix, where the model supports it
    max_output_tokens: int | None = None
    default: bool = False            # the model the harness uses when none is given, where known
    hidden: bool = False


@dataclass(frozen=True, kw_only=True)
class ModelCatalog:
    models: tuple                    # ModelInfo; only those offered on `provider`
    free_form: bool                  # the harness also takes model IDs it doesn't list
    source: str                      # "table" (kept by mv2) or "native" (asked of the harness)
    version: str | None = None       # "table": the harness version it was taken from
    provider: str | None = None      # Claude: "first-party", "bedrock", "vertex", "foundry", "mantle", ...


@dataclass(frozen=True, kw_only=True)
class ModelCheck:
    ok: bool
    model: str | None = None         # the resolved ID, when it resolves
    effort: str | None = None        # the effort a run would use: the given one when the model takes it, the
                                     # model's default_effort when none was given, None when it takes none
    verified: bool = False           # the model is in the catalog; False when ok only because it is free-form
    reason: str | None = None        # why not ok, or why ok is unverified


class ModelProvider(Protocol):
    """Optional: a harness's models and a model/effort check (observe). `environment` is what a launch would
    get, a variable mapped to None where the launch removes it; the adapter reads only what it needs from it,
    never os.environ."""
    def models(self, *, environment: Mapping[str, str | None]) -> ModelCatalog: ...
    def check_model(self, model: str, *, effort: str | None = None,
                    environment: Mapping[str, str | None]) -> ModelCheck: ...


class ModelNameProvider(Protocol):
    """Optional table labels beyond the catalog's IDs/aliases (observe), never live model observations.

    Include provider-specific spellings when their names are known. The environment selects current table
    aliases, as for ModelProvider; core applies universe display overrides after these names.
    """
    def model_names(self, *, environment: Mapping[str, str | None]) -> Mapping[str, str]: ...


@dataclass(frozen=True, kw_only=True)
class RunText:
    text: str


@dataclass(frozen=True, kw_only=True)
class RunImage:
    media_type: str
    data: bytes


@dataclass(frozen=True, kw_only=True)
class RunMessage:
    role: str
    blocks: tuple[RunText | RunImage, ...]


@dataclass(frozen=True, kw_only=True)
class RunRow:
    format: str
    data: dict


@dataclass(frozen=True, kw_only=True)
class RunRequest:
    messages: tuple[RunRow | RunMessage, ...]
    prompt: str
    system: str
    model: str
    effort: str | None = None
    transport_retries: int | None = None


@dataclass(frozen=True, kw_only=True)
class RunOptions:
    supported: bool
    reason: str = ''
    images: bool = False
    formats: tuple[str, ...] = ()
    transport_retries: tuple[int, int] | None = None


@dataclass(frozen=True, kw_only=True)
class RunDirectory:
    """A core-chosen private cwd and UUID, before anything has been written."""
    path: Path
    id: str


@dataclass(frozen=True, kw_only=True)
class RunFile:
    """An exclusively created relative file, below the run cwd or selected harness home."""
    path: str
    content: bytes
    root: Literal['run', 'store'] = 'run'


@dataclass(frozen=True, kw_only=True)
class RunLink:
    """A link inside a disposable tree; its absolute target is borrowed, never opened by core."""
    path: str
    target: str


class RunChannel(Protocol):
    """Bounded JSON frames to a private stdio child. All dialogue decisions stay in the caller."""
    def send(self, frame: dict) -> None: ...
    def receive(self) -> dict: ...


@dataclass(frozen=True, kw_only=True)
class RunOutcome:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, kw_only=True)
class RunReply:
    text: str
    model: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    stop: str | None = None
    lost: tuple[str, ...] = ()
    duration: float = 0.0


class RunFailure(ValueError):
    """A classified, bounded native diagnostic; never attach the request or response body."""
    def __init__(self, kind, reason, *, retry_after=None, confirmed=None, usage=None, cost_usd=None,
                 model=None, duration=0.0):
        self.kind, self.reason = kind, str(reason)[:4096]
        self.retry_after, self.confirmed = retry_after, confirmed
        self.usage, self.cost_usd, self.model = dict(usage or {}), cost_usd, model
        self.duration = duration
        super().__init__(self.reason)


@dataclass(frozen=True, kw_only=True)
class RunPlan:
    command: CommandSpec
    interpret: Callable[[RunOutcome], RunReply] | None = None
    files: tuple[RunFile, ...] = ()
    dialogue: Callable[[RunChannel], RunReply] | None = None
    disposable: tuple[str, ...] = ()
    links: tuple[RunLink, ...] = ()


class RunProvider(Protocol):
    """Optional one-shot model call: no tools, no saved conversation, no adapter-owned writes or processes."""
    def run_options(self, *, environment: Mapping[str, str | None]) -> RunOptions: ...
    def run_plan(self, request: RunRequest, *, environment: Mapping[str, str | None],
                 run: RunDirectory) -> RunPlan: ...


class PaneDisplay(Protocol):
    """Optional: a live pane's captured text as the dashboard shows it (observe). A pure function of `text`: no
    files, processes or network. It only drops the harness's own chrome (input-box rules, status rows, footer,
    padding) and returns `text` unchanged when it doesn't recognize the screen. Core shows the raw text when the
    result is not an in-order subset of the lines of `text`."""
    def display_pane(self, text: str) -> str: ...
