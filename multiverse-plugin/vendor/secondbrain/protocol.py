"""SecondBrain host <-> connector wire protocol (v1).

This module is the **authoritative, dependency-free** implementation of the wire
format specified in ``docs/INFRA.md`` §10. A Multiverse-side connector author codes
their client against exactly these frame shapes.

Design (per the spec):

* Every WebSocket **text** message is exactly one JSON object -- one *frame*.
* Every frame carries a string ``t`` (type discriminator) and integer ``v``
  (protocol version, currently ``1``).
* Post-``challenge`` frames travel inside an E2E AEAD channel (§6.3); the shapes
  encoded/decoded here are always the *plaintext* JSON. Sealing/opening is the job
  of ``auth.py`` (gated on decision D2) -- the protocol layer is transport-agnostic.

Two equivalent APIs are provided:

* Per-frame :meth:`Frame.to_json` / :meth:`Frame.from_json` (dict <-> dataclass).
* Module-level :func:`encode` (frame -> str) / :func:`decode` (str -> frame) which
  dispatch on the ``t`` discriminator. Unknown / malformed frames raise
  :class:`ProtocolError`.

Round-trip invariant (exercised by ``tests/test_protocol.py``)::

    decode(encode(frame)) == frame

Keep this module **stdlib-only** (``dataclasses``, ``json``, ``enum``). It is imported
by both the host and, conceptually, the reference connector, so it must stay portable.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from enum import Enum, IntEnum, StrEnum
from typing import Any, ClassVar

# --------------------------------------------------------------------------------------
# Versioning & limits
# --------------------------------------------------------------------------------------

#: Wire protocol version carried in every frame's ``v`` field.
PROTOCOL_VERSION = 1

#: Default maximum size (bytes) of a single frame. Over-limit senders must
#: summarize/omit; receivers close with :attr:`CloseCode.FRAME_TOO_BIG` (§4.2).
#: Configurable via :class:`sb_host.config.HostConfig.max_frame_bytes`.
MAX_FRAME_BYTES = 32 * 1024 * 1024  # 32 MiB


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ProtocolError(ValueError):
    """Raised when a byte string cannot be decoded into a known, well-formed frame.

    Covers: not JSON, not a JSON object, missing/unknown ``t`` discriminator, or a
    frame body missing a required field. Callers should map this to a
    :attr:`CloseCode.PROTOCOL_ERROR` (``4400``) close.
    """


# --------------------------------------------------------------------------------------
# Enumerations (string-valued so they serialize transparently to JSON)
# --------------------------------------------------------------------------------------


class FrameType(StrEnum):
    """The ``t`` discriminator for every frame type in the v1 protocol (§10)."""

    # -- control frames (§10.1) --
    HELLO = "hello"
    CHALLENGE = "challenge"
    AUTH = "auth"
    AUTH_OK = "auth_ok"
    RESUME = "resume"
    ACK = "ack"
    LOCK = "lock"
    UNLOCK = "unlock"
    PING = "ping"
    PONG = "pong"
    ERROR = "error"
    BYE = "bye"

    # -- data frames (§10.2 / §10.3 / §10.4) --
    EVENT = "event"
    ACTION = "action"
    ACTION_RESULT = "action_result"
    QUERY = "query"
    QUERY_RESULT = "query_result"

    # -- enrollment sub-flow (§6.2, path /enroll, pre-auth) --
    ENROLL_HELLO = "enroll_hello"
    ENROLL_CHALLENGE = "enroll_challenge"
    ENROLL_PROOF = "enroll_proof"
    ENROLL_OK = "enroll_ok"
    ENROLL_ERROR = "enroll_error"


class EventKind(StrEnum):
    """Perception event kinds (wrapper -> SB), §10.2 table -- kept 1:1 with the spec."""

    WORKSPACE_CONNECTED = "workspace.connected"
    WORKSPACE_DISCONNECTED = "workspace.disconnected"
    AGENT_CREATED = "agent.created"
    AGENT_STATUS_CHANGED = "agent.status_changed"  # to == "waiting" is the key alert
    USER_PROMPT = "user.prompt"
    AGENT_RESPONSE = "agent.response"
    AGENT_MESSAGE = "agent.message"
    AGENT_SPAWN_REQUEST = "agent.spawn_request"


class ActionKind(StrEnum):
    """Agency action kinds (SB -> wrapper), §10.3 table."""

    PROMPT_AGENT = "prompt_agent"
    ANSWER_QUESTION = "answer_question"
    ANSWER_PERMISSION_PROMPT = "answer_permission_prompt"
    MESSAGE_AGENT = "message_agent"
    SPAWN_AGENT = "spawn_agent"
    # Reserved for later lifecycle verbs (§10.3 "(later)"):
    KILL_AGENT = "kill_agent"
    COMPACT_SESSION = "compact_session"
    RESUME_AGENT = "resume_agent"


class QueryKind(StrEnum):
    """Read-only backfill query kinds (SB -> wrapper, req/resp), §10.4 table."""

    LIST_WORKSPACES = "list_workspaces"
    LIST_AGENTS = "list_agents"
    GET_STATUS = "get_status"
    GET_TRANSCRIPT = "get_transcript"


class Authority(StrEnum):
    """The basis SB acted under for an action (§10.3 / DESIGN.md §4c, §9)."""

    AUTONOMOUS = "autonomous"  # SB decided within an `auto` grant
    USER_APPROVED = "user_approved"  # SB proposed, user one-tapped
    USER_DIRECTED = "user_directed"  # user told SB to do it


class ResultStatus(StrEnum):
    """Status of an ``action_result`` / ``query_result`` (§10.3 / §10.4)."""

    OK = "ok"
    ERROR = "error"
    REJECTED = "rejected"


class CloseCode(IntEnum):
    """WebSocket close codes used by the host/connector (§10.5)."""

    NORMAL = 1000  # normal / bye
    FRAME_TOO_BIG = 1009  # frame over max_frame_bytes
    PROTOCOL_ERROR = 4400  # bad frame order / bad frame or signature format
    AUTH_FAILED = 4401  # bad device signature, or client-side bad server-key verify
    FORBIDDEN = 4403  # unknown / revoked device, or workspace-ACL violation
    AUTH_TIMEOUT = 4408  # auth not completed within auth_deadline_s
    RATE_LIMITED = 4429  # too many connection attempts


# --------------------------------------------------------------------------------------
# Frame base
# --------------------------------------------------------------------------------------


class Frame:
    """Base for every frame dataclass.

    Subclasses are ``@dataclass``es that set the ClassVar :attr:`T`. Instance fields are
    the frame body; ``t`` and ``v`` are *not* instance fields (they are derived from the
    class + :data:`PROTOCOL_VERSION`), so they never participate in equality and the
    round-trip invariant holds regardless of protocol version bookkeeping.

    Optional fields default to ``None`` and are **omitted** from the wire JSON; on decode
    a missing key restores the ``None`` default, so ``decode(encode(f)) == f``.
    """

    #: The wire discriminator for this frame type. Set on every concrete subclass.
    T: ClassVar[FrameType]
    #: The protocol version stamped into every frame.
    V: ClassVar[int] = PROTOCOL_VERSION

    def to_json(self) -> dict[str, Any]:
        """Serialize to a JSON-ready dict, including ``t`` and ``v``.

        ``None``-valued fields are dropped to keep the wire clean; enum values are
        lowered to their string form.
        """
        out: dict[str, Any] = {"t": self.T.value, "v": self.V}
        for f in dataclasses.fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            if value is None:
                continue
            out[f.name] = value.value if isinstance(value, Enum) else value
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Frame:
        """Construct from a decoded dict, ignoring ``t``/``v`` and any unknown keys.

        Unknown keys are tolerated for forward-compatibility (a newer connector may add
        fields). Missing *required* fields surface as a ``TypeError`` from the dataclass
        constructor, which :func:`decode` maps to :class:`ProtocolError`.
        """
        field_names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        kwargs = {k: v for k, v in data.items() if k in field_names}
        return cls(**kwargs)  # type: ignore[call-arg]


# --------------------------------------------------------------------------------------
# §10.1 Control frames
# --------------------------------------------------------------------------------------


@dataclass
class Hello(Frame):
    """client -> server, first frame on ``/stream`` (§6.3)."""

    device_id: str
    client_version: str
    epk_c: str  # base64 X25519 ephemeral public key (client)
    workspaces: list[str] = field(default_factory=list)  # abs cwds this device exposes
    T: ClassVar[FrameType] = FrameType.HELLO


@dataclass
class Challenge(Frame):
    """server -> client. Server proves its identity + binds it to this connection (§6.3).

    ``server_pubkey`` is SB's static Ed25519 public key; the client checks
    ``fingerprint(server_pubkey) == pinned_fingerprint`` (SSH-host-key style) *before*
    trusting the signature. ``server_sig`` covers ``"SBv1-chal" ‖ epk_s ‖ epk_c ‖ nonce_s``
    so an active MITM cannot substitute its own ephemeral key.
    """

    epk_s: str  # base64 X25519 ephemeral public key (server)
    server_pubkey: str  # base64 ed25519 static server public key (pin-checked by client)
    server_sig: str  # base64 ed25519 over "SBv1-chal" ‖ epk_s ‖ epk_c ‖ nonce_s
    nonce_s: str  # base64 32B fresh server nonce
    T: ClassVar[FrameType] = FrameType.CHALLENGE


@dataclass
class Auth(Frame):
    """client -> server. Proof of device-key possession bound to this channel (§6.3).

    ``device_sig`` covers ``nonce_s ‖ epk_s ‖ epk_c ‖ device_id`` so it cannot be
    replayed onto another connection.
    """

    device_sig: str  # base64 ed25519
    T: ClassVar[FrameType] = FrameType.AUTH


@dataclass
class AuthOk(Frame):
    """server -> client. Binds the socket to the device, returns its ACL, and carries the
    **server-authoritative replay origin** ``from_seq`` (§8.1).

    ``from_seq`` is the host's highest contiguous acked event seq for this device (its
    durable cursor); the connector replays every buffered event with ``seq > from_seq``
    immediately after ``auth_ok``. ``from_seq == 0`` on a brand-new device means "SB has
    nothing" → the connector replays from the start and SB also issues backfill queries.
    There is **no separate client ``resume`` frame at connect** — the origin is the host's
    to declare, so it rides ``auth_ok`` (one fewer round-trip, no ambiguity).
    """

    session_id: str
    server_time: str  # RFC3339 UTC
    from_seq: int = 0  # host cursor: replay events with seq > from_seq
    granted_workspaces: list[str] = field(default_factory=list)
    resume_supported: bool = True
    T: ClassVar[FrameType] = FrameType.AUTH_OK


@dataclass
class Resume(Frame):
    """**server -> client**, mid-session self-heal (§8.1). Re-issued by the host when it
    detects a seq gap, asking the connector to re-replay events with ``seq > from_seq``.

    Not sent at connect (the replay origin rides ``auth_ok.from_seq``); this frame exists
    only for the gap-recovery path, where the host's cursor is the single source of truth.
    """

    from_seq: int
    T: ClassVar[FrameType] = FrameType.RESUME


@dataclass
class Ack(Frame):
    """server -> client. Cumulative: everything through ``ack_seq`` is durably persisted."""

    ack_seq: int
    T: ClassVar[FrameType] = FrameType.ACK


@dataclass
class Lock(Frame):
    """server -> client. Compaction park (§9). Connector holds new events in its buffer."""

    reason: str = "compaction"
    T: ClassVar[FrameType] = FrameType.LOCK


@dataclass
class Unlock(Frame):
    """server -> client. Release the compaction park; behaves like a reconnect (§9)."""

    T: ClassVar[FrameType] = FrameType.UNLOCK


@dataclass
class Ping(Frame):
    """either direction. App-level liveness / RTT probe (§4.2)."""

    ts: str
    T: ClassVar[FrameType] = FrameType.PING


@dataclass
class Pong(Frame):
    """either direction. Reply to :class:`Ping`."""

    ts: str
    T: ClassVar[FrameType] = FrameType.PONG


@dataclass
class Error(Frame):
    """either direction. Non-fatal (``fatal=False``) or fatal application error."""

    code: str
    message: str
    fatal: bool = False
    T: ClassVar[FrameType] = FrameType.ERROR


@dataclass
class Bye(Frame):
    """either direction. Graceful close intent (maps to close code 1000)."""

    reason: str = "shutdown"
    T: ClassVar[FrameType] = FrameType.BYE


# --------------------------------------------------------------------------------------
# §6.2 Enrollment sub-flow (path /enroll, pre-auth)
# --------------------------------------------------------------------------------------


@dataclass
class EnrollHello(Frame):
    """client -> server on ``/enroll``. Announces a fresh device pubkey + desired cwds."""

    device_pubkey: str  # base64 ed25519 device public key
    device_name: str
    want: list[str] = field(default_factory=list)  # requested workspace cwds
    epk_c: str = ""  # base64 X25519 ephemeral public key (client)
    T: ClassVar[FrameType] = FrameType.ENROLL_HELLO


@dataclass
class EnrollChallenge(Frame):
    """server -> client on ``/enroll``. Server-authenticates before the device proves PC.

    Same server-auth + channel-binding shape as :class:`Challenge` (the ``/enroll`` twin):
    the client pin-checks ``server_pubkey`` then verifies ``server_sig`` over
    ``"SBv1-enrchal" ‖ epk_s ‖ epk_c ‖ nonce``, and derives the E2E channel so the
    pairing-code proof travels sealed (edge-blind, §6.2). Note the field is ``nonce`` here
    vs ``nonce_s`` on ``/stream``.
    """

    epk_s: str
    server_pubkey: str  # base64 ed25519 static server public key (pin-checked by client)
    server_sig: str  # base64 ed25519 over "SBv1-enrchal" ‖ epk_s ‖ epk_c ‖ nonce
    nonce: str  # base64 fresh nonce
    T: ClassVar[FrameType] = FrameType.ENROLL_CHALLENGE


@dataclass
class EnrollProof(Frame):
    """client -> server on ``/enroll``. MAC (keyed by the pairing code) + device sig.

    The pairing code is *never* on the wire; it keys ``mac`` over the transcript so an
    edge that never saw PC can neither read it nor swap the device pubkey (§6.2).
    """

    mac: str  # base64 MAC_PC(transcript) (or PAKE proof)
    device_sig: str  # base64 ed25519 over transcript
    T: ClassVar[FrameType] = FrameType.ENROLL_PROOF


@dataclass
class EnrollOk(Frame):
    """server -> client on ``/enroll``. Device registered; returns its id + grants."""

    device_id: str
    granted_workspaces: list[str] = field(default_factory=list)
    T: ClassVar[FrameType] = FrameType.ENROLL_OK


@dataclass
class EnrollError(Frame):
    """server -> client on ``/enroll``. Enrollment refused (bad PC, expired, etc.)."""

    code: str
    message: str
    T: ClassVar[FrameType] = FrameType.ENROLL_ERROR


# --------------------------------------------------------------------------------------
# §10.2 Event frames (wrapper -> SB)
# --------------------------------------------------------------------------------------


@dataclass
class Event(Frame):
    """A perception event. ``seq`` is the per-device monotonic replay cursor (§8.1).

    ``kind`` is an :class:`EventKind`; ``payload`` is kind-specific (see §10.2 table).
    ``agent`` is present for ``agent.*`` / ``user.prompt`` kinds.
    """

    seq: int  # monotonic per device
    event_id: str  # stable, citable in memory/logs/reports
    ts: str  # RFC3339 UTC
    workspace_id: str  # "<device_id>:/abs/cwd"
    workspace_label: str  # cwd basename, display only
    kind: EventKind
    agent: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    T: ClassVar[FrameType] = FrameType.EVENT


# --------------------------------------------------------------------------------------
# §10.3 Action frames (SB -> wrapper) + result
# --------------------------------------------------------------------------------------


@dataclass
class Action(Frame):
    """An action addressed to a workspace. ``action_id`` doubles as the idempotency key."""

    action_id: str  # == idempotency_key
    ts: str
    workspace_id: str
    kind: ActionKind
    actor: str = "secondbrain"  # attributable-to-SB (§10.3, confirmed)
    authority: Authority = Authority.AUTONOMOUS
    agent: str | None = None  # target agent, for kinds that need it
    payload: dict[str, Any] = field(default_factory=dict)
    T: ClassVar[FrameType] = FrameType.ACTION


@dataclass
class ActionResult(Frame):
    """wrapper -> SB, correlated by ``action_id``. ``error`` present iff status != ok."""

    action_id: str
    status: ResultStatus = ResultStatus.OK  # ok | error | rejected
    result: dict[str, Any] | None = None  # kind-specific, e.g. {"spawned_name": "triage"}
    error: dict[str, Any] | None = None  # {"code": "...", "message": "..."}
    T: ClassVar[FrameType] = FrameType.ACTION_RESULT


# --------------------------------------------------------------------------------------
# §10.4 Query frames (SB -> wrapper, request/response)
# --------------------------------------------------------------------------------------


@dataclass
class Query(Frame):
    """A read-only backfill query. ``workspace_id`` is absent for ``list_workspaces``."""

    query_id: str
    kind: QueryKind
    workspace_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    T: ClassVar[FrameType] = FrameType.QUERY


@dataclass
class QueryResult(Frame):
    """wrapper -> SB, correlated by ``query_id``. ``error`` present iff status != ok."""

    query_id: str
    status: ResultStatus = ResultStatus.OK
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    T: ClassVar[FrameType] = FrameType.QUERY_RESULT


# --------------------------------------------------------------------------------------
# Registry + encode/decode
# --------------------------------------------------------------------------------------

#: Every concrete frame class, in spec order. Drives the decode dispatch table.
_FRAME_CLASSES: tuple[type[Frame], ...] = (
    Hello,
    Challenge,
    Auth,
    AuthOk,
    Resume,
    Ack,
    Lock,
    Unlock,
    Ping,
    Pong,
    Error,
    Bye,
    EnrollHello,
    EnrollChallenge,
    EnrollProof,
    EnrollOk,
    EnrollError,
    Event,
    Action,
    ActionResult,
    Query,
    QueryResult,
)

#: ``t`` value -> frame class. The single source of truth for decode dispatch.
_REGISTRY: dict[str, type[Frame]] = {cls.T.value: cls for cls in _FRAME_CLASSES}


def frame_class_for(t: str) -> type[Frame]:
    """Return the dataclass for a ``t`` discriminator, or raise :class:`ProtocolError`."""
    try:
        return _REGISTRY[t]
    except KeyError:
        raise ProtocolError(f"unknown frame type {t!r}") from None


def encode(frame: Frame) -> str:
    """Serialize a frame to a compact single-line JSON string (one WS text message)."""
    return json.dumps(frame.to_json(), separators=(",", ":"), ensure_ascii=False)


def decode(raw: str | bytes) -> Frame:
    """Parse one WS text message into a typed frame.

    Raises :class:`ProtocolError` for: non-JSON input, a non-object payload, a
    missing/unknown ``t`` discriminator, or a body missing a required field. The caller
    should close with :attr:`CloseCode.PROTOCOL_ERROR` (``4400``) on this error.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"frame is not valid UTF-8: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError(f"frame must be a JSON object, got {type(data).__name__}")
    t = data.get("t")
    if not isinstance(t, str):
        raise ProtocolError("frame missing string 't' discriminator")
    cls = frame_class_for(t)
    try:
        return cls.from_json(data)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"malformed {t!r} frame: {exc}") from exc


__all__ = [
    "PROTOCOL_VERSION",
    "MAX_FRAME_BYTES",
    "ProtocolError",
    "FrameType",
    "EventKind",
    "ActionKind",
    "QueryKind",
    "Authority",
    "ResultStatus",
    "CloseCode",
    "Frame",
    "Hello",
    "Challenge",
    "Auth",
    "AuthOk",
    "Resume",
    "Ack",
    "Lock",
    "Unlock",
    "Ping",
    "Pong",
    "Error",
    "Bye",
    "EnrollHello",
    "EnrollChallenge",
    "EnrollProof",
    "EnrollOk",
    "EnrollError",
    "Event",
    "Action",
    "ActionResult",
    "Query",
    "QueryResult",
    "encode",
    "decode",
    "frame_class_for",
]
