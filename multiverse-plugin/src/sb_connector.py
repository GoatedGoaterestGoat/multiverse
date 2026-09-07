"""Multiverse-side SecondBrain connector — the transport/auth *client* of the seam.

This is the Multiverse half of the SecondBrain seam (``SecondBrain/docs/INFRA.md``).
SecondBrain (SB) is a separate, locally-hosted "apprentice" that watches the user
across all their Multiverse workspaces and — within earned trust — acts on their
behalf. The connector is how a Multiverse remote becomes *observable and actionable*
to SB over one authenticated channel:

* **This machine is a "device."** One Ed25519 device keypair identifies it; the
  ``device_id`` (self-certifying, ``base32(sha256(pubkey))[:16]``) is both the auth
  identity and the namespacing key. A device exposes one or more workspaces (cwds).
* **The connector dials OUT.** SB is a server (behind NAT, on the user's Mac); the
  connector always connects out to ``wss://…`` — so a Multiverse remote on a GPU
  cluster only needs outbound internet. One long-lived WebSocket per device.
* **One socket per device.** SB's host *supersedes* a second connect from the same
  device, so the connector runs as a **host-aware singleton daemon** (lockfile under
  ``~/.multiverse/secondbrain/``) — any monitor can start/stop it; it survives the
  monitor quitting so SB keeps perceiving.

What is COMPLETE here: device keygen, the one-time ``/enroll`` flow, the pinned-server
handshake, the X25519->HKDF->ChaCha20-Poly1305 sealed channel, the durable
``seq``/``ack`` replay buffer, action idempotency, and ``lock``/``unlock`` park —
the transport/auth plumbing, adapted from ``SecondBrain/connector/sb_connector.py``
against the byte-identical vendored ``secondbrain.protocol`` + ``secondbrain.auth``.

What is STUBBED (the next phase — the "binding"): translating Multiverse's file
substrate (hooks, ``~/.multiverse/`` status/inbox/spawn/full_jsonls) into perception
*events*, and executing inbound *actions*/*queries* against Multiverse. Those handlers
return a clear ``not_implemented`` result for now; a single real event —
``workspace.connected`` — is emitted on connect as the end-to-end liveness proof.

CLI::

    python -m src.sb_connector enroll   # one-time device enrollment (widget-driven)
    python -m src.sb_connector stream    # run the long-lived connector daemon
    python -m src.sb_connector status    # print the current status JSON
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Make the vendored SecondBrain contract importable (same pattern the plugin uses
# for vendor/compaction): vendor/ on sys.path, then import the top-level package.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_VENDOR = str(_PLUGIN_ROOT / "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402

from secondbrain import auth  # noqa: E402  (vendored)
from secondbrain import protocol as p  # noqa: E402  (vendored)

from .atomic_files import (  # noqa: E402
    atomic_create_bytes,
    atomic_write_json,
    exclusive_file_lock,
)
from .paths import multiverse_dir  # noqa: E402

log = logging.getLogger("sb_connector")

#: Hard timeout for a single ``ask_agent`` fork (agreed with toolsmith). SB's host-side
#: query timeout is budgeted at 120s (this + margin for subprocess spawn + serialization
#: + seam roundtrip). Side-questions to a tool-less fork should return fast; if 90s isn't
#: enough the fork is stuck and cutting it is the right call.
_ASK_TIMEOUT = 90.0

#: Per-ask timeout for the first-connect workspace summary fan-out. Must EXCEED
#: Claude Code's persistent-retry budget (:envvar:`CLAUDE_CODE_UNATTENDED_RETRY` caps
#: total wait at 6 hours), otherwise ``subprocess.run`` would kill a still-retrying
#: fork mid-backoff and lose the summary to a rate-limit blip. 7 hours gives the CLI
#: room to burn its full retry envelope before we cut it.
_WORKSPACE_ASK_TIMEOUT_S = 7 * 60 * 60  # 25200s = 7 hours

#: Parallel-ask concurrency for the workspace summary fan-out. Match Multiverse's
#: ``ask_all`` default (5) — high enough to finish a typical workspace fast, low
#: enough not to flood the API with N simultaneous requests all vying for the same
#: rate-limit bucket.
_WORKSPACE_MAX_PARALLEL = 5


def _supports_secondbrain_runtime(runtime: str) -> bool:
    """Whether a Multiverse runtime participates in the SecondBrain seam."""
    return runtime in ("claude", "codex", "pi")


def _ssl_for(url: str) -> "ssl.SSLContext | None":
    """Build an SSL context for a ``wss://`` URL using certifi's CA bundle.

    Returns ``None`` for a plaintext ``ws://`` URL (websockets skips TLS).

    Uses certifi's bundle instead of the system default because minimal dev
    containers (Debian/Ubuntu slim images, some Kubernetes runtimes) often
    lack ``ca-certificates`` in Python's default trust-store search paths,
    yielding ``[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer
    certificate`` on every enroll/connect. certifi ships Mozilla's root store
    with the wheel — no OS package required — so we get consistent trust
    regardless of the host's ca-certificates state.
    """
    if not url.lower().startswith("wss://"):
        return None
    import ssl
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


def _ensure_claude_retry_env() -> None:
    """Guarantee the daemon's forks inherit Multiverse's rate-limit retry tuning.

    Interactive sessions launched via ``bin/multiverse`` get these vars exported by
    the launcher itself. But the SB connector daemon may be spawned outside that
    path (via ``sb_control.start()``, systemd, a dev-container init, etc.), and if
    its parent shell doesn't have these set, ``ask_all`` forks would fall back to
    the ``claude`` CLI's default 10-attempt retry with a short exponential backoff
    — a single rate-limit blip on a workspace-summary ask would surface as
    ``[ask failed: claude exited 1]`` in ``workspace.connected`` instead of
    retrying through it.

    Sets only if unset (respects an explicit operator override).
    """
    # Values kept in sync with bin/multiverse:52-53.
    os.environ.setdefault("CLAUDE_CODE_MAX_RETRIES", "30")
    os.environ.setdefault("CLAUDE_CODE_UNATTENDED_RETRY", "1")


def _validate_answers_against_questions(questions: list, answers: list) -> tuple[bool, str]:
    """Validate SB's ``answers`` against the pending AskUserQuestion ``questions`` shape.

    Returns ``(ok, err)`` — the ``err`` is the human message SB will surface on
    ``bad_request``. Checks: same length; per-question shape matches ``multiSelect``
    (string vs. list); at most one non-matching entry per multi-select (only one Other
    slot per question).
    """
    if not isinstance(answers, list):
        return False, "answers must be a list"
    if len(answers) != len(questions):
        return False, (
            f"answers length ({len(answers)}) doesn't match questions length "
            f"({len(questions)})"
        )
    for i, (q, a) in enumerate(zip(questions, answers)):
        options = q.get("options") or []
        labels = {opt.get("label", "") for opt in options if isinstance(opt, dict) and opt.get("label")}
        is_multi = bool(q.get("multiSelect"))
        if is_multi:
            if not isinstance(a, list):
                return False, (
                    f"answers[{i}] must be a list (question {i} is multiSelect); got "
                    f"{type(a).__name__}"
                )
            non_matching = [s for s in a if s not in labels]
            if len(non_matching) > 1:
                return False, (
                    f"answers[{i}] has {len(non_matching)} entries that don't match any "
                    "option label; at most one Other (custom text) allowed per question"
                )
        else:
            if not isinstance(a, str):
                return False, (
                    f"answers[{i}] must be a string (question {i} is single-select); "
                    f"got {type(a).__name__}"
                )
    return True, ""


def _compose_answer_status(agent_name: str, questions: list, answers: list) -> str:
    """Human-readable status text for the ``answer_question`` ActionResult — embedded
    verbatim in SB's tool return. Distinguishes preset picks from typed 'Other' text so
    the SB agent (and the user reading its output) can see when a custom answer was
    typed vs. one of the offered options selected."""

    def _fmt_entry(a, labels: set[str]) -> str:
        if isinstance(a, list):
            parts = []
            for s in a:
                if s in labels:
                    parts.append(f"'{s}'")
                else:
                    parts.append(f"'{s}' (typed)")
            return ", ".join(parts) if parts else "(no selection)"
        s = a if isinstance(a, str) else ""
        if s in labels:
            return f"'{s}'"
        return f"'{s}' (typed)"

    if len(questions) == 1:
        q = questions[0]
        labels = {
            opt.get("label", "") for opt in (q.get("options") or [])
            if isinstance(opt, dict) and opt.get("label")
        }
        return f"Answered '{agent_name}''s question with {_fmt_entry(answers[0], labels)}."

    parts: list[str] = []
    for i, (q, a) in enumerate(zip(questions, answers)):
        labels = {
            opt.get("label", "") for opt in (q.get("options") or [])
            if isinstance(opt, dict) and opt.get("label")
        }
        parts.append(f"Q{i + 1} → {_fmt_entry(a, labels)}")
    return f"Answered '{agent_name}''s question: {'; '.join(parts)}."


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def current_host() -> str:
    """Host identity for the singleton lock (shared-MULTIVERSE_ROOT deployments)."""
    return socket.gethostname()


# --------------------------------------------------------------------------------------
# On-disk layout under ~/.multiverse/secondbrain/  (outside the git repo)
# --------------------------------------------------------------------------------------


def sb_dir() -> Path:
    """The connector's state directory. Created lazily; 0700."""
    d = multiverse_dir() / "secondbrain"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def device_key_path() -> Path:
    return sb_dir() / "device.key"


def cf_creds_path() -> Path:
    return sb_dir() / "cf_access.json"


def config_path() -> Path:
    return sb_dir() / "config.json"


def status_path() -> Path:
    return sb_dir() / "status.json"


def lock_path() -> Path:
    return sb_dir() / "connector.lock"


_LOCK_TOKEN = uuid.uuid4().hex


def _lock_owner_path(token: str = _LOCK_TOKEN) -> Path:
    return sb_dir() / f".connector-lock-{token}.json"


def _lock_guard_path() -> Path:
    return sb_dir() / ".connector-lock.guard"


def _claimed_owner_path() -> Path | None:
    path = lock_path()
    try:
        raw = os.readlink(path)
    except OSError:
        return None
    target = Path(raw)
    return target if target.is_absolute() else path.parent / target


def _prune_orphan_lock_owners(active: Path) -> None:
    try:
        candidates = list(sb_dir().glob(".connector-lock-*.json"))
    except OSError:
        return
    for candidate in candidates:
        if candidate == active:
            continue
        try:
            candidate.unlink()
        except OSError:
            pass


def log_path() -> Path:
    return sb_dir() / "daemon.log"


# Default endpoint: SB hosted on the user's Mac, reached over the reused Cloudflare
# named tunnel. Overridable per-remote (the widget / SB_BASE_URL) — e.g. a same-box
# dev run points at ws://127.0.0.1:8765.
DEFAULT_BASE_URL = "wss://secondbrain.silicon23.me"


def _atomic_write_json(path: Path, data: dict, *, mode: int | None = None) -> None:
    atomic_write_json(path, data, mode=mode or 0o666, indent=2)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------------------
# Config (endpoint URL) + Cloudflare Access service-token creds
# --------------------------------------------------------------------------------------


def load_config() -> dict:
    """Connector config: the SB base URL. ``$SB_BASE_URL`` overrides the stored value."""
    data = _read_json(config_path()) or {}
    base = os.environ.get("SB_BASE_URL") or data.get("base_url") or DEFAULT_BASE_URL
    return {"base_url": base.rstrip("/")}


def save_config(base_url: str) -> None:
    _atomic_write_json(config_path(), {"base_url": base_url.rstrip("/")})


def enroll_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/enroll"


def stream_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/stream"


def load_cf_creds() -> dict[str, str] | None:
    """Cloudflare Access service-token creds, or ``None`` if unset.

    Precedence: environment ($CF_ACCESS_CLIENT_ID / $CF_ACCESS_CLIENT_SECRET, which
    ``bin/multiverse.local`` is the natural place to export on a cluster) over the
    0600 file the connect widget writes. Only needed for the ``wss://`` tunnel path;
    a same-box ``ws://127.0.0.1`` run needs none.
    """
    cid = os.environ.get("CF_ACCESS_CLIENT_ID")
    csecret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if cid and csecret:
        return {"client_id": cid, "client_secret": csecret}
    data = _read_json(cf_creds_path())
    if data and data.get("client_id") and data.get("client_secret"):
        return {"client_id": data["client_id"], "client_secret": data["client_secret"]}
    return None


def save_cf_creds(client_id: str, client_secret: str) -> None:
    """Persist CF Access creds 0600 (same at-rest posture as the device key)."""
    _atomic_write_json(
        cf_creds_path(),
        {"client_id": client_id, "client_secret": client_secret},
        mode=0o600,
    )


def clear_cf_creds() -> None:
    try:
        cf_creds_path().unlink()
    except OSError:
        pass


def cf_access_headers(creds: dict[str, str] | None) -> dict[str, str] | None:
    """Turn CF Access creds into the request headers the edge requires (INFRA §5.1)."""
    if creds and creds.get("client_id") and creds.get("client_secret"):
        return {
            "CF-Access-Client-Id": creds["client_id"],
            "CF-Access-Client-Secret": creds["client_secret"],
        }
    return None


# --------------------------------------------------------------------------------------
# Status file — what the monitor reads to render the SecondBrain widget
# --------------------------------------------------------------------------------------

# state ∈ {unenrolled, stopped, connecting, connected, reconnecting, locked, error}
def write_status(state: str, *, detail: str = "", **extra) -> None:
    payload = {
        "state": state,
        "detail": detail,
        "ts": _now_iso(),
        "host": current_host(),
        "pid": os.getpid(),
        **extra,
    }
    try:
        _atomic_write_json(status_path(), payload)
    except OSError:
        pass


def read_status() -> dict:
    return _read_json(status_path()) or {"state": "stopped", "detail": ""}


# --------------------------------------------------------------------------------------
# Durable connector state — MUST survive daemon restarts (INFRA §8.1)
# --------------------------------------------------------------------------------------
#
# ``seq`` is monotonic per device and durable across reconnects; the host's ack cursor
# survives too, so a restarted daemon must continue the sequence rather than restart at
# 1 (which would collide with the host's cursor). ``seeded`` is the set of workspaces
# already announced via ``workspace.connected`` — a first-ever event, not a
# per-reconnect one — so a restart doesn't re-announce them.


def state_path() -> Path:
    return sb_dir() / "state.json"


def outbox_path() -> Path:
    """Durable outbound event queue — un-acked events persist here so a notification
    survives a daemon restart (not just a socket blip), replaying on the next connect."""
    return sb_dir() / "outbox.json"


def load_conn_state() -> dict:
    """Read the durable connector state.

    ``exposed`` is the set of workspaces the user has currently opted-in for SB
    observation (a subset of the sidecar's ``granted_workspaces``). Introduced
    2026-07-20 for per-workspace connect/disconnect independence. Backward compat:
    if the field is missing (state written by pre-refactor code), fall back to the
    full granted list from the sidecar, preserving pre-refactor "everything
    granted is observed" behavior on first read after upgrade. Callers persist
    the field explicitly on any change via :func:`save_conn_state`.
    """
    data = _read_json(state_path()) or {}
    exposed_raw = data.get("exposed")
    if exposed_raw is None:
        # Legacy state.json → default to full granted list.
        try:
            exposed_raw = list(load_identity().granted_workspaces)
        except (FileNotFoundError, OSError, KeyError):
            exposed_raw = []
    return {
        "seq_hw": int(data.get("seq_hw", 0)),
        "seeded": list(data.get("seeded", [])),
        "seed_epochs": dict(data.get("seed_epochs") or {}),
        "exposed": list(exposed_raw),
    }


def save_conn_state(state: dict) -> None:
    _atomic_write_json(
        state_path(),
        {
            "seq_hw": int(state.get("seq_hw", 0)),
            "seeded": sorted(set(state.get("seeded", []))),
            "seed_epochs": dict(state.get("seed_epochs") or {}),
            "exposed": sorted(set(state.get("exposed", []))),
        },
    )


# --------------------------------------------------------------------------------------
# Host-aware singleton lock (mirrors sessions.py's host guard for shared FS)
# --------------------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _daemon_pid_alive(pid: int) -> bool:
    """True only if ``pid`` is alive AND is actually our connector daemon.

    Liveness alone is not enough for a pid read back off disk. The lock
    record persists across crashes and reboots, and once the OS recycles
    the number a bare probe reports the lock as held forever — which
    matters most in ``sb_control.stop()``, where that pid is then
    SIGTERM'd. Killing whatever inherited the number is not recoverable.

    Deliberately identity, not the heartbeat sitting right next to it.
    They answer different questions: the heartbeat says "is the daemon
    healthy", identity says "is this pid still mine". A stop button
    needs the second — a wedged-but-alive daemon that stopped
    heartbeating is exactly what someone is pressing stop to kill, and
    gating on health would refuse to kill it. (The heartbeat keeps its
    own job on the foreign-host path below, where a pid from another
    machine is meaningless and proof-of-life is all there is.)

    Unverifiable means False. This feeds three callers, and they don't
    want the same default: ``acquire_lock`` would rather assume held
    (avoid a second daemon), ``stop`` would rather assume not-ours
    (avoid signalling a stranger). False is correct because the
    double-start it risks is independently prevented — acquire_lock
    claims under an exclusive flock — while a wrong signal has no
    second line of defence.
    """
    if not _pid_alive(pid):
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "sb_connector" in out


#: The daemon refreshes the lock's ``heartbeat`` field every ``_HEARTBEAT_INTERVAL_S``
#: seconds. A lock whose heartbeat hasn't been touched in ``_HEARTBEAT_STALE_S`` is
#: considered dead — either the daemon crashed (missed heartbeats), or the entire host
#: is gone (foreign-container killed abruptly). The stale threshold is >3x the
#: interval to tolerate one missed write + normal clock skew between shared-FS hosts.
_HEARTBEAT_INTERVAL_S = 30.0
_HEARTBEAT_STALE_S = 120.0


def _parse_iso(s: str) -> float | None:
    """Parse an ISO-8601 UTC timestamp (as written by :func:`_now_iso`) to epoch seconds.

    Returns ``None`` on any parse failure so callers can treat missing/malformed
    timestamps as "unknown, treat as stale".
    """
    if not s:
        return None
    try:
        from datetime import datetime, timezone
        # _now_iso() writes YYYY-MM-DDTHH:MM:SSZ (no fractional part, trailing Z).
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return None


def _lock_is_fresh(data: dict) -> bool:
    """True if the lock's heartbeat is recent enough that we should treat it as live.

    Missing heartbeat → stale (locks written before the heartbeat feature landed, OR
    any daemon that died between acquire and the first heartbeat write; either way
    the safe read is "no live daemon behind this lock, take over").
    """
    import time
    hb = _parse_iso(data.get("heartbeat", ""))
    if hb is None:
        return False
    return (time.time() - hb) < _HEARTBEAT_STALE_S


def lock_holder() -> dict | None:
    """The live daemon holding the singleton lock, or None.

    Two signals, each used where it is the RIGHT one — not both everywhere.
    (An earlier version of this docstring claimed "both signals must agree" and
    described the heartbeat as belt-and-suspenders on same-host; the code never
    did that, and it shouldn't.)

    * **Same host** — process identity (``_daemon_pid_alive``): alive AND the
      pid's command line still names our daemon. The pid is meaningful here, and
      a recycled one would otherwise hold the lock forever and be SIGTERM'd by
      ``sb_control.stop()``.

      The heartbeat is deliberately not consulted on this path. It answers "is
      the daemon healthy", which is the wrong question for a caller deciding
      whether it may signal a pid: a wedged-but-alive daemon that stopped
      heartbeating is precisely what someone presses stop to kill, and gating on
      freshness would make it unkillable.

    * **Foreign host** — heartbeat freshness (``_lock_is_fresh``). A pid from
      another machine is meaningless on this one, so proof-of-life is all there
      is. Older than ``_HEARTBEAT_STALE_S``, or no ``heartbeat`` field at all
      (legacy schema), reads as stale.
    """
    data = _read_json(lock_path())
    if not data:
        return None
    try:
        pid = int(data.get("pid", 0))
    except (TypeError, ValueError):
        return None
    if data.get("host") == current_host():
        # Same host: the pid is meaningful, so identity is authoritative —
        # alive AND still our daemon, since a recycled pid would otherwise
        # hold the lock forever (and get SIGTERM'd by sb_control.stop()).
        # Heartbeat is deliberately NOT consulted here: a live daemon
        # briefly stalled between writes shouldn't be reaped, and a wedged
        # one must stay killable.
        return data if _daemon_pid_alive(pid) else None
    # Foreign host: PID is meaningless, heartbeat is the only proof of life.
    return data if _lock_is_fresh(data) else None


def acquire_lock() -> bool:
    """Claim the singleton lock for this process. False if a live daemon holds it.

    Writes an initial ``heartbeat`` immediately so a concurrent second start on
    another host reads us as live without waiting ``_HEARTBEAT_INTERVAL_S`` for
    the first refresh.
    """
    now = _now_iso()
    owner_path = _lock_owner_path()
    payload = {
        "host": current_host(),
        "pid": os.getpid(),
        "token": _LOCK_TOKEN,
        "started_at": now,
        "heartbeat": now,
    }
    with exclusive_file_lock(_lock_guard_path()):
        _atomic_write_json(owner_path, payload)
        if lock_path().is_symlink() or lock_path().exists():
            existing = _read_json(lock_path()) or {}
            if existing.get("token") == _LOCK_TOKEN:
                _prune_orphan_lock_owners(owner_path)
                return True
            if lock_holder() is not None:
                try:
                    owner_path.unlink()
                except OSError:
                    pass
                return False
            stale_owner = _claimed_owner_path()
            try:
                lock_path().unlink()
            except OSError:
                try:
                    owner_path.unlink()
                except OSError:
                    pass
                return False
            if (
                stale_owner is not None
                and stale_owner.name.startswith(".connector-lock-")
            ):
                try:
                    stale_owner.unlink()
                except OSError:
                    pass
        try:
            lock_path().symlink_to(owner_path.name)
            _prune_orphan_lock_owners(owner_path)
            return True
        except OSError:
            pass

    try:
        owner_path.unlink()
    except OSError:
        pass
    return False


def refresh_heartbeat() -> None:
    """Rewrite the lock file with a fresh heartbeat. Preserves ``started_at``.

    No-op if the lock has been taken over by a different (host, pid) — protects
    against stepping on a legitimate concurrent owner that reaped us as stale.
    """
    with exclusive_file_lock(_lock_guard_path()):
        data = _read_json(lock_path()) or {}
        if data.get("token") != _LOCK_TOKEN:
            return
        data["heartbeat"] = _now_iso()
        _atomic_write_json(_lock_owner_path(), data)


def release_lock() -> None:
    with exclusive_file_lock(_lock_guard_path()):
        data = _read_json(lock_path())
        if data and data.get("token") == _LOCK_TOKEN:
            try:
                lock_path().unlink()
            except OSError:
                pass
            try:
                _lock_owner_path().unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------------------
# Device identity (device key + enrollment result) — persisted 0600 on this device
# --------------------------------------------------------------------------------------


def code_mtime() -> float:
    """Latest modification time (epoch seconds) across every ``*.py`` file in this
    plugin's ``src/`` directory.

    The widget compares this against the running daemon's ``started_at`` to detect
    when the on-disk code has been updated (i.e. after a ``git pull``) but the
    daemon is still running the old code — a subtle footgun the user hit on their
    remote (restarting a specific agent's wrapper doesn't restart the SB daemon;
    the daemon is a device-level singleton with its own lifecycle). When stale,
    the widget surfaces a warning + offers the ``r`` reload shortcut.
    """
    src = _PLUGIN_ROOT / "src"
    if not src.is_dir():
        return 0.0
    latest = 0.0
    for path in src.rglob("*.py"):
        try:
            mt = path.stat().st_mtime
        except OSError:
            continue
        if mt > latest:
            latest = mt
    return latest


def load_or_create_device_key(path: str | os.PathLike[str]) -> ed25519.Ed25519PrivateKey:
    """Load the device's Ed25519 private key, generating it 0600 on first run."""
    p_ = Path(path)
    if p_.exists():
        return ed25519.Ed25519PrivateKey.from_private_bytes(p_.read_bytes())
    priv = ed25519.Ed25519PrivateKey.generate()
    if atomic_create_bytes(p_, priv.private_bytes_raw(), mode=0o600):
        return priv
    return ed25519.Ed25519PrivateKey.from_private_bytes(p_.read_bytes())


@dataclass
class DeviceIdentity:
    """The device's durable credential + enrollment result."""

    device_id: str
    private_key: ed25519.Ed25519PrivateKey
    pinned_server_fingerprint: str
    granted_workspaces: list[str] = field(default_factory=list)
    #: The human-readable label the user chose at enroll time (e.g. "goat-macbook").
    #: SB stores its own copy in its trust-store (that's what shows up in SB's device
    #: registry), so this field is a local-only convenience for the widget to prefill
    #: on subsequent runs — a user opening the widget from a different remote in a
    #: shared-~/.multiverse deployment doesn't want to see their new container's
    #: hostname suggested as if a re-enroll were expected. Empty string on legacy
    #: sidecars that predate this field.
    device_name: str = ""

    @property
    def device_pubkey_b64(self) -> str:
        return auth.b64e(self.private_key.public_key().public_bytes_raw())

    @classmethod
    def sidecar_path(cls, key_path: str | os.PathLike[str]) -> Path:
        return Path(f"{key_path}.json")

    def save(self, key_path: str | os.PathLike[str]) -> None:
        _atomic_write_json(
            self.sidecar_path(key_path),
            {
                "device_id": self.device_id,
                "pinned_server_fingerprint": self.pinned_server_fingerprint,
                "granted_workspaces": self.granted_workspaces,
                "device_name": self.device_name,
            },
            mode=0o600,
        )

    @classmethod
    def load(cls, key_path: str | os.PathLike[str]) -> DeviceIdentity:
        priv = load_or_create_device_key(key_path)
        data = json.loads(cls.sidecar_path(key_path).read_text(encoding="utf-8"))
        return cls(
            device_id=data["device_id"],
            private_key=priv,
            pinned_server_fingerprint=data["pinned_server_fingerprint"],
            granted_workspaces=data.get("granted_workspaces", []),
            device_name=data.get("device_name", ""),  # legacy sidecars: no name stored
        )


def is_enrolled() -> bool:
    return DeviceIdentity.sidecar_path(device_key_path()).exists()


def load_identity() -> DeviceIdentity:
    return DeviceIdentity.load(device_key_path())


# --------------------------------------------------------------------------------------
# Enrollment (§6.2) — one time per device
# --------------------------------------------------------------------------------------


async def enroll(
    *,
    url: str,
    pairing_code: str,
    pinned_fingerprint: str,
    device_name: str,
    want: list[str],
    key_path: str | os.PathLike[str],
    cf_access: dict[str, str] | None = None,
) -> DeviceIdentity:
    """Run the ``/enroll`` sub-flow and persist the resulting identity (§6.2)."""
    from websockets.asyncio.client import connect

    priv = load_or_create_device_key(key_path)
    device_pubkey = auth.b64e(priv.public_key().public_bytes_raw())

    async with connect(
        url,
        max_size=p.MAX_FRAME_BYTES,
        additional_headers=cf_access,
        ssl=_ssl_for(url),
    ) as ws:
        x_priv, epk_c = auth.generate_ephemeral()
        await ws.send(
            p.encode(
                p.EnrollHello(
                    device_pubkey=device_pubkey,
                    device_name=device_name,
                    want=want,
                    epk_c=epk_c,
                )
            )
        )
        chal = p.decode(await ws.recv())
        if not isinstance(chal, p.EnrollChallenge):
            raise ConnectionError(f"expected enroll_challenge, got {chal.T.value}")

        # Verify server identity BEFORE proving the pairing code: pin, then signature.
        _verify_server(
            chal.server_pubkey,
            chal.server_sig,
            pinned_fingerprint,
            auth.enroll_chal_transcript(chal.epk_s, epk_c, chal.nonce),
        )
        channel = auth.derive_channel(x_priv, chal.epk_s, chal.nonce, is_server=False)

        transcript = auth.enroll_transcript(
            device_pubkey_b64=device_pubkey,
            epk_c_b64=epk_c,
            epk_s_b64=chal.epk_s,
            nonce_b64=chal.nonce,
            device_name=device_name,
            want=want,
        )
        proof = p.EnrollProof(
            mac=auth.enroll_mac(pairing_code, transcript),  # pairing code stays off the wire
            device_sig=auth.sign(priv, transcript),
        )
        await ws.send(auth.seal(channel, p.encode(proof).encode("utf-8")))

        resp = p.decode(auth.open(channel, await ws.recv()))
        if isinstance(resp, p.EnrollError):
            raise ConnectionError(f"enroll rejected: {resp.code} — {resp.message}")
        if not isinstance(resp, p.EnrollOk):
            raise ConnectionError(f"expected enroll_ok, got {resp.T.value}")

        identity = DeviceIdentity(
            device_id=resp.device_id,
            private_key=priv,
            pinned_server_fingerprint=pinned_fingerprint,
            granted_workspaces=resp.granted_workspaces,
            device_name=device_name,
        )
        identity.save(key_path)
        log.info("enrolled device_id=%s granted=%s", resp.device_id, resp.granted_workspaces)
        return identity


def _verify_server(
    server_pubkey_b64: str, server_sig_b64: str, pinned_fp: str, transcript: bytes
) -> None:
    """Pin-check the server key then verify its signature; raise on either failure (§6.3)."""
    if auth.fingerprint_for_pubkey(auth.b64d(server_pubkey_b64)) != pinned_fp:
        raise ConnectionError("server fingerprint mismatch — possible MITM; aborting")
    if not auth.verify(server_pubkey_b64, server_sig_b64, transcript):
        raise ConnectionError("bad server signature — aborting")


# --------------------------------------------------------------------------------------
# Durable outbound buffer (events) + action dedup — INFRA §8
# --------------------------------------------------------------------------------------


class OutboundBuffer:
    """A durable, ordered buffer of un-acked events (§8.1). Events accumulate a
    strictly-increasing per-device ``seq`` and stay buffered until SB cumulatively
    ``ack``s them; on reconnect the tail (``seq > from_seq``) is replayed.

    When ``path`` is given the buffer is **persisted to disk** — un-acked events survive
    a daemon restart, so a notification produced while SB is disconnected is not lost.
    """

    def __init__(
        self, *, path: Path | None = None, start_seq: int = 0, on_seq=None,
        buffer_max: int = 50_000,
    ) -> None:
        self._path = path
        self._events: list[p.Event] = self._load()
        self._seq = itertools.count(start_seq + 1)  # continue from the durable high-water
        self._on_seq = on_seq  # callback(seq) -> persist the new high-water
        self._buffer_max = buffer_max

    def _load(self) -> list[p.Event]:
        if self._path is None:
            return []
        data = _read_json(self._path)
        if not data:
            return []
        try:
            return [p.Event.from_json(d) for d in data.get("events", [])]
        except (TypeError, ValueError):
            return []

    def _persist(self) -> None:
        if self._path is None:
            return
        try:
            _atomic_write_json(self._path, {"events": [e.to_json() for e in self._events]})
        except OSError:
            log.debug("outbox persist failed", exc_info=True)

    def next_seq(self) -> int:
        s = next(self._seq)
        if self._on_seq is not None:
            self._on_seq(s)  # durable before the event goes on the wire (§8.1)
        return s

    def append(self, event: p.Event) -> None:
        self._events.append(event)
        self._persist()
        if len(self._events) > self._buffer_max:
            log.warning("outbound buffer at cap (%s) — applying backpressure", self._buffer_max)

    def trim_through(self, ack_seq: int) -> None:
        before = len(self._events)
        self._events = [e for e in self._events if e.seq > ack_seq]
        if len(self._events) != before:
            self._persist()

    def replay_from(self, from_seq: int) -> list[p.Event]:
        return [e for e in self._events if e.seq > from_seq]

    def pending_after(self, seq: int) -> list[p.Event]:
        """Buffered events with ``seq`` greater than ``seq`` (the unsent tail)."""
        return [e for e in self._events if e.seq > seq]

    def has_after(self, seq: int) -> bool:
        return any(e.seq > seq for e in self._events)


class ActionDedup:
    """Idempotency guard for inbound actions (§8.2): ``action_id -> cached result``.
    A duplicate action is NOT re-executed — the cached result is returned. In-flight
    claims close the window before a background action has produced its result."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._seen: dict[str, p.ActionResult] = {}
        self._in_flight: set[str] = set()
        self._max = max_entries

    def cached(self, action_id: str) -> p.ActionResult | None:
        return self._seen.get(action_id)

    def claim(self, action_id: str) -> bool:
        """Reserve a new action ID; return ``False`` if seen or already executing."""
        if action_id in self._seen or action_id in self._in_flight:
            return False
        self._in_flight.add(action_id)
        return True

    def remember(self, result: p.ActionResult) -> None:
        self._in_flight.discard(result.action_id)
        self._seen[result.action_id] = result
        if len(self._seen) > self._max:
            self._seen.pop(next(iter(self._seen)))


# --------------------------------------------------------------------------------------
# The connector
# --------------------------------------------------------------------------------------


def _workspace_label(cwd: str) -> str:
    return Path(cwd).name or cwd


def _workspace_summary(cwd: str) -> str:
    """A short human description of a workspace for SB to seed per-workspace memory.

    TODO(binding): produce a real summary from the workspace's history (agents,
    recent activity). For now a minimal placeholder — enough for the first-connect
    ``workspace.connected`` liveness signal.
    """
    return f"Multiverse workspace '{_workspace_label(cwd)}' at {cwd}."


class SBConnector:
    """A single device's connection loop to SB (INFRA §13 steps 3-8)."""

    def __init__(
        self,
        base_url: str,
        identity: DeviceIdentity,
        cf_access: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.identity = identity
        self.cf_access = cf_access
        # Durable state (survives daemon restarts, §8.1): continue seq from the
        # high-water, remember which workspaces we've announced, and which spawn
        # requests we've already handled.
        self._state = load_conn_state()
        self.buffer = OutboundBuffer(
            path=outbox_path(), start_seq=self._state["seq_hw"], on_seq=self._persist_seq
        )
        self.dedup = ActionDedup()
        self.channel: auth.SessionChannel | None = None
        self._locked = False  # between lock and unlock (§9)
        self._stop = asyncio.Event()
        self._seeded: set[str] = set(self._state["seeded"])
        # workspace_id -> onboarding epoch, so a re-onboarded workspace emits a
        # distinct workspace.connected instead of colliding with the earlier one.
        self._seed_epochs: dict[str, str] = dict(self._state.get("seed_epochs") or {})
        # ``_exposed`` is the user's per-workspace opt-in — the subset of granted
        # workspaces we're actively observing right now. Emission is gated on this
        # so `d` in the widget can unexpose a single workspace without affecting
        # others. See ``load_conn_state`` for backward-compat defaulting.
        self._exposed: set[str] = {
            f"{identity.device_id}:{cwd}"
            for cwd in self._state["exposed"]
            if cwd in identity.granted_workspaces
        }
        self._marker_inited: set[str] = set()  # workspaces whose transcript markers are set
        # Serializes _emit_workspace_connected against concurrent invocation. Before
        # this, three rapid reconnects would fan out 3× ask_all runs in parallel
        # (17×3=51 forks in the user's droid_pipeline case) and interleave payloads
        # on the SB-cache write path.
        self._emit_lock = asyncio.Lock()
        self._ws = None  # the live connection while connected (for a graceful close)
        # ``ask_agent`` (long-running, up to 90s each) runs as background tasks so slow
        # asks don't block the receiver from other frames. Concurrency cap agreed with
        # toolsmith (SB is a single-issuer so 4 is ample headroom); asks over the cap are
        # rejected fast with ``busy`` rather than queued (SB's 120s query timeout would
        # expire before a queued ask completed anyway).
        self._active_asks: int = 0
        self._ask_max: int = 4
        # Producer <-> sender coordination: the watcher enqueues events and sets
        # _new_event; the per-connection sender flushes everything past _sent_through.
        self._new_event = asyncio.Event()
        self._sent_through = 0  # highest seq sent on the current connection
        self._poll_interval = 1.0  # producer poll cadence (s)

    def _persist_seq(self, seq: int) -> None:
        self._state["seq_hw"] = seq
        save_conn_state(self._state)

    def request_stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        """Graceful shutdown: flag stop, then send a proper WebSocket close (code 1000).

        Cancelling the task instead would abort the close handshake mid-flight and the
        peer would see an abnormal drop (1006). Closing the socket explicitly sends the
        1000 close frame AND unblocks a parked ``recv``, so :meth:`run` exits cleanly
        (its ``except`` sees ``_stop`` set and breaks). Bounded so an unresponsive peer
        can't wedge shutdown.
        """
        self.request_stop()
        ws = self._ws
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(code=1000, reason="shutdown"), timeout=5.0)
            except Exception:  # noqa: BLE001 - best-effort close; force-drop on timeout
                pass

    # ---- §13.3 connect loop with backoff + resume ---------------------------------

    async def run(self) -> None:
        from websockets.asyncio.client import connect

        backoff = 1.0
        while not self._stop.is_set():
            try:
                write_status(
                    "connecting",
                    detail=f"dialing {stream_url(self.base_url)}",
                    device_id=self.identity.device_id,
                    url=self.base_url,
                )
                async with connect(
                    stream_url(self.base_url),
                    max_size=p.MAX_FRAME_BYTES,
                    additional_headers=self.cf_access,
                    ssl=_ssl_for(stream_url(self.base_url)),
                ) as ws:
                    self._ws = ws  # exposed to aclose() for a graceful 1000 close
                    try:
                        backoff = 1.0
                        self.channel = None
                        from_seq = await self._handshake(ws)
                        write_status(
                            "connected",
                            detail="authenticated",
                            device_id=self.identity.device_id,
                            url=self.base_url,
                            granted_workspaces=self.identity.granted_workspaces,
                        )
                        await self._emit_workspace_connected(from_seq)  # rewinds + enqueues
                        await self._pump(ws, from_seq)
                    finally:
                        self._ws = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - top-level reconnect guard
                if self._stop.is_set():
                    break
                log.info("connection lost (%s); reconnecting in %.1fs", exc, backoff)
                write_status(
                    "reconnecting",
                    detail=str(exc),
                    device_id=self.identity.device_id,
                    url=self.base_url,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    # ---- sealed frame I/O (post-challenge) ----------------------------------------

    async def _send(self, ws, frame: p.Frame) -> None:  # noqa: ANN001
        text = p.encode(frame)
        if self.channel is not None:
            await ws.send(auth.seal(self.channel, text.encode("utf-8")))
        else:
            await ws.send(text)

    async def _recv(self, ws) -> p.Frame:  # noqa: ANN001
        raw = await ws.recv()
        if self.channel is not None:
            raw = auth.open(self.channel, raw)
        return p.decode(raw)

    # ---- §13.3 handshake: hello -> challenge -> auth -> auth_ok --------------------

    async def _handshake(self, ws) -> int:  # noqa: ANN001
        x_priv, epk_c = auth.generate_ephemeral()
        await ws.send(
            p.encode(
                p.Hello(
                    device_id=self.identity.device_id,
                    client_version="mv-conn/0.1",
                    epk_c=epk_c,
                    workspaces=list(self.identity.granted_workspaces),
                )
            )
        )

        challenge = p.decode(await ws.recv())
        if not isinstance(challenge, p.Challenge):
            raise ConnectionError(f"expected challenge, got {challenge.T.value}")
        _verify_server(
            challenge.server_pubkey,
            challenge.server_sig,
            self.identity.pinned_server_fingerprint,
            auth.chal_transcript(challenge.epk_s, epk_c, challenge.nonce_s),
        )
        self.channel = auth.derive_channel(
            x_priv, challenge.epk_s, challenge.nonce_s, is_server=False
        )

        device_sig = auth.sign(
            self.identity.private_key,
            auth.auth_transcript(
                challenge.nonce_s, challenge.epk_s, epk_c, self.identity.device_id
            ),
        )
        await self._send(ws, p.Auth(device_sig=device_sig))

        auth_ok = await self._recv(ws)
        if not isinstance(auth_ok, p.AuthOk):
            raise ConnectionError(f"expected auth_ok, got {auth_ok.T.value}")
        # Refresh the ACL from the host and persist it (grants may have changed).
        if auth_ok.granted_workspaces != self.identity.granted_workspaces:
            self.identity.granted_workspaces = auth_ok.granted_workspaces
            self.identity.save(device_key_path())
        if auth_ok.from_seq == 0:
            log.info("new device/host — SB will backfill via queries")
        return auth_ok.from_seq  # the sender replays events with seq > from_seq

    # ---- one real event: workspace.connected (liveness proof) ---------------------

    async def _emit_workspace_connected(self, from_seq: int) -> None:  # noqa: ARG002
        """Enqueue ``workspace.connected`` for every FIRST-EVER exposed workspace.

        Semantics (SB 2026-07-16 + per-workspace refactor 2026-07-20):
          - "Exposed" = user has opted-in for this workspace to be observed by SB
            (see ``self._exposed``). Subset of granted; managed via the widget.
          - Only workspaces that are BOTH exposed AND not yet in ``_seeded`` need
            a first-connect run here. Everything else fast-paths.
          - SB drops non-first-connect events silently, so reconnect emission is
            gone entirely (see 2026-07-16 retirement).

        **First-ever exposed** (this ``(device, workspace)`` pair not yet in
        ``_seeded``):
          - Initialize transcript delivery markers for every named agent in the
            workspace to its latest jsonl entry, BEFORE any events fire — so
            subsequent transcript-bearing events cover "from now on," not the
            pre-existing backlog.
          - Run ``ask_all`` against every named agent with the fixed
            workspace-summary question — with the configured long retry timeout,
            5-parallel. Failed asks land as bracketed placeholder strings.
          - Emit event with payload ``{first_connect: True, workspace_summary: {…}}``.
            Stable ``event_id=wsconn-first:<workspace_id>`` — a mid-flow crash +
            restart is idempotent under SB's dedup.
          - Add to ``_seeded`` + persist.

        **Already seeded (all exposed workspaces)**: emit NOTHING. Sub-µs.

        Concurrency: guarded by ``self._emit_lock``. Before the guard, three rapid
        reconnects fanned out three concurrent ask_all runs (17×3=51 forks in
        droid_pipeline's case) and interleaved payloads on SB's cache write. The
        lock serializes; a second concurrent call finds ``_seeded`` already
        populated (by the first) and fast-returns.

        ``from_seq`` is kept in the signature for caller ergonomics but no longer
        used — the pre-2026-07-16 rewind-and-re-emit logic that used it existed
        only to make reconnect ``workspace.connected`` precede downtime backlog.
        """
        from . import sb_events

        async with self._emit_lock:
            # Pending set = workspaces the user has opted into observing (_exposed)
            # that haven't been first-connected yet. Note that _exposed already
            # filters against identity.granted_workspaces at init time — a workspace
            # can't be exposed without being granted.
            pending_first = [
                (cwd, f"{self.identity.device_id}:{cwd}")
                for cwd in self.identity.granted_workspaces
                if f"{self.identity.device_id}:{cwd}" in self._exposed
                and f"{self.identity.device_id}:{cwd}" not in self._seeded
            ]
            if not pending_first:
                return

            for cwd, workspace_id in pending_first:
                # The id has to survive a re-onboarding. SB dedups on event_id alone,
                # so an id keyed only on workspace_id is unique per SUBJECT, not per
                # event -- and workspace_id (device:cwd) is reusable forever. Today
                # `seeded` only ever grows, so it can't repeat by itself; but wiping
                # state.json to re-onboard a workspace is a real operation, and it
                # would re-emit the same id and have this silently dropped. That is
                # the most expensive event we produce -- an ask_all across every agent
                # in the workspace -- so losing it without an error is the worst case.
                #
                # The epoch makes the id unique per onboarding while still dedupng a
                # retry of the SAME one. It is persisted BEFORE the event is built, so
                # a crash between emitting and recording `seeded` replays the identical
                # id rather than inventing a second onboarding.
                epoch = self._seed_epochs.get(workspace_id)
                if not epoch:
                    epoch = uuid.uuid4().hex[:8]
                    self._seed_epochs[workspace_id] = epoch
                    self._state["seed_epochs"] = dict(self._seed_epochs)
                    save_conn_state(self._state)

                self._init_markers_for_workspace(cwd)
                summary = await self._gather_workspace_summary(cwd)
                self.make_event(
                    workspace_id=workspace_id,
                    workspace_label=_workspace_label(cwd),
                    kind=p.EventKind.WORKSPACE_CONNECTED,
                    payload=sb_events.workspace_connected_event_fields(agents_summary=summary),
                    event_id=f"wsconn-first:{workspace_id}:{epoch}",
                )
                self._seeded.add(workspace_id)
                self._marker_inited.add(workspace_id)

            self._state["seeded"] = sorted(self._seeded)
            self._state["seed_epochs"] = dict(self._seed_epochs)
            save_conn_state(self._state)

    def _init_markers_for_workspace(self, cwd: str) -> None:
        """Initialize sb_transcript markers to the latest jsonl entry for every named
        agent in ``cwd``. Called at first-ever workspace.connected — before any events
        fire — so the first transcript-bearing event we emit covers "from now on,"
        never the backlog. Idempotent per agent (``sb_transcript.initialize_marker``
        no-ops if a marker already exists), so a mid-flow crash + restart is safe."""
        from . import sb_transcript
        from .sessions import list_named_agents_in_cwd
        try:
            agents = [
                row for row in list_named_agents_in_cwd(cwd)
                if _supports_secondbrain_runtime(row.runtime)
            ]
        except Exception:  # noqa: BLE001
            log.debug("list_named_agents failed for %s", cwd, exc_info=True)
            return
        for row in agents:
            try:
                sb_transcript.initialize_marker(cwd, row.name)
            except Exception:  # noqa: BLE001
                log.debug("marker init failed for %s/%s", cwd, row.name, exc_info=True)

    async def _gather_workspace_summary(self, cwd: str) -> dict[str, str]:
        """Run per-agent asks across every named agent in ``cwd`` with the fixed
        workspace-summary question, returning a per-agent dict for the first-connect
        payload. Blocking work runs in an executor thread.

        Empty workspace (no named agents with a native session id) → ``{}`` (payload will
        carry ``workspace_summary: {}`` on the wire). Individual ask failures land as
        bracketed placeholder strings SB renders as-is, keeping the dict shape uniform.

        Rate-limit resilience:
          - :func:`_ensure_claude_retry_env` guarantees ``CLAUDE_CODE_MAX_RETRIES`` /
            ``CLAUDE_CODE_UNATTENDED_RETRY`` are set in the daemon's process env so
            the ``claude`` CLI's persistent-retry mode kicks in inside every fork.
          - Per-ask timeout is generous (:const:`_WORKSPACE_ASK_TIMEOUT_S`) — must
            EXCEED the CLI's retry budget, otherwise ``subprocess.run`` would kill a
            still-retrying fork mid-backoff.
          - We fan out with :class:`ThreadPoolExecutor` directly (rather than
            ``ask_all``) so we can log each ask's outcome as it lands — the user
            sees real-time progress in ``daemon.log`` instead of one final line
            after the slowest ask finishes.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import time

        from .ask_agents import AskTarget, _ask_one
        from .sessions import list_named_agents_in_cwd

        question = (
            "Please give a concise summary of what you have achieved so far "
            "in this workspace."
        )

        def _blocking() -> dict[str, str]:
            try:
                agents = [
                    row for row in list_named_agents_in_cwd(cwd)
                    if _supports_secondbrain_runtime(row.runtime)
                ]
            except Exception as exc:  # noqa: BLE001
                log.info("workspace summary: agent enumeration failed for %s: %s", cwd, exc)
                return {}
            targets = [
                AskTarget(
                    name=a.name,
                    cwd=a.cwd,
                    claude_uuid=a.claude_uuid,
                    runtime=a.runtime,
                    session_id=a.session_id,
                )
                for a in agents if a.session_id or a.claude_uuid
            ]
            if not targets:
                log.info("workspace summary: %s has no askable agents (empty workspace)", cwd)
                return {}

            log.info(
                "workspace summary: asking %d agents in %s (per-ask timeout %ds, "
                "%d parallel, retry env: MAX_RETRIES=%s UNATTENDED=%s)",
                len(targets), cwd, int(_WORKSPACE_ASK_TIMEOUT_S), _WORKSPACE_MAX_PARALLEL,
                os.environ.get("CLAUDE_CODE_MAX_RETRIES", "unset"),
                os.environ.get("CLAUDE_CODE_UNATTENDED_RETRY", "unset"),
            )
            t0 = time.monotonic()
            plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT")
            out: dict[str, str] = {}
            done = 0
            with ThreadPoolExecutor(max_workers=_WORKSPACE_MAX_PARALLEL) as ex:
                futures = {
                    ex.submit(_ask_one, t, question, plugin_root,
                              _WORKSPACE_ASK_TIMEOUT_S, None): t
                    for t in targets
                }
                for f in as_completed(futures):
                    t = futures[f]
                    done += 1
                    try:
                        r = f.result()
                    except Exception as exc:  # noqa: BLE001
                        out[t.name] = f"[ask failed: {exc}]"
                        log.info("workspace summary [%d/%d]: %s FAILED after threw: %s",
                                 done, len(targets), t.name, exc)
                        continue
                    if r.error:
                        out[r.name] = f"[ask failed: {r.error}]"
                        log.info(
                            "workspace summary [%d/%d]: %s FAILED in %.1fs: %s",
                            done, len(targets), r.name, r.elapsed, r.error,
                        )
                    elif r.response:
                        out[r.name] = r.response
                        log.info(
                            "workspace summary [%d/%d]: %s ok in %.1fs (%d chars)",
                            done, len(targets), r.name, r.elapsed, len(r.response),
                        )
                    else:
                        out[r.name] = "[ask failed: empty response]"
                        log.info(
                            "workspace summary [%d/%d]: %s empty response in %.1fs",
                            done, len(targets), r.name, r.elapsed,
                        )
            wall = time.monotonic() - t0
            n_ok = sum(1 for v in out.values() if not v.startswith("[ask failed:"))
            log.info(
                "workspace summary: %d/%d agents summarized in %.1fs wall time",
                n_ok, len(targets), wall,
            )
            return out

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _blocking)

    # ---- §13.4-6 stream: receiver + sender, concurrently -------------------------

    async def _pump(self, ws, from_seq: int) -> None:  # noqa: ANN001
        """Run the receiver and sender together until the connection drops.

        The sender starts by replaying the un-acked tail (``seq > from_seq``), then
        streams newly-produced events as the watcher enqueues them. If either side
        errors (e.g. the socket closes), we cancel the other and re-raise so ``run``
        reconnects.
        """
        self._sent_through = from_seq
        self._new_event.set()  # kick the sender to flush the tail (seq > from_seq)
        recv_task = asyncio.ensure_future(self._receiver(ws))
        send_task = asyncio.ensure_future(self._sender(ws))
        try:
            done, _ = await asyncio.wait(
                {recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for t in (recv_task, send_task):
                t.cancel()
            await asyncio.gather(recv_task, send_task, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc is not None:
                raise exc

    async def _receiver(self, ws) -> None:  # noqa: ANN001
        while not self._stop.is_set():
            frame = await self._recv(ws)
            await self._dispatch(ws, frame)

    async def _sender(self, ws) -> None:  # noqa: ANN001
        while not self._stop.is_set():
            if not self._locked:  # §9: hold everything while SB is compacting
                for event in self.buffer.pending_after(self._sent_through):
                    await self._send(ws, event)
                    self._sent_through = event.seq
            self._new_event.clear()
            # Re-check after clearing so a wakeup that raced the flush isn't lost.
            if not self._locked and self.buffer.has_after(self._sent_through):
                continue
            await self._new_event.wait()

    # ---- producer: watch the Multiverse substrate, enqueue events ------------------

    async def run_producer(self) -> None:
        """Relay events into the durable buffer. Runs continuously across reconnects
        (independent of the socket): events accumulate in the durable outbox while
        disconnected and replay on the next connect.

        Events are produced **firsthand** at Multiverse's own choke-points (e.g.
        ``spawn_requests.write_spawn_request`` / ``write_resolution`` via :mod:`sb_notify`)
        and dropped into a durable pending queue — the connector does not poll or race the
        file substrate. This loop just drains that queue onto the wire. The "connected
        before" gate was applied at record time, so nothing here needs re-gating."""
        while not self._stop.is_set():
            try:
                self._init_new_markers()            # transcript markers for newly-seeded workspaces
                self._consume_pending()              # spawn events (fully composed at the choke-point)
                self._consume_response_signals()     # agent.response (transcript generated here)
                self._consume_user_prompt_signals()   # user.prompt (transcript = context + text + overview)
                self._consume_question_signals()     # agent.question (transcript = "why" context)
                self._consume_answered_signals()     # user.answered (no transcript — agent was blocked)
                self._consume_permission_request_signals()  # agent.permission_request (transcript = "why")
                self._consume_permission_decision_signals() # user.permission_decision (no transcript)
            except Exception:  # noqa: BLE001 - a step error must not kill the producer
                log.debug("producer step failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    def _init_new_markers(self) -> None:
        """For each newly-seeded workspace, initialize the transcript delivery marker of
        each of its current agents to the LAST live turn — so the first agent.response
        covers "from now on", never the backlog (DESIGN §4b + the transcript spec). Runs
        off the connect path (in the producer loop); idempotent per workspace + per agent.
        A NEW agent created later has no marker → its first transcript covers its whole
        (short) history, which is correct."""
        pending = self._seeded - self._marker_inited
        if not pending:
            return
        from . import sb_transcript
        from .sessions import list_named_agents_in_cwd

        for workspace_id in pending:
            _, _, cwd = workspace_id.partition(":")
            try:
                for row in list_named_agents_in_cwd(cwd):
                    if not _supports_secondbrain_runtime(row.runtime):
                        continue
                    sb_transcript.initialize_marker(cwd, row.name)
            except Exception:  # noqa: BLE001
                log.debug("marker init failed for %s", cwd, exc_info=True)
            self._marker_inited.add(workspace_id)

    def _consume_response_signals(self) -> None:
        """Turn each fast turn-end signal (from the Stop hook) into an ``agent.response``
        event: generate the partial transcript (peek), compose the payload, buffer it,
        commit the marker, then drop the signal. Marker is committed AFTER the event is
        durably buffered, so a crash re-delivers (SB dedups), never a gap.

        Empty-peek suppression: when multiple Stop hooks fire before the producer's poll
        iteration (rapid-succession turns, or a batch of signals queued while the daemon
        was reconnecting), the FIRST peek drains all new content and advances the marker.
        Subsequent signals in the same iteration peek an already-current marker → empty
        text → useless event with empty ``full`` and a template ``summary`` reading
        ``agent 'X' finished a turn``. Skip those (still delete the signal + commit the
        marker as a no-op so we don't loop) — the first non-empty event already covers
        the same turns."""
        from . import sb_events, sb_notify, sb_transcript

        pending_prompt_turns = {
            (sig.get("cwd", ""), sig.get("agent", ""), sig.get("turn_id"))
            for sig in sb_notify.list_user_prompt_signals()
            if sig.get("turn_id")
        }
        for sig in sb_notify.list_response_signals():
            cwd = sig.get("cwd", "")
            agent = sig.get("agent", "")
            turn_id = sig.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                if (cwd, agent, turn_id) in pending_prompt_turns:
                    log.debug(
                        "agent.response: %s turn %s still has a prompt signal; "
                        "deferring to preserve event order",
                        agent,
                        turn_id,
                    )
                    continue
                turn_state = sb_transcript.codex_turn_state(cwd, agent, turn_id)
                if turn_state is None:
                    log.debug(
                        "agent.response: %s turn %s not materialized; deferring",
                        agent,
                        turn_id,
                    )
                    continue
                status = turn_state.get("status")
                if status == "aborted":
                    log.info(
                        "agent.response: %s turn %s aborted; dropping signal",
                        agent,
                        turn_id,
                    )
                    sb_notify.delete_response_signal(sig)
                    continue
                if status != "completed":
                    log.debug(
                        "agent.response: %s turn %s is %s; deferring",
                        agent,
                        turn_id,
                        status or "unknown",
                    )
                    continue
            peeked = sb_transcript.peek_partial_transcript(cwd, agent)
            if peeked is None:  # agent unresolvable (gone?) — drop the signal
                log.debug("agent.response: %s unresolvable, dropping signal", agent)
                sb_notify.delete_response_signal(sig)
                continue
            text, cursor = peeked
            # Content-emptiness check: skip if the peek has nothing meaningful.
            # Two shapes qualify:
            #   1) truly empty ("") — batched-signal case (a prior signal drained it)
            #   2) only structural [turn] headers, no [user]/[assistant]/[tool:] lines
            #      — happens when parse_session yields turns whose leaves all got
            #      dropped by filter_compactable (structural noise like task_reminder,
            #      skill_listing, deferred-tools; or an all-thinking turn with no
            #      asst_text; or a turn whose only content is tool_result which
            #      _render_leaf drops). Suppress it — the event would carry a full of
            #      just "[turn]\n[turn]" (SB caches it but has_full renders False for
            #      whitespace-only content) and the composer's _last_assistant_line
            #      falls back to the placeholder template.
            has_meaningful = any(
                line.startswith("[user] ")
                or line.startswith("[assistant] ")
                or line.startswith("[tool:")
                or line.startswith("[tool-result]")
                for line in text.splitlines()
            )
            if not text or not has_meaningful:
                # Rich diagnostic so we can see WHY the peek is content-less. Include
                # the peek length + line counts so SB-side reports have something to
                # correlate against.
                stem = Path(sig["_path"]).stem[:8]
                lines = text.splitlines()
                n_turn = sum(1 for l in lines if l.startswith("[turn"))
                log.info(
                    "agent.response: %s SUPPRESSED (aresp:%s..., %d chars, %d lines, "
                    "%d [turn] headers, 0 meaningful content lines); marker committed "
                    "as no-op / re-anchor",
                    agent, stem, len(text), len(lines), n_turn,
                )
                sb_transcript.commit_marker(cwd, agent, cursor)  # no-op / re-anchor
                sb_notify.delete_response_signal(sig)
                continue
            payload = sb_events.agent_response_event_fields(agent, text)
            self.make_event(
                workspace_id=sig.get("workspace_id", ""),
                workspace_label=sig.get("workspace_label", ""),
                kind="agent.response",
                payload=payload,
                agent=agent,
                event_id=f"aresp:{Path(sig['_path']).stem}",  # unique per turn-end signal
            )
            sb_transcript.commit_marker(cwd, agent, cursor)  # after durable buffer
            sb_notify.delete_response_signal(sig)
            # Rich per-emission log: byte length + counts so SB-side "empty full"
            # reports can be correlated against what we actually sent.
            n_asst = text.count("\n[assistant] ") + (
                1 if text.startswith("[assistant] ") else 0
            )
            n_tool = text.count("\n[tool:") + (1 if text.startswith("[tool:") else 0)
            n_user = text.count("\n[user] ") + (1 if text.startswith("[user] ") else 0)
            log.info(
                "relayed agent.response (idle) for %s (%d chars full, "
                "%d asst, %d tool, %d user)",
                agent, len(text), n_asst, n_tool, n_user,
            )

    def _consume_user_prompt_signals(self) -> None:
        """Turn each turn-start signal (from the UserPromptSubmit hook) into a
        ``user.prompt`` event: generate the partial transcript (peek) as context
        including this new prompt at the end, compose the payload (text + cheap
        connector-composed overview_since_last + full), buffer it, commit the marker,
        drop the signal. Order = make_event → commit → delete for at-least-once (SB
        dedups by ``event_id``).

        Claude's hook fires after its JSONL row is written. Codex can invoke the hook
        before its rollout row is visible, so turn-keyed Codex signals are deferred until
        the matching turn contains real user input. The eventual transcript therefore
        still ends with the new prompt — deliberate redundancy with the ``text`` field
        per the SB contract."""
        from . import sb_events, sb_notify, sb_transcript

        for sig in sb_notify.list_user_prompt_signals():
            cwd = sig.get("cwd", "")
            agent = sig.get("agent", "")
            text = sig.get("text", "")
            turn_id = sig.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                turn_state = sb_transcript.codex_turn_state(cwd, agent, turn_id)
                if turn_state is None or (
                    not turn_state.get("has_user_input")
                    and not turn_state.get("synthetic_only")
                ):
                    log.debug(
                        "user.prompt: %s turn %s not materialized; deferring",
                        agent,
                        turn_id,
                    )
                    continue
                if turn_state.get("synthetic_only"):
                    log.info(
                        "user.prompt: %s turn %s synthetic; dropping signal",
                        agent,
                        turn_id,
                    )
                    sb_notify.delete_user_prompt_signal(sig)
                    continue
            if isinstance(turn_id, str) and turn_id:
                peeked = sb_transcript.peek_partial_transcript_through_codex_user(
                    cwd, agent, turn_id
                )
            else:
                peeked = sb_transcript.peek_partial_transcript(cwd, agent)
            transcript = ""
            cursor: dict | None = None
            if peeked is not None:
                transcript, cursor = peeked
            # A dropped signal here is safer than a broken event — if the agent is
            # unresolvable, still relay with empty full/overview so SB sees the prompt
            # (text is authoritative and independent of the transcript walker).
            payload = sb_events.user_prompt_event_fields(agent, text, transcript)
            self.make_event(
                workspace_id=sig.get("workspace_id", ""),
                workspace_label=sig.get("workspace_label", ""),
                kind=p.EventKind.USER_PROMPT,
                payload=payload,
                agent=agent,
                event_id=f"prompt:{Path(sig['_path']).stem}",
            )
            if cursor is not None:
                sb_transcript.commit_marker(cwd, agent, cursor)
            sb_notify.delete_user_prompt_signal(sig)
            log.info("relayed user.prompt for %s (%d chars)", agent, len(text))

    def _consume_question_signals(self) -> None:
        """Expand each ask-tool question signal into an ``agent.question`` event:
        generate the partial transcript (peek) as the "why" context, compose the payload
        (full question + choices verbatim in ``summary``; transcript in ``full``),
        buffer it, commit the marker, drop the signal. Order = make_event → commit →
        delete for at-least-once (SB dedups by ``event_id``)."""
        from . import sb_events, sb_notify, sb_transcript

        for sig in sb_notify.list_question_signals():
            cwd = sig.get("cwd", "")
            agent = sig.get("agent", "")
            question_id = sig.get("question_id", "")
            questions = sig.get("questions") or []
            peeked = sb_transcript.peek_partial_transcript(cwd, agent)
            transcript = ""
            cursor: dict | None = None
            if peeked is not None:
                transcript, cursor = peeked
            # If the agent is unresolvable we still relay the question — the transcript is
            # just missing "why" context, but the question itself is authoritative from the
            # PreToolUse payload we captured firsthand.
            payload = sb_events.agent_question_event_fields(
                agent, question_id, questions, transcript
            )
            self.make_event(
                workspace_id=sig.get("workspace_id", ""),
                workspace_label=sig.get("workspace_label", ""),
                kind="agent.question",
                payload=payload,
                agent=agent,
                event_id=f"aq:{question_id}",  # stable → SB dedups on retry
            )
            if cursor is not None:
                sb_transcript.commit_marker(cwd, agent, cursor)
            sb_notify.delete_question_signal(sig)
            log.info("relayed agent.question (%s) for %s", question_id, agent)

    def _consume_answered_signals(self) -> None:
        """Turn each answered signal into a ``user.answered`` event — no transcript
        (the agent was blocked between pre/post so nothing happened). Shares
        ``question_id`` with the earlier ``agent.question`` so SB pairs them."""
        from . import sb_events, sb_notify

        for sig in sb_notify.list_answered_signals():
            agent = sig.get("agent", "")
            question_id = sig.get("question_id", "")
            payload = sb_events.user_answered_event_fields(
                agent,
                question_id,
                sig.get("answers"),
                sig.get("questions"),
                sig.get("annotations"),
            )
            self.make_event(
                workspace_id=sig.get("workspace_id", ""),
                workspace_label=sig.get("workspace_label", ""),
                kind="user.answered",
                payload=payload,
                agent=agent,
                event_id=f"uans:{question_id}",
            )
            sb_notify.delete_answered_signal(sig)
            log.info("relayed user.answered (%s) for %s", question_id, agent)

    def _consume_permission_request_signals(self) -> None:
        """Expand each permission-request signal into an ``agent.permission_request``
        event: generate the partial transcript (peek) as the "why" context, compose the
        payload, buffer it, commit the marker, drop the signal. Order = make_event →
        commit → delete for at-least-once (SB dedups by ``event_id``).

        Codex invokes ``PermissionRequest`` before choosing its automatic reviewer or
        the user. Its hook therefore records a candidate, not proof that anybody is
        blocked. Promote that candidate only after the live tmux pane shows a focused
        approval selector; auto-approved/denied candidates are cleaned by PostToolUse
        or Stop without reaching the wire.
        """
        from . import sb_events, sb_notify, sb_transcript

        for sig in sb_notify.list_permission_request_signals():
            cwd = sig.get("cwd", "")
            agent = sig.get("agent", "")
            tool_use_id = sig.get("tool_use_id", "")
            tool_name = sig.get("tool_name", "")
            tool_input = sig.get("tool_input")
            turn_id = sig.get("codex_turn_id")
            if sig.get("await_user_confirmation") and not sb_notify.had_permission_prompt(
                tool_use_id
            ):
                if not self._codex_user_permission_prompt_is_visible(cwd, agent):
                    continue
                # Marker first: if the local user answers between this pane capture and
                # event composition, PostToolUse/Stop still closes the same pair.
                sb_notify.mark_permission_requested(tool_use_id)
            if turn_id and sb_transcript.codex_turn_state(cwd, agent, turn_id) is None:
                # Codex can invoke PermissionRequest before its rollout append becomes
                # visible. Keep the durable signal for the next producer pass so the
                # existing "why" transcript contract is not silently emptied.
                continue
            peeked = sb_transcript.peek_partial_transcript(cwd, agent)
            transcript = ""
            cursor: dict | None = None
            if peeked is not None:
                transcript, cursor = peeked
            payload = sb_events.agent_permission_request_event_fields(
                agent, tool_use_id, tool_name, tool_input, transcript
            )
            self.make_event(
                workspace_id=sig.get("workspace_id", ""),
                workspace_label=sig.get("workspace_label", ""),
                kind="agent.permission_request",
                payload=payload,
                agent=agent,
                event_id=f"apr:{tool_use_id}",  # stable → SB dedups on retry
            )
            if cursor is not None:
                sb_transcript.commit_marker(cwd, agent, cursor)
            sb_notify.delete_permission_request_signal(sig)
            log.info("relayed agent.permission_request (%s) for %s", tool_use_id, agent)

    @staticmethod
    def _codex_user_permission_prompt_is_visible(cwd: str, agent: str) -> bool:
        """Whether ``agent`` is blocked on an interactive Codex approval selector.

        This is deliberately the same strict parser used by
        ``answer_permission_prompt``. Merely seeing PermissionRequest hook output or a
        rendered composer is insufficient: auto-review receives the former, while an
        inactive/old approval surface must not be treated as a request for the user.
        """
        from . import sb_actions
        from .sessions import RUNTIME_CODEX, find_agent_by_name, tmux_session_exists

        target = find_agent_by_name(cwd, agent)
        if target is None or target.runtime != RUNTIME_CODEX or target.session is None:
            return False
        tmux_name = target.session.tmux_name
        if not tmux_session_exists(tmux_name):
            return False
        ok, pane = sb_actions.capture_pane(tmux_name)
        if not ok:
            return False
        options, focused = sb_actions.parse_codex_permission_options(pane)
        return bool(options) and focused is not None

    def _consume_permission_decision_signals(self) -> None:
        """Turn each decision signal into a ``user.permission_decision`` event — shares
        ``tool_use_id`` with the earlier ``agent.permission_request``. No transcript
        (agent was blocked between prompt and decision).

        Codex may finish an allowed tool before its rollout row is readable. In that
        case the request consumer deliberately keeps its signal pending so it can
        preserve the transcript contract; keep the matching decision pending too so
        the durable outbox can never publish the pair in reverse order.
        """
        from . import sb_events, sb_notify

        pending_requests = {
            sig.get("tool_use_id", "")
            for sig in sb_notify.list_permission_request_signals()
        }
        for sig in sb_notify.list_permission_decision_signals():
            agent = sig.get("agent", "")
            tool_use_id = sig.get("tool_use_id", "")
            if tool_use_id in pending_requests:
                continue
            payload = sb_events.user_permission_decision_event_fields(
                agent,
                tool_use_id,
                sig.get("decision", "allow"),
                sig.get("tool_name", ""),
                sig.get("tool_input"),
            )
            self.make_event(
                workspace_id=sig.get("workspace_id", ""),
                workspace_label=sig.get("workspace_label", ""),
                kind="user.permission_decision",
                payload=payload,
                agent=agent,
                event_id=f"upd:{tool_use_id}",
            )
            sb_notify.delete_permission_decision_signal(sig)
            log.info(
                "relayed user.permission_decision (%s) for %s: %s",
                tool_use_id, agent, sig.get("decision"),
            )

    def _consume_pending(self) -> None:
        """Drain durable pending event records (firsthand-produced) into the outbound
        buffer, oldest first, assigning each the next monotonic ``seq``. A record is
        deleted once it's durably buffered; a crash in that window re-buffers it under the
        same ``event_id``, which SB dedups (at-least-once → effectively-once)."""
        from . import sb_notify

        for rec in sb_notify.list_pending():
            self.make_event(
                workspace_id=rec.get("workspace_id", ""),
                workspace_label=rec.get("workspace_label", ""),
                kind=rec.get("kind", ""),
                payload=rec.get("payload", {}),
                agent=rec.get("agent"),
                event_id=rec.get("event_id"),
            )
            log.info("relayed %s (%s)", rec.get("kind"), rec.get("event_id"))
            sb_notify.delete_pending(rec)

    async def _dispatch(self, ws, frame: p.Frame) -> None:  # noqa: ANN001
        t = frame.T
        if t is p.FrameType.ACTION:
            await self._on_action(ws, frame)  # type: ignore[arg-type]
        elif t is p.FrameType.QUERY:
            await self._on_query(ws, frame)  # type: ignore[arg-type]
        elif t is p.FrameType.ACK:
            self.buffer.trim_through(frame.ack_seq)  # type: ignore[attr-defined]
        elif t is p.FrameType.RESUME:  # server gap self-heal: re-send seq > from_seq
            self._sent_through = min(self._sent_through, frame.from_seq)  # type: ignore[attr-defined]
            self._new_event.set()
        elif t is p.FrameType.LOCK:
            self._locked = True  # §9: sender holds new events; seqs keep advancing
            write_status(
                "locked",
                detail=getattr(frame, "reason", "compaction"),
                device_id=self.identity.device_id,
                url=self.base_url,
                granted_workspaces=self.identity.granted_workspaces,
            )
        elif t is p.FrameType.UNLOCK:
            self._locked = False  # §9: release; sender drains the held tail
            self._new_event.set()
            write_status(
                "connected",
                detail="unlocked",
                device_id=self.identity.device_id,
                url=self.base_url,
                granted_workspaces=self.identity.granted_workspaces,
            )
        elif t is p.FrameType.PING:
            await self._send(ws, p.Pong(ts=frame.ts))  # type: ignore[attr-defined]
        elif t is p.FrameType.BYE:
            raise ConnectionError("server sent bye")

    async def _on_action(self, ws, action: p.Action) -> None:  # noqa: ANN001
        """Execute an inbound action against Multiverse, idempotently (§8.2)."""
        cached = self.dedup.cached(action.action_id)
        if cached is not None:
            await self._send(ws, cached)  # retry: return cached result, do NOT re-run
            return
        if not self.dedup.claim(action.action_id):
            # The original background action will send the result for this action_id.
            return
        if action.kind == p.ActionKind.PROMPT_AGENT:
            # Delivery can involve tmux send-keys or spawning a resumed session — neither
            # is instantaneous. Run in a background task so the receiver stays responsive
            # to other frames (events, queries). The task dedups + sends the result.
            asyncio.create_task(self._handle_prompt_agent_action(ws, action))
            return
        if action.kind == p.ActionKind.ANSWER_QUESTION:
            asyncio.create_task(self._handle_answer_question_action(ws, action))
            return
        if action.kind == p.ActionKind.ANSWER_SPAWN_REQUEST:
            asyncio.create_task(self._handle_answer_spawn_request_action(ws, action))
            return
        if action.kind == p.ActionKind.SPAWN_AGENT:
            asyncio.create_task(self._handle_spawn_agent_action(ws, action))
            return
        if action.kind == p.ActionKind.ANSWER_PERMISSION_PROMPT:
            asyncio.create_task(self._handle_answer_permission_action(ws, action))
            return
        # TODO(binding): map the verb to a real Multiverse operation:
        #   answer_permission_prompt / message_agent
        result = p.ActionResult(
            action_id=action.action_id,
            status=p.ResultStatus.REJECTED,
            error={
                "code": "not_implemented",
                "message": (
                    f"connector action binding not implemented yet "
                    f"(verb={getattr(action.kind, 'value', action.kind)})"
                ),
            },
        )
        self.dedup.remember(result)
        await self._send(ws, result)

    async def _handle_prompt_agent_action(self, ws, action: p.Action) -> None:  # noqa: ANN001
        """Deliver a user-style prompt to a target agent (contract locked with the SB
        toolsmith). Runs as a background task off the receiver.

        Routing by target status:
          idle/working/listening         → tmux send-keys with save/clear/restore
                                            (ignores attached-check — per user directive)
          waiting                       → refuse while its interactive modal is open
          dormant/ended                  → create_session(initial_prompt=text) — auto-submits
                                            as the first user turn via the runtime wrapper
          retired                       → refuse permanently and name its successor
          retiring                      → refuse while its transcript is reserved
          starting                       → wait bounded for ready, then send-keys
          compacting/ending (locked)     → refuse with a specific message; SB retries later

        Errors: not_found | forbidden | retired | retiring | locked | deliver_failed. All
        ActionResults are
        cached in ``self.dedup`` so a retry with the same action_id returns the cached
        outcome and never re-executes (§8.2).
        """
        payload = action.payload or {}
        text = str(payload.get("text") or "")
        agent_name = str(action.agent or "").strip()

        def _finalize(status: p.ResultStatus, *, result=None, error=None) -> p.ActionResult:
            r = p.ActionResult(
                action_id=action.action_id, status=status, result=result, error=error,
            )
            self.dedup.remember(r)
            return r

        # ACL + basic validation. workspace_id is <device_id>:<cwd>; only granted cwds pass.
        wsid = action.workspace_id or ""
        _, sep, cwd = wsid.partition(":")
        if not sep or not cwd:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "workspace_id missing/malformed"},
            ))
        try:
            from pathlib import Path as _Path
            cwd_abs = str(_Path(cwd).expanduser().resolve())
        except (OSError, RuntimeError) as exc:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": f"invalid cwd: {exc}"},
            ))
        granted = {str(Path(w).expanduser().resolve()) for w in self.identity.granted_workspaces}
        if cwd_abs not in granted:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "forbidden", "message": "unknown/ungranted workspace"},
            ))
        if not agent_name:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "action.agent is required"},
            ))
        if not text:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "payload.text is required"},
            ))

        # Do the actual work in an executor thread — tmux subprocess + create_session are
        # blocking and must not sit on the event loop.
        loop = asyncio.get_running_loop()
        try:
            status_str, status_text, err = await loop.run_in_executor(
                None, self._deliver_prompt_blocking, cwd_abs, agent_name, text,
            )
        except Exception as exc:  # noqa: BLE001 — surface unexpected exceptions as deliver_failed
            log.exception("prompt_agent deliver crashed")
            return await self._send(ws, _finalize(
                p.ResultStatus.ERROR,
                error={"code": "deliver_failed", "message": str(exc)},
            ))

        if status_str == "ok":
            await self._send(ws, _finalize(
                p.ResultStatus.OK, result={"status": status_text},
            ))
            log.info("prompt_agent delivered to %s: %s", agent_name, status_text)
        else:
            await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": status_str, "message": err or ""},
            ))
            log.info("prompt_agent %s for %s: %s", status_str, agent_name, err)

    def _deliver_prompt_blocking(
        self, cwd_abs: str, agent_name: str, text: str,
    ) -> tuple[str, str, str]:
        """Blocking body of the prompt_agent action. Returns ``(status, status_text, err)``:

          status='ok'    → status_text = human-readable success line for SB to render
          status=<code>  → err = the message for SB to render (code = one of the error
                             codes documented on :meth:`_handle_prompt_agent_action`)

        The routing lives here (not on the async side) because we do a blocking roster
        resolution up-front, and the delivery primitives (:mod:`sb_actions`) are blocking.
        """
        from . import sb_actions
        from .compact_state import is_locked
        from .sessions import (
            current_host,
            find_agent_by_name,
            retired_agent_message,
            retiring_agent_message,
            tmux_session_exists,
        )

        target = find_agent_by_name(cwd_abs, agent_name)
        if target is None or not _supports_secondbrain_runtime(target.runtime):
            return ("not_found", "", f"no agent '{agent_name}' in this workspace")

        native_session_id = target.session_id
        if not native_session_id and target.session is not None:
            native_session_id = target.session.runtime_session_id

        # A compaction lock temporarily overlays a retired row as
        # ``compacting``, so consult the underlying persisted Session as well.
        # This must precede the lock route: retirement forbids new prompts at
        # all times, rather than merely postponing them until compaction ends.
        if target.status == "retired" or (
            target.session is not None and target.session.status == "retired"
        ):
            return (
                "retired",
                "",
                retired_agent_message(
                    agent_name,
                    target.session.successor if target.session is not None else None,
                ),
            )

        # ``retiring`` reserves an ALREADY-DORMANT transcript while context is
        # generated from it. Nothing is running, so the danger isn't that a prompt
        # fails -- it's that the routes below succeed: the dormant route RESUMES the
        # session, disturbing the very transcript being read, and pi's route queues a
        # prompt durably against it. Refuse ahead of both. The refusal is transient
        # ("try again later") because the reservation is, unlike ``retired``.
        # Same dual read as ``retired``: a compaction lock overlays the row status,
        # so consult the persisted Session too.
        if target.status == "retiring" or (
            target.session is not None and target.session.status == "retiring"
        ):
            return ("retiring", "", retiring_agent_message())

        # The status is part of the durable roster and must be sufficient on
        # its own. The lock remains an authoritative second source for the
        # small cross-monitor windows where a compaction status is stale.
        if target.status == "compacting" or (
            target.claude_uuid and is_locked(target.claude_uuid)
        ):
            return (
                "locked", "",
                f"'{agent_name}' can't be prompted right now because it is compacting.",
            )

        if target.status == "ending" or (
            target.session is not None
            and target.session.ending_marker_path.is_file()
        ):
            return (
                "locked", "",
                f"'{agent_name}' can't be prompted right now because it is ending.",
            )

        # If the session is None (never wrapped) or ended → dormant path.
        session = target.session
        is_dormant = session is None or target.status == "ended"

        if is_dormant:
            # Resume with the SB prompt as the first user turn — no send-keys needed;
            # the selected runtime wrapper picks up initial_prompt as a positional arg.
            if not native_session_id:
                return (
                    "not_found", "",
                    f"'{agent_name}' has no tracked {target.runtime} session ID yet — "
                    "can't resume",
                )
            try:
                self._auto_resume_with_prompt(target, cwd_abs, text)
            except Exception as exc:  # noqa: BLE001
                return ("deliver_failed", "", f"auto-resume failed: {exc}")
            return (
                "ok",
                f"Woke '{agent_name}' (was dormant) and delivered your prompt as its "
                "first user turn.",
                "",
            )

        # Pi's extension can accept a real user message without touching the terminal.
        # Queue durably rather than using Claude/Codex's composer parser: this preserves
        # any user draft, works while pi is busy, and also crosses hosts when the
        # Multiverse root is shared. The extension commits only after sendUserMessage
        # accepts the hand-off.
        if target.runtime == "pi":
            from .pi_prompt_queue import (
                PROMPT_CONSUMER_VERSION,
                has_prompt_consumer_capability,
                write_prompt,
            )

            if not has_prompt_consumer_capability(session.dir):
                return (
                    "deliver_failed",
                    "",
                    f"'{agent_name}' does not advertise the pi SecondBrain prompt "
                    f"consumer v{PROMPT_CONSUMER_VERSION}; restart its live pi "
                    "session before prompting it through SecondBrain",
                )
            write_prompt(cwd_abs, agent_name, text)
            return (
                "ok",
                f"Prompt queued for '{agent_name}' and will be delivered by its pi "
                "extension as a user message.",
                "",
            )

        # Claude/Codex live path — need the tmux name. Guard: if the session is alive on
        # ANOTHER host in a shared MULTIVERSE_ROOT, we can't reach its tmux from here.
        if session.host and session.host != current_host():
            return (
                "deliver_failed", "",
                f"'{agent_name}' is running on host '{session.host}', not this host",
            )
        tmux_name = session.tmux_name

        # Gate on the tmux session actually existing (defensive).
        if not tmux_session_exists(tmux_name):
            return (
                "deliver_failed", "",
                f"tmux session for '{agent_name}' is missing (was it just reaped?)",
            )

        # A ``waiting`` agent has a modal UI open — either AskUserQuestion (multi-choice
        # question) or a permission prompt — and Claude Code's TUI is actively consuming
        # keystrokes to navigate it. Send-keys here would corrupt the modal rather than
        # deliver a user-style prompt. Refuse cleanly; SB can call ``answer_question`` /
        # ``answer_permission_prompt`` first to close the modal, then retry ``prompt_agent``.
        #
        # Contrast with ``listening`` (blocked in monitor_inbox): that path shows no
        # interactive modal, so send-keys buffered in the terminal is picked up naturally
        # when the blocking tool call returns — the user confirmed that's fine to deliver.
        if target.status == "waiting":
            return (
                "waiting", "",
                f"'{agent_name}' is currently waiting for user input (a question or "
                "permission prompt); the prompt cannot be delivered until they respond.",
            )

        # ``starting`` (or generally: tmux exists but Claude UI not yet drawn) — bounded wait.
        if target.status == "starting":
            ok, err = sb_actions.wait_for_ready(tmux_name, runtime=target.runtime)
            if not ok:
                return ("deliver_failed", "", f"target didn't become ready: {err}")

        # Re-check the teardown claim immediately before terminal input. A
        # prompt may have entered the bounded starting wait just before the
        # user pressed `k`; the earlier status snapshot is no longer enough.
        if session.ending_marker_path.is_file():
            return (
                "locked", "",
                f"'{agent_name}' can't be prompted right now because it is ending.",
            )

        # require_composer closes the window between the status read above and these
        # keystrokes. `waiting` is refused up front, but status is a snapshot -- a target
        # can raise a modal in between, and typing a prompt into one is not a benign
        # miss: the modal reads digits as selections, so an ordinary prompt containing
        # a "1" picks option 1. No intent needed for that, which is what makes it worth
        # a second check rather than trusting the first.
        ok, saved, err = sb_actions.send_prompt_via_tmux(
            tmux_name, text, runtime=target.runtime, require_composer=True
        )
        if not ok:
            return ("deliver_failed", "", err)
        if saved:
            return (
                "ok",
                f"Prompt delivered to '{agent_name}'; saved and restored the text that "
                "was in its input box.",
                "",
            )
        return ("ok", f"Prompt delivered to '{agent_name}'.", "")

    def _auto_resume_with_prompt(self, target, cwd: str, prompt: str) -> None:
        """Headless resume of a dormant/ended target with an initial user prompt. Mirrors
        the message_agent auto_resume flow, but hands ``initial_prompt`` to create_session
        so the SB prompt becomes the runtime's first user turn."""
        from .sessions import (
            RUNTIME_CLAUDE,
            RUNTIME_CODEX,
            create_session,
            foreign_host_still_reported_live,
            kill_session,
        )
        if not _supports_secondbrain_runtime(target.runtime):
            raise RuntimeError(
                f"runtime '{target.runtime}' is not supported by SecondBrain"
            )
        native_session_id = target.session_id
        if not native_session_id and target.session is not None:
            native_session_id = target.session.runtime_session_id
        if not native_session_id:
            raise RuntimeError(
                f"'{target.name}' has no tracked {target.runtime} session ID"
            )
        if target.session is not None and target.status == "ended":
            if foreign_host_still_reported_live(target.session):
                raise RuntimeError(
                    f"'{target.name}' was created on host '{target.session.host}' and "
                    "its status there still says it's active — refusing to auto-resume "
                    "as a new session here"
                )
            kill_session(target.session)
        if target.runtime == RUNTIME_CLAUDE:
            runtime_args = ["--resume", native_session_id]
        elif target.runtime == RUNTIME_CODEX:
            runtime_args = ["resume", native_session_id]
        else:
            runtime_args = ["--session", native_session_id]
        create_session(
            cwd=cwd,
            claude_args=runtime_args,
            resumed_from=native_session_id,
            name=target.name,
            initial_prompt=prompt,
            runtime=target.runtime,
        )

    async def _handle_answer_question_action(self, ws, action: p.Action) -> None:  # noqa: ANN001
        """Answer a pending AskUserQuestion on the target agent (contract locked with the
        SB toolsmith). Runs as a background task off the receiver.

        Payload: ``{question_id, answers}`` where ``answers[i]`` pairs with the pending
        ``tool_input.questions[i]``. Each entry is a string (single-select — matching a
        label picks the preset; non-match is typed into Other) or a list of strings
        (multi-select, same rule per item, at most one Other per question).

        Errors: ``not_found`` (agent missing) | ``no_pending_question`` (no such AskUser
        Question pending for this agent) | ``forbidden`` (workspace ACL) | ``bad_request``
        (shape / length / multi-Other) | ``deliver_failed`` (tmux / send-keys failure).
        """
        payload = action.payload or {}
        question_id = str(payload.get("question_id") or "").strip()
        sb_answers = payload.get("answers")
        agent_name = str(action.agent or "").strip()

        def _finalize(status: p.ResultStatus, *, result=None, error=None) -> p.ActionResult:
            r = p.ActionResult(
                action_id=action.action_id, status=status, result=result, error=error,
            )
            self.dedup.remember(r)
            return r

        wsid = action.workspace_id or ""
        _, sep, cwd = wsid.partition(":")
        if not sep or not cwd:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "workspace_id missing/malformed"},
            ))
        try:
            cwd_abs = str(Path(cwd).expanduser().resolve())
        except (OSError, RuntimeError) as exc:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": f"invalid cwd: {exc}"},
            ))
        granted = {str(Path(w).expanduser().resolve()) for w in self.identity.granted_workspaces}
        if cwd_abs not in granted:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "forbidden", "message": "unknown/ungranted workspace"},
            ))
        if not agent_name or sb_answers is None:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request",
                       "message": "agent + answers required"},
            ))
        if not isinstance(sb_answers, list):
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "answers must be a list"},
            ))

        loop = asyncio.get_running_loop()
        try:
            status_str, status_text, err = await loop.run_in_executor(
                None, self._deliver_answer_blocking,
                cwd_abs, agent_name, question_id, sb_answers,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("answer_question crashed")
            return await self._send(ws, _finalize(
                p.ResultStatus.ERROR,
                error={"code": "deliver_failed", "message": str(exc)},
            ))

        if status_str == "ok":
            await self._send(ws, _finalize(
                p.ResultStatus.OK, result={"status": status_text},
            ))
            log.info("answer_question delivered to %s: %s", agent_name, status_text)
        else:
            await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": status_str, "message": err or ""},
            ))
            log.info("answer_question %s for %s: %s", status_str, agent_name, err)

    def _deliver_answer_blocking(
        self, cwd_abs: str, agent_name: str, question_id: str, sb_answers: list,
    ) -> tuple[str, str, str]:
        """Blocking body of answer_question: verify the pending AskUserQuestion, validate
        SB's answers against the pending questions[], and drive the modal via
        :func:`sb_actions.answer_askuserquestion_via_tmux`. Returns ``(status, ok_text, err)``.

        ``question_id`` is OPTIONAL (empty string when absent). When present, we verify
        it matches the pending AskUserQuestion for this agent — an authoritative
        "is-this-still-the-pending-one?" race guard. When absent, we resolve the pending
        AskUserQuestion from live state (an agent has at most one pending AskUserQuestion
        at a time — the tool blocks its turn). Toolsmith's design improvement, 2026-07-13.
        """
        from . import sb_actions, sb_notify
        from .sessions import (
            RUNTIME_PI,
            current_host,
            find_agent_by_name,
            tmux_session_exists,
        )

        target = find_agent_by_name(cwd_abs, agent_name)
        if target is None or not _supports_secondbrain_runtime(target.runtime):
            return ("not_found", "", f"no agent '{agent_name}' in this workspace")
        if target.runtime == RUNTIME_PI:
            return (
                "no_pending_question",
                "",
                "pi has no AskUserQuestion bridge for SecondBrain to answer",
            )

        # Locate the pending AskUserQuestion for this agent. Two paths:
        #  (a) explicit question_id from SB → look up pending_tools/<id>.json + verify
        #  (b) no question_id → scan pending_tools/ for THIS agent's live AskUserQuestion
        if question_id:
            pending = sb_notify.pending_tool_data(question_id)
            if pending is None:
                return (
                    "no_pending_question", "",
                    f"no pending question with id '{question_id}' for '{agent_name}' "
                    "(agent may have moved on or the question was already answered)",
                )
            if pending.get("tool_name") != "AskUserQuestion":
                return (
                    "no_pending_question", "",
                    f"pending tool for id '{question_id}' is "
                    f"'{pending.get('tool_name')}', not AskUserQuestion",
                )
            # Guard: pending record's agent must match the requested target — protects
            # against a stale question_id that happens to match a different agent's tool.
            if pending.get("agent") and pending.get("agent") != agent_name:
                return (
                    "no_pending_question", "",
                    f"pending question '{question_id}' is for agent "
                    f"'{pending.get('agent')}', not '{agent_name}'",
                )
        else:
            pending = sb_notify.find_pending_askuserquestion(cwd_abs, agent_name)
            if pending is None:
                return (
                    "no_pending_question", "",
                    f"no pending AskUserQuestion for '{agent_name}' in this workspace "
                    "(agent may have moved on or the question was already answered)",
                )

        tool_input = pending.get("tool_input") or {}
        questions = tool_input.get("questions") or []
        ok, err = _validate_answers_against_questions(questions, sb_answers)
        if not ok:
            return ("bad_request", "", err)

        # Live target guards.
        if target.session is None:
            return ("deliver_failed", "",
                    f"'{agent_name}' has no live session to answer the question in")
        if target.session.host and target.session.host != current_host():
            return (
                "deliver_failed", "",
                f"'{agent_name}' is running on host '{target.session.host}', not this host",
            )
        tmux_name = target.session.tmux_name
        if not tmux_session_exists(tmux_name):
            return ("deliver_failed", "",
                    f"tmux session for '{agent_name}' is missing (was it just reaped?)")

        ok, err = sb_actions.answer_askuserquestion_via_tmux(tmux_name, questions, sb_answers)
        if not ok:
            return ("deliver_failed", "", err)

        return ("ok", _compose_answer_status(agent_name, questions, sb_answers), "")

    async def _handle_answer_spawn_request_action(self, ws, action: p.Action) -> None:  # noqa: ANN001
        """Approve or decline a pending spawn_agent request (contract locked with the SB
        toolsmith).

        Delivery is FILE-BASED — the requesting spawn_agent MCP tool blocks reading a
        specific resolution file, so we just write it via
        :func:`spawn_requests.write_resolution` (O_EXCL-atomic — first writer wins).
        No send-keys, no tmux, no modal navigation. And write_resolution internally
        calls :func:`sb_notify.record_spawn_decision`, so the paired
        ``user.spawn_decision`` event fires back to SB through the normal pipeline
        without extra work.

        Payload: ``{request_id, decision: "approve"|"decline", name?: str}``. ``name``
        is only meaningful when approving; omitted/empty = keep the requester's
        default_name. An approval inherits the request's local ``caller_runtime`` and
        persists it in the resolution; runtime remains outside the SecondBrain payload.
        Errors: ``not_found`` / ``forbidden`` / ``already_decided`` / ``bad_request`` /
        ``deliver_failed``.
        """
        payload = action.payload or {}
        request_id = str(payload.get("request_id") or "").strip()
        decision = str(payload.get("decision") or "").strip()
        raw_name = payload.get("name")
        name = str(raw_name).strip() if raw_name else ""

        def _finalize(status: p.ResultStatus, *, result=None, error=None) -> p.ActionResult:
            r = p.ActionResult(
                action_id=action.action_id, status=status, result=result, error=error,
            )
            self.dedup.remember(r)
            return r

        # Validate payload.
        if not request_id:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "payload.request_id is required"},
            ))
        if decision not in ("approve", "decline"):
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request",
                       "message": f"payload.decision must be 'approve' or 'decline' (got {decision!r})"},
            ))
        if decision == "decline" and name:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request",
                       "message": "payload.name is only meaningful when decision is 'approve'"},
            ))

        # Workspace ACL from action.workspace_id.
        wsid = action.workspace_id or ""
        _, sep, cwd = wsid.partition(":")
        if not sep or not cwd:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "workspace_id missing/malformed"},
            ))
        try:
            cwd_abs = str(Path(cwd).expanduser().resolve())
        except (OSError, RuntimeError) as exc:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": f"invalid cwd: {exc}"},
            ))
        granted = {str(Path(w).expanduser().resolve()) for w in self.identity.granted_workspaces}
        if cwd_abs not in granted:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "forbidden", "message": "unknown/ungranted workspace"},
            ))

        # File I/O + write_resolution are blocking (subprocess call to tmux for the
        # caller-alive check). Run in the executor to keep the receiver responsive.
        loop = asyncio.get_running_loop()
        try:
            status_str, status_text, err_code, err_msg = await loop.run_in_executor(
                None, self._deliver_spawn_decision_blocking,
                cwd_abs, request_id, decision, name,
            )
        except Exception as exc:  # noqa: BLE001 - surface unexpected exceptions
            log.exception("answer_spawn_request deliver crashed")
            return await self._send(ws, _finalize(
                p.ResultStatus.ERROR,
                error={"code": "deliver_failed", "message": str(exc)},
            ))

        if status_str == "ok":
            await self._send(ws, _finalize(
                p.ResultStatus.OK, result={"status": status_text},
            ))
            log.info("answer_spawn_request delivered: %s", status_text)
        else:
            await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": err_code, "message": err_msg},
            ))
            log.info("answer_spawn_request %s (request_id=%s): %s",
                     err_code, request_id, err_msg)

    def _deliver_spawn_decision_blocking(
        self, cwd_abs: str, request_id: str, decision: str, name: str,
    ) -> tuple[str, str, str, str]:
        """Blocking body: read the spawn request, verify state, write resolution.

        Returns ``(status, status_text, err_code, err_msg)`` where:
          ``status='ok'``  → ``status_text`` is the success line for SB to render
          ``status='err'`` → ``err_code`` / ``err_msg`` for the ActionResult error dict
        """
        from . import spawn_requests as sr

        req_path = sr.request_path(request_id)
        if not req_path.is_file():
            return ("err", "", "not_found",
                    f"no pending spawn request with id '{request_id}' "
                    "(already resolved and cleaned up, or never existed)")

        # Read the request to get default_name + caller_tmux for liveness.
        try:
            req = json.loads(req_path.read_text())
        except (OSError, ValueError) as exc:
            return ("err", "", "not_found",
                    f"spawn request '{request_id}' is unreadable: {exc}")

        # Workspace match: the request's cwd must equal the action's cwd. Defends
        # against a misrouted request_id from a different workspace on the same device.
        req_cwd = str(req.get("cwd") or "")
        try:
            req_cwd_abs = str(Path(req_cwd).expanduser().resolve()) if req_cwd else ""
        except (OSError, RuntimeError):
            req_cwd_abs = ""
        if req_cwd_abs != cwd_abs:
            return ("err", "", "forbidden",
                    f"spawn request '{request_id}' is in a different workspace")

        default_name = str(req.get("default_name") or "").strip()

        # Caller liveness — if the requesting agent's tmux session died, the spawn tool
        # can no longer receive the resolution. Report not_found so SB knows the pair is
        # moot.
        caller_tmux = str(req.get("caller_tmux_session") or "")
        if not sr._caller_alive(caller_tmux):
            return ("err", "", "not_found",
                    f"spawn request '{request_id}' caller session died before it could "
                    "receive the decision")

        # Already-decided race check BEFORE writing. If someone already wrote a
        # resolution (user clicked the dashboard widget, or a prior SB action landed,
        # or ActionDedup missed a retry), we surface what won.
        def _describe(res: dict) -> str:
            st = str(res.get("status") or "").strip()
            nm = res.get("name")
            if st == "accepted":
                if nm and nm != default_name:
                    return f"approved with name '{nm}' (renamed from '{default_name}')"
                return f"approved with name '{nm or default_name}'"
            if st == "rejected":
                return "declined"
            return st or "unknown"

        existing = sr.read_resolution(request_id)
        if existing is not None:
            return ("err", "", "already_decided",
                    f"spawn request '{request_id}' was already resolved: {_describe(existing)}")

        # Do the write. O_EXCL — if we race and lose, treat as already_decided too.
        if decision == "approve":
            from . import sessions

            try:
                child_runtime = sessions.normalize_runtime(
                    req.get("caller_runtime") or sessions.RUNTIME_CLAUDE
                )
            except ValueError as exc:
                return ("err", "", "deliver_failed",
                        f"spawn request '{request_id}' has an invalid caller runtime: {exc}")
            if not _supports_secondbrain_runtime(child_runtime):
                return (
                    "err", "", "deliver_failed",
                    f"spawn request '{request_id}' uses runtime '{child_runtime}', "
                    "which is not supported by SecondBrain",
                )
            final_name = name if name else default_name
            wrote = sr.write_resolution(
                request_id, "accepted", final_name, runtime=child_runtime
            )
        else:  # decline
            final_name = ""
            wrote = sr.write_resolution(request_id, "rejected", None)

        if not wrote:
            # Race lost. Read what won and surface it.
            winner = sr.read_resolution(request_id) or {}
            return ("err", "", "already_decided",
                    f"spawn request '{request_id}' was resolved concurrently: {_describe(winner)}")

        # Success — compose result string per the samples locked with toolsmith.
        if decision == "approve":
            if name and name != default_name:
                text = f"Approved spawn of '{name}' (renamed from '{default_name}')."
            else:
                text = f"Approved spawn of '{final_name}' (requested name kept)."
        else:
            text = f"Declined spawn request for '{default_name}'."
        return ("ok", text, "", "")

    async def _handle_spawn_agent_action(self, ws, action: p.Action) -> None:  # noqa: ANN001
        """SB-initiated spawn of a new named agent (contract locked with toolsmith).

        Distinct from the ``spawn_agent`` MCP tool that Multiverse agents call: THAT
        writes a spawn_request file the user must approve; THIS one goes straight to
        :func:`sessions.create_session` under SB's own permission grant. So NO
        agent.spawn_request / user.spawn_decision pair fires — but the normal
        firsthand agent.created event (and user.prompt if instruction given) still
        flows back to SB as observation.

        Payload: ``{name, instruction?}``. ``name`` REQUIRED, unique in the workspace.
        ``instruction`` OPTIONAL — if provided, delivered as the new agent's first
        user turn via the selected runtime wrapper's positional-prompt path.

        Because the established payload has no runtime field and there is no local
        approval widget, runtime is a device-local policy selected by
        ``MULTIVERSE_SECONDBRAIN_SPAWN_RUNTIME`` (default: ``claude``).

        Result on ok: ``{status, spawned_name}``. Errors: ``duplicate_name`` /
        ``bad_request`` / ``forbidden`` / ``deliver_failed``.
        """
        payload = action.payload or {}
        name = str(payload.get("name") or "").strip()
        raw_instr = payload.get("instruction")
        instruction = str(raw_instr).strip() if raw_instr is not None else ""

        def _finalize(status: p.ResultStatus, *, result=None, error=None) -> p.ActionResult:
            r = p.ActionResult(
                action_id=action.action_id, status=status, result=result, error=error,
            )
            self.dedup.remember(r)
            return r

        if not name:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "payload.name is required (non-empty)"},
            ))

        # Workspace ACL from action.workspace_id.
        wsid = action.workspace_id or ""
        _, sep, cwd = wsid.partition(":")
        if not sep or not cwd:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "workspace_id missing/malformed"},
            ))
        try:
            cwd_abs = str(Path(cwd).expanduser().resolve())
        except (OSError, RuntimeError) as exc:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": f"invalid cwd: {exc}"},
            ))
        granted = {str(Path(w).expanduser().resolve()) for w in self.identity.granted_workspaces}
        if cwd_abs not in granted:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "forbidden", "message": "unknown/ungranted workspace"},
            ))

        # create_session forks tmux + a subprocess — not instantaneous. Run blocking.
        loop = asyncio.get_running_loop()
        try:
            status_str, status_text, spawned_name, err_code, err_msg = await loop.run_in_executor(
                None, self._deliver_spawn_blocking,
                cwd_abs, name, instruction,
            )
        except Exception as exc:  # noqa: BLE001 - surface unexpected exceptions
            log.exception("spawn_agent deliver crashed")
            return await self._send(ws, _finalize(
                p.ResultStatus.ERROR,
                error={"code": "deliver_failed", "message": str(exc)},
            ))

        if status_str == "ok":
            await self._send(ws, _finalize(
                p.ResultStatus.OK,
                result={"status": status_text, "spawned_name": spawned_name},
            ))
            log.info("spawn_agent delivered: %s", status_text)
        else:
            await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": err_code, "message": err_msg},
            ))
            log.info("spawn_agent %s (name=%s): %s", err_code, name, err_msg)

    def _deliver_spawn_blocking(
        self, cwd_abs: str, name: str, instruction: str,
    ) -> tuple[str, str, str, str, str]:
        """Blocking body: duplicate check + create_session.

        Returns ``(status, status_text, spawned_name, err_code, err_msg)`` where:
          ``status='ok'``  → ``status_text`` and ``spawned_name`` populate the result
          ``status='err'`` → ``err_code`` / ``err_msg`` populate the error dict
        """
        from . import sessions

        # Duplicate check — find_agent_by_name spans the whole roster (live + dormant),
        # so we catch collisions with agents the user or a prior SB action created.
        existing = sessions.find_agent_by_name(cwd_abs, name)
        if existing is not None:
            return ("err", "", "", "duplicate_name",
                    f"an agent named '{name}' already exists in this workspace")

        try:
            runtime = sessions.normalize_runtime(
                os.environ.get(
                    "MULTIVERSE_SECONDBRAIN_SPAWN_RUNTIME",
                    sessions.RUNTIME_CLAUDE,
                )
            )
        except ValueError as exc:
            return (
                "err", "", "", "deliver_failed",
                "invalid MULTIVERSE_SECONDBRAIN_SPAWN_RUNTIME: " + str(exc),
            )
        if not _supports_secondbrain_runtime(runtime):
            return (
                "err", "", "", "deliver_failed",
                "MULTIVERSE_SECONDBRAIN_SPAWN_RUNTIME must be 'claude', 'codex', or 'pi'",
            )

        try:
            sessions.create_session(
                cwd=cwd_abs, claude_args=[], name=name,
                runtime=runtime,
                initial_prompt=(instruction or None),
            )
        except Exception as exc:  # noqa: BLE001 - surface whatever create_session raised
            return ("err", "", "", "deliver_failed", f"create_session failed: {exc}")

        label = Path(cwd_abs).name or cwd_abs
        if instruction:
            text = f"Spawned '{name}' in '{label}' with an initial task."
        else:
            text = f"Spawned '{name}' idle in '{label}' (no initial task)."
        return ("ok", text, name, "", "")

    async def _handle_answer_permission_action(self, ws, action: p.Action) -> None:  # noqa: ANN001
        """Answer a pending permission prompt on the target agent (contract locked with
        the SB toolsmith, 2026-08-25). Runs as a background task off the receiver.

        Payload: ``{tool_use_id, decision: "allow"|"deny", feedback?}``, and the Action's
        ``agent`` field is REQUIRED -- the handler cross-checks that ``tool_use_id``
        belongs to that agent and refuses with ``stale_prompt`` otherwise, so a handle
        can't be used to answer a different agent's prompt. That requirement was missing
        from the contract as written and made the caller's first attempt fail on every
        call; it is stated here because the payload spec alone does not imply it.

        v1 deliberately
        offers allow-once and deny ONLY — no "don't ask again". That option writes a
        persistence rule whose destination can be a settings file and whose breadth is a
        user-editable pattern, so it outlives the call it was approving. It is also the
        conditional row, so excluding it keeps the option set we must recognise to the
        two that always render.

        Worth knowing why this verb is handled so defensively: a prompt that reached a
        human is the residue that the active runtime's policy did not approve. The pane
        is therefore re-read immediately before every decision, and each runtime driver
        accepts only an explicit one-time allow or non-persistent denial label.

        Errors: ``not_found`` | ``no_pending_permission`` | ``unsupported_option`` |
        ``stale_prompt`` | ``forbidden`` | ``bad_request`` | ``locked`` | ``deliver_failed``.
        """
        payload = action.payload or {}
        tool_use_id = str(payload.get("tool_use_id") or "").strip()
        decision = str(payload.get("decision") or "").strip().lower()
        feedback = payload.get("feedback")
        agent_name = str(action.agent or "").strip()

        def _finalize(status: p.ResultStatus, *, result=None, error=None) -> p.ActionResult:
            r = p.ActionResult(
                action_id=action.action_id, status=status, result=result, error=error,
            )
            self.dedup.remember(r)
            return r

        wsid = action.workspace_id or ""
        _, sep, cwd = wsid.partition(":")
        if not sep or not cwd:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "workspace_id missing/malformed"},
            ))
        try:
            cwd_abs = str(Path(cwd).expanduser().resolve())
        except (OSError, RuntimeError) as exc:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": f"invalid cwd: {exc}"},
            ))
        granted = {str(Path(w).expanduser().resolve()) for w in self.identity.granted_workspaces}
        if cwd_abs not in granted:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "forbidden", "message": "unknown/ungranted workspace"},
            ))
        if not agent_name:
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "agent required"},
            ))
        if decision not in ("allow", "deny"):
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request",
                       "message": "decision must be 'allow' or 'deny'"},
            ))
        if feedback is not None and not isinstance(feedback, str):
            return await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": "bad_request", "message": "feedback must be a string"},
            ))

        loop = asyncio.get_running_loop()
        try:
            status_str, status_text, err = await loop.run_in_executor(
                None, self._deliver_permission_blocking,
                cwd_abs, agent_name, tool_use_id, decision, feedback or "",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("answer_permission_prompt crashed")
            return await self._send(ws, _finalize(
                p.ResultStatus.ERROR,
                error={"code": "deliver_failed", "message": str(exc)},
            ))

        if status_str == "ok":
            await self._send(ws, _finalize(
                p.ResultStatus.OK, result={"status": status_text},
            ))
            log.info("answer_permission_prompt delivered to %s: %s", agent_name, status_text)
        else:
            await self._send(ws, _finalize(
                p.ResultStatus.REJECTED,
                error={"code": status_str, "message": err or ""},
            ))
            log.info("answer_permission_prompt %s for %s: %s", status_str, agent_name, err)

    def _deliver_permission_blocking(
        self, cwd: str, agent_name: str, tool_use_id: str, decision: str, feedback: str,
    ) -> tuple[str, str, str]:
        """Blocking half of ``answer_permission_prompt``. ``(outcome, status_text, error)``.

        The live pane is the authority on whether a prompt is up and what it offers.
        ``tool_use_id`` is checked when supplied, but only to catch SB answering a prompt
        that has since been replaced — a cache entry can outlive the modal it described,
        which is exactly how the AskUserQuestion detection went wrong.
        """
        from . import sb_actions, sb_notify
        from .compact_state import is_locked
        from .sessions import (
            RUNTIME_CODEX,
            RUNTIME_PI,
            find_agent_by_name,
            tmux_session_exists,
        )

        target = find_agent_by_name(cwd, agent_name)
        if target is None or not _supports_secondbrain_runtime(target.runtime):
            return "not_found", "", f"no agent named '{agent_name}' in this workspace"
        if target.runtime == RUNTIME_PI:
            return (
                "no_pending_permission",
                "",
                "pi has no permission prompt bridge for SecondBrain to answer",
            )
        if target.claude_uuid and is_locked(target.claude_uuid):
            return "locked", "", f"'{agent_name}' is compacting; cannot answer right now"
        tmux_name = target.session.tmux_name if target.session else None
        if not tmux_name or not tmux_session_exists(tmux_name):
            return (
                "no_pending_permission", "",
                f"'{agent_name}' has no live session, so no prompt can be pending",
            )

        if tool_use_id:
            record = sb_notify.pending_tool_data(tool_use_id)
            if record is None:
                return (
                    "stale_prompt", "",
                    f"no pending tool call {tool_use_id} — the prompt was likely "
                    "already answered",
                )
            if record.get("agent") not in ("", None, agent_name):
                return (
                    "stale_prompt", "",
                    f"tool call {tool_use_id} belongs to "
                    f"'{record.get('agent')}', not '{agent_name}'",
                )

        if target.runtime == RUNTIME_CODEX:
            ok, label, err = sb_actions.answer_codex_permission_via_tmux(
                tmux_name, decision, feedback=feedback or None,
            )
        else:
            ok, label, err = sb_actions.answer_permission_via_tmux(
                tmux_name, decision, feedback=feedback or None,
            )
        if not ok:
            if (
                "no permission prompt on screen" in err
                or "no Codex permission prompt on screen" in err
            ):
                return "no_pending_permission", "", err
            if "no option implements" in err:
                return "unsupported_option", "", err
            return "deliver_failed", "", err

        verb = "Allowed" if decision == "allow" else "Denied"
        text = f"{verb} '{agent_name}''s pending tool call (chose \"{label}\")."
        if feedback:
            text += f" Told it: {feedback}"
        return "ok", text, ""

    async def _on_query(self, ws, query: p.Query) -> None:  # noqa: ANN001
        """Answer a read-only backfill query from Multiverse state (§10.4)."""
        from . import sb_queries

        kind = query.kind
        kind_str = getattr(kind, "value", kind)  # enum -> its value; plain string as-is
        params = query.params or {}
        try:
            if kind == p.QueryKind.LIST_WORKSPACES:  # device-scoped; no workspace_id
                result = {"workspaces": sb_queries.list_workspaces(self.identity.granted_workspaces)}
            elif kind == p.QueryKind.GET_DASHBOARD:  # the full monitor dashboard (§4d)
                cwd = self._query_cwd(query)
                if cwd is None:
                    return await self._reject_query(ws, query, "forbidden", "unknown/ungranted workspace")
                result = {"sessions": sb_queries.dashboard(cwd)}
            elif kind == p.QueryKind.LIST_AGENTS:
                cwd = self._query_cwd(query)
                if cwd is None:
                    return await self._reject_query(ws, query, "forbidden", "unknown/ungranted workspace")
                result = {"agents": sb_queries.list_agents(cwd)}
            elif kind == p.QueryKind.GET_STATUS:
                cwd = self._query_cwd(query)
                agent = params.get("agent")
                if cwd is None or not agent:
                    return await self._reject_query(ws, query, "bad_request", "workspace_id + agent required")
                st = sb_queries.get_status(cwd, agent)
                result = st if st is not None else {"status": None}
            elif kind == p.QueryKind.ASK_AGENT:
                # Runs in a BACKGROUND task: the fork can take up to _ASK_TIMEOUT and we
                # can't stall the receiver on it. The task sends the QueryResult itself.
                asyncio.create_task(self._handle_ask_query(ws, query))
                return
            elif kind == p.QueryKind.PEEK_AGENT:
                # tmux capture-pane is fast (~10-50ms) — safe to run inline in the
                # receive loop. sb_queries.peek_agent returns a discriminated tuple we
                # translate directly to QueryResult ok / error+code.
                cwd = self._query_cwd(query)
                agent = params.get("agent")
                if cwd is None:
                    return await self._reject_query(ws, query, "forbidden", "unknown/ungranted workspace")
                if not agent:
                    return await self._reject_query(ws, query, "bad_request", "workspace_id + agent required")
                outcome, payload = sb_queries.peek_agent(cwd, agent)
                if outcome == "ok":
                    result = payload
                else:
                    return await self._reject_query(ws, query, outcome, payload["message"])
            else:  # get_transcript (recall path) — a later phase-2 step
                return await self._reject_query(
                    ws, query, "not_implemented",
                    f"query {kind_str} not implemented yet",
                )
        except Exception as exc:  # noqa: BLE001 - surface a failure, don't drop the query
            return await self._reject_query(ws, query, "query_failed", str(exc), status=p.ResultStatus.ERROR)
        await self._send(
            ws, p.QueryResult(query_id=query.query_id, status=p.ResultStatus.OK, result=result)
        )

    async def _handle_ask_query(self, ws, query: p.Query) -> None:  # noqa: ANN001
        """Background handler for ``ask_agent`` — run the blocking side-fork in an
        executor and reply when it returns.

        Contract with toolsmith: params ``{agent, question}``; workspace_id ACL-gated to
        this device's granted workspaces. Reply ``{answer}`` on ok; on failure, error
        codes ``not_found`` (agent missing / no runnable native session id), ``timeout`` (fork
        exceeded ``_ASK_TIMEOUT``), ``ask_failed`` (spawn or protocol failure), ``busy``
        (concurrency cap hit — SB will see this as a fast, retriable reject).
        """
        params = query.params or {}
        agent_name = (params.get("agent") or "").strip()
        question_text = (params.get("question") or "").strip()
        cwd = self._query_cwd(query)
        if cwd is None:
            return await self._reject_query(ws, query, "forbidden", "unknown/ungranted workspace")
        if not agent_name or not question_text:
            return await self._reject_query(ws, query, "bad_request", "agent + question required")
        if self._active_asks >= self._ask_max:
            log.info(
                "ask_agent rejected (busy): cap=%d, agent=%s", self._ask_max, agent_name,
            )
            return await self._reject_query(
                ws, query, "busy",
                f"connector at ask concurrency cap ({self._ask_max}); retry shortly",
            )

        # Resolve the target inside the executor thread so the roster lookup + fork share
        # one hop off the event loop. Import here to keep sb_connector's top-level light.
        from .ask_agents import AskTarget, ask_one
        from .sessions import find_agent_by_name

        def _blocking_ask() -> tuple[str, str | None, str | None]:
            """Return (status, answer_or_none, error_message_or_none) where status is
            one of "ok"/"not_found"/"timeout"/"ask_failed"."""
            target = find_agent_by_name(cwd, agent_name)
            if target is None or not _supports_secondbrain_runtime(target.runtime):
                return ("not_found", None, f"no agent '{agent_name}' in this workspace")
            if not target.session_id and not target.claude_uuid:
                return (
                    "not_found",
                    None,
                    f"agent '{agent_name}' has no tracked {target.runtime} session id yet "
                    "(just started?)",
                )
            r = ask_one(
                question=question_text,
                target=AskTarget(
                    name=target.name,
                    cwd=target.cwd,
                    claude_uuid=target.claude_uuid,
                    runtime=target.runtime,
                    session_id=target.session_id,
                ),
                timeout=_ASK_TIMEOUT,
            )
            if r.error:
                code = "timeout" if "timed out" in r.error else "ask_failed"
                return (code, None, r.error)
            return ("ok", r.response or "", None)

        # Truncate the question preview in the log so a big paste doesn't dominate the log.
        q_preview = question_text if len(question_text) <= 100 else question_text[:97] + "..."
        log.info(
            "handling ask_agent(agent=%s, cwd=%s, question=%r) [in_flight=%d/%d]",
            agent_name, cwd, q_preview, self._active_asks + 1, self._ask_max,
        )
        started = time.monotonic()
        self._active_asks += 1
        try:
            loop = asyncio.get_running_loop()
            status, answer, err = await loop.run_in_executor(None, _blocking_ask)
        except Exception as exc:  # noqa: BLE001 - unexpected; report as ask_failed
            elapsed = time.monotonic() - started
            log.info(
                "ask_agent crashed after %.2fs (agent=%s): %s", elapsed, agent_name, exc,
            )
            return await self._reject_query(ws, query, "ask_failed", str(exc), status=p.ResultStatus.ERROR)
        finally:
            self._active_asks -= 1

        elapsed = time.monotonic() - started
        if status == "ok":
            ans_len = len(answer or "")
            log.info(
                "ask_agent returned ok in %.2fs (agent=%s, %d chars)",
                elapsed, agent_name, ans_len,
            )
            await self._send(
                ws,
                p.QueryResult(
                    query_id=query.query_id,
                    status=p.ResultStatus.OK,
                    result={"answer": answer or ""},
                ),
            )
        else:
            log.info(
                "ask_agent returned %s in %.2fs (agent=%s): %s",
                status, elapsed, agent_name, err,
            )
            await self._reject_query(ws, query, status, err or "")

    def _query_cwd(self, query: p.Query) -> str | None:
        """Resolve a workspace-scoped query's cwd from ``workspace_id`` (``<device_id>:<cwd>``),
        gated by the device's ACL — return None if it isn't one of our granted workspaces.

        ``device_id`` is base32 (no ``:``), so a single split separates it from the abs cwd.
        The host already enforces the ACL; this is defense-in-depth.
        """
        wsid = query.workspace_id or (query.params or {}).get("workspace_id", "")
        _, sep, cwd = wsid.partition(":")
        if not sep or not cwd:
            return None
        cwd_abs = str(Path(cwd).expanduser().resolve())
        granted = {str(Path(w).expanduser().resolve()) for w in self.identity.granted_workspaces}
        return cwd_abs if cwd_abs in granted else None

    async def _reject_query(  # noqa: ANN001
        self, ws, query: p.Query, code: str, message: str,
        status: p.ResultStatus = p.ResultStatus.REJECTED,
    ) -> None:
        await self._send(
            ws,
            p.QueryResult(
                query_id=query.query_id,
                status=status,
                error={"code": code, "message": message},
            ),
        )

    # ---- producer side: translate a Multiverse signal into an event ----------------

    def make_event(
        self,
        workspace_id: str,
        workspace_label: str,
        kind,  # p.EventKind | str  (unknown kinds pass through as strings, §10)
        payload: dict,
        agent: str | None = None,
        event_id: str | None = None,
    ) -> p.Event:
        """Build an outbound event with the next durable per-device ``seq`` (§8.1)."""
        seq = self.buffer.next_seq()
        event = p.Event(
            seq=seq,
            event_id=event_id or f"evt_{seq:08d}",  # TODO(binding): source-citable ids
            ts=_now_iso(),
            workspace_id=workspace_id,
            workspace_label=workspace_label,
            kind=kind,
            agent=agent,
            payload=payload,
        )
        self.buffer.append(event)  # durable
        self._new_event.set()  # wake the sender (no-op if disconnected)
        return event


# --------------------------------------------------------------------------------------
# Daemon entry (stream) — singleton, signal-aware
# --------------------------------------------------------------------------------------


async def _run_daemon() -> int:
    if not is_enrolled():
        write_status("unenrolled", detail="no device identity — enroll first")
        log.error("not enrolled; run `sb_connector enroll` (or use the dashboard widget)")
        return 2
    if not acquire_lock():
        holder = lock_holder() or {}
        log.error("another connector daemon is live (host=%s pid=%s)",
                  holder.get("host"), holder.get("pid"))
        return 3

    # Set Claude Code retry env BEFORE spawning any forks — the workspace-summary
    # ask_all + future ask_agent forks both inherit these.
    _ensure_claude_retry_env()

    identity = load_identity()
    cfg = load_config()
    creds = load_cf_creds()
    conn = SBConnector(cfg["base_url"], identity, cf_access=cf_access_headers(creds))

    loop = asyncio.get_running_loop()
    run_task = asyncio.ensure_future(conn.run())
    # The producer runs alongside the connection (across reconnects): it watches the
    # Multiverse substrate and enqueues events, which buffer durably while disconnected.
    producer_task = asyncio.ensure_future(conn.run_producer())

    # Heartbeat refresher: rewrites the lock's ``heartbeat`` timestamp every
    # ``_HEARTBEAT_INTERVAL_S``. A dead-and-gone remote container (or a SIGKILLed
    # local daemon) will stop refreshing, and after ``_HEARTBEAT_STALE_S`` the next
    # daemon anywhere can take over — no manual `rm ~/.multiverse/secondbrain/lock`
    # required.
    async def _heartbeat() -> None:
        while True:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                refresh_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a disk error shouldn't kill the daemon
                log.exception("heartbeat refresh failed")
    heartbeat_task = asyncio.ensure_future(_heartbeat())

    def _shutdown() -> None:
        # Graceful: flag stop + send a proper 1000 close (which also unblocks a parked
        # recv), so run() breaks on its own. A watchdog force-cancels if the close hangs.
        async def _go() -> None:
            await conn.aclose()
            producer_task.cancel()
            heartbeat_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(run_task), timeout=8.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                run_task.cancel()

        asyncio.ensure_future(_go())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except (NotImplementedError, ValueError):
            pass

    try:
        await run_task
        return 0
    except asyncio.CancelledError:
        return 0
    finally:
        producer_task.cancel()
        heartbeat_task.cancel()
        await asyncio.gather(producer_task, heartbeat_task, return_exceptions=True)
        write_status("stopped", detail="daemon exited", device_id=identity.device_id)
        release_lock()


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Multiverse SecondBrain connector")
    sub = parser.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll", help="one-time device enrollment")
    e.add_argument("--base-url", default=load_config()["base_url"],
                   help="SB base URL, e.g. wss://secondbrain.silicon23.me")
    e.add_argument("--code", required=True, help="pairing code from `sb-pair`")
    e.add_argument("--fingerprint", required=True, help="pinned SB server fingerprint")
    e.add_argument("--name", default=current_host(), help="device name")
    e.add_argument("--workspace", action="append", default=[],
                   help="requested cwd to expose (repeatable)")

    sub.add_parser("stream", help="run the connector daemon (after enrollment)")
    sub.add_parser("status", help="print current status JSON")
    sub.add_parser("reload", help="stop the running daemon and start a fresh one "
                                  "(picks up new on-disk code after a git pull)")

    args = parser.parse_args()

    if args.cmd == "enroll":
        save_config(args.base_url)
        identity = asyncio.run(
            enroll(
                url=enroll_url(args.base_url),
                pairing_code=args.code,
                pinned_fingerprint=args.fingerprint,
                device_name=args.name,
                want=[str(Path(w).expanduser().resolve()) for w in args.workspace],
                key_path=device_key_path(),
                cf_access=cf_access_headers(load_cf_creds()),
            )
        )
        print(f"enrolled device_id={identity.device_id} "
              f"granted={identity.granted_workspaces}")
    elif args.cmd == "stream":
        raise SystemExit(asyncio.run(_run_daemon()))
    elif args.cmd == "status":
        print(json.dumps(read_status(), indent=2))
    elif args.cmd == "reload":
        # Import lazily so `enroll` / `status` / `stream` don't pull in sb_control's
        # Textual-adjacent chain (the widget imports sb_control which imports us; a
        # normal `stream` path shouldn't need the reverse trip).
        from . import sb_control
        result = sb_control.reload()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
