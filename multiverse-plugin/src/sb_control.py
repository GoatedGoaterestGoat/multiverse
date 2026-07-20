"""Monitor-facing control surface for the SecondBrain connector.

Headless (no Textual), in the spirit of :mod:`web_share`: the dashboard's SecondBrain
widget calls :func:`status` / :func:`start` / :func:`stop` / :func:`enroll` and renders
the result. All heavy lifting (the wire protocol, auth, the reconnect loop) lives in the
detached daemon (:mod:`src.sb_connector`); this module just:

* reports **status** the widget renders (enrolled? running? connected?),
* **starts/stops** the daemon as a **host-aware singleton** subprocess (survives the
  monitor quitting, so SB keeps perceiving; only one per device — SB supersedes a
  second connect from the same device),
* runs the one-time **enrollment** in-process (bounded, returns the device id/grants),
* stores/reads the **CF Access creds** + the **endpoint URL** the widget collects.

The device key, CF creds, endpoint config, status, lock, and daemon log all live under
``~/.multiverse/secondbrain/`` — outside the git repo, so secrets are never committable.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import sb_connector as C

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


class SBControlError(RuntimeError):
    """A control-plane failure surfaced to the widget (bad enroll, foreign-host stop…)."""


# --------------------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------------------

# States the widget renders:
#   unenrolled  — no device identity yet (need to enroll)
#   stopped     — enrolled, daemon not running
#   connecting  — daemon up, dialing / handshaking
#   connected   — authenticated stream live
#   reconnecting— dropped, backing off
#   locked      — SB is compacting; events parked
#   error       — last attempt failed
_LIVE_STATES = {"connecting", "connected", "reconnecting", "locked"}


def is_enrolled() -> bool:
    return C.is_enrolled()


def is_running() -> bool:
    """True if a live connector daemon holds the singleton lock (host-aware)."""
    return C.lock_holder() is not None


def status() -> dict:
    """A normalized status dict for the widget. Cross-checks the daemon's self-reported
    state against the live singleton lock, so a crashed daemon reads ``stopped``.

    Also computes ``code_stale`` — ``True`` when the on-disk connector code has been
    modified after the running daemon started (typical shape: user ran ``git pull``
    but the daemon is still running the old code because they restarted an agent's
    wrapper, not the SB daemon itself). The widget surfaces this with a warning +
    the ``r`` reload shortcut."""
    if not C.is_enrolled():
        return {
            "state": "unenrolled",
            "enrolled": False,
            "running": False,
            "detail": "",
            "base_url": C.load_config()["base_url"],
            "has_cf_creds": C.load_cf_creds() is not None,
            "code_stale": False,
        }

    identity = C.load_identity()
    running = is_running()
    st = C.read_status()
    state = st.get("state", "stopped")
    detail = st.get("detail", "")
    if not running and state in _LIVE_STATES:
        # Daemon died without writing a clean "stopped".
        state, detail = "stopped", "daemon not running"
    elif running and state in ("unenrolled", "stopped"):
        # Just launched, hasn't written a live state yet.
        state, detail = "connecting", "starting"

    # code_stale detection: read the lock's started_at + compare to the on-disk
    # code's latest mtime. A running daemon whose code predates a recent git pull
    # should be reloaded.
    code_stale = False
    if running:
        holder = C.lock_holder() or {}
        started = C._parse_iso(str(holder.get("started_at") or ""))
        if started:
            latest = C.code_mtime()
            if latest and latest > started:
                code_stale = True

    return {
        "state": state,
        "detail": detail,
        "enrolled": True,
        "running": running,
        "device_id": identity.device_id,
        "granted_workspaces": identity.granted_workspaces,
        "exposed_workspaces": sorted(_load_state_exposed()),
        "base_url": C.load_config()["base_url"],
        "has_cf_creds": C.load_cf_creds() is not None,
        "ts": st.get("ts", ""),
        "host": st.get("host", ""),
        "code_stale": code_stale,
    }


# --------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------


def start(base_url: str | None = None) -> dict:
    """Start the connector daemon if not already running. Idempotent."""
    if not C.is_enrolled():
        raise SBControlError("not enrolled — enroll this device with SecondBrain first")
    if base_url:
        C.save_config(base_url)
    if is_running():
        return status()

    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT") or str(_PLUGIN_ROOT)
    logf = open(C.log_path(), "ab", buffering=0)  # daemon stdout/err tail
    subprocess.Popen(
        [sys.executable, "-m", "src.sb_connector", "stream"],
        cwd=plugin_root,
        stdout=logf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # survive monitor exit; we stop it explicitly
        env=os.environ.copy(),
    )
    # Give it a moment to acquire the lock (or fail fast on a stale bind).
    deadline = time.time() + 6.0
    while time.time() < deadline:
        if is_running():
            break
        time.sleep(0.1)
    if not is_running():
        raise SBControlError(f"daemon did not start (see {C.log_path()})")
    return status()


def stop() -> dict:
    """Stop the connector daemon. Host-aware: refuses to (falsely) reap a daemon
    that's alive on a different host in a shared-``~/.multiverse`` deployment."""
    holder = C.lock_holder()
    if holder is None:
        return status()
    if holder.get("host") != C.current_host():
        raise SBControlError(
            f"daemon runs on another host ({holder.get('host')}); stop it there"
        )
    pid = int(holder.get("pid", 0))
    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 6.0
    while time.time() < deadline:
        if not is_running():
            break
        time.sleep(0.1)
    return status()


def reload() -> dict:
    """Stop the running daemon (if any) and start a fresh one — the intended way to
    pick up new on-disk code after a ``git pull``.

    Restarting the multiverse monitor or a specific agent's wrapper does NOT restart
    the SB daemon: the daemon is a device-level singleton with its own lifecycle.
    This helper does the stop→start atomically so the widget's ``r`` binding + the
    ``python -m src.sb_connector reload`` CLI don't require the caller to know that
    detail.
    """
    if is_running():
        stop()
    return start()


def save_device_name(name: str) -> None:
    """Persist the device name to the identity sidecar without a full re-enroll.

    Legacy sidecars from before device_name persistence come up with an empty
    ``device_name`` — the widget's Device name field then falls back to the
    ephemeral container hostname, which the user has to keep re-typing whenever
    they enroll from a new container. This lets the widget save whatever they
    type on connect so subsequent widget openings prefill correctly.

    No-op if the value equals what's already stored, or if the caller isn't
    enrolled yet (the enroll flow itself already writes device_name).
    """
    if not C.is_enrolled() or not name:
        return
    ident = C.load_identity()
    if ident.device_name == name:
        return
    ident.device_name = name
    ident.save(C.device_key_path())


# --------------------------------------------------------------------------------------
# Enrollment (one-time) + credential/config helpers the widget collects
# --------------------------------------------------------------------------------------


def enroll(
    *,
    base_url: str,
    code: str,
    fingerprint: str,
    name: str,
    want: list[str],
    cf_client_id: str | None = None,
    cf_client_secret: str | None = None,
) -> dict:
    """Run the ``/enroll`` handshake and persist the device identity.

    Blocking (bounded) — call from a worker thread. Saves the endpoint URL and any CF
    creds first so the enroll dial itself can pass the edge gate. Returns
    ``{device_id, granted_workspaces}``.

    **Additive grants** (2026-07-20): the sidecar's ``granted_workspaces`` is
    UNIONed with SB's enroll_ok response rather than overwritten. Fixes a real bug
    where re-enrolling from workspace B (with ``want=[B]``) would silently drop
    the previously-granted A. Now: SB responds with what it granted this call;
    we merge with whatever we already had. To REMOVE a workspace from grants,
    call ``unenroll_workspace`` — enroll is strictly additive.

    If the daemon is already running, it's reloaded here so it picks up the merged
    grants + any new exposed entries. If no daemon is running, this is a no-op; the
    caller will call start() next as normal.
    """
    if cf_client_id and cf_client_secret:
        C.save_cf_creds(cf_client_id, cf_client_secret)
    C.save_config(base_url)
    want_abs = [str(Path(w).expanduser().resolve()) for w in want if w.strip()]

    # Snapshot the pre-enroll grants so we can union afterward. Empty list on
    # first-ever enroll (no sidecar yet).
    prior_grants: list[str] = []
    if C.is_enrolled():
        try:
            prior_grants = list(C.load_identity().granted_workspaces)
        except Exception:  # noqa: BLE001
            prior_grants = []

    try:
        identity = asyncio.run(
            C.enroll(
                url=C.enroll_url(base_url),
                pairing_code=code,
                pinned_fingerprint=fingerprint,
                device_name=name,
                want=want_abs,
                key_path=C.device_key_path(),
                cf_access=C.cf_access_headers(C.load_cf_creds()),
            )
        )
    except Exception as exc:  # normalize to a widget-friendly error
        raise SBControlError(str(exc)) from exc

    # Additive merge: sidecar becomes union(prior grants, SB's response). C.enroll
    # already wrote the sidecar with just the SB response — rewrite additively.
    merged = sorted(set(prior_grants) | set(identity.granted_workspaces))
    if merged != list(identity.granted_workspaces):
        identity.granted_workspaces = merged
        identity.save(C.device_key_path())

    # Post-enroll: if a daemon is running, it's holding the pre-enroll identity in
    # memory (SBConnector.__init__ captured it at start time). Reload so it picks
    # up the merged sidecar. If no daemon is running, this is a no-op.
    if is_running():
        reload()

    return {
        "device_id": identity.device_id,
        "granted_workspaces": identity.granted_workspaces,
    }


# --------------------------------------------------------------------------------------
# Per-workspace expose / unexpose / un-enroll (2026-07-20 refactor)
# --------------------------------------------------------------------------------------


def _load_state_exposed() -> set[str]:
    """Current exposed set from state.json (list of resolved cwd strings).

    Legacy state.json without an ``exposed`` field → default to the sidecar's
    granted_workspaces (matches pre-refactor behavior: everything granted is
    implicitly observed).
    """
    st = C.load_conn_state()
    return set(st.get("exposed", []))


def is_exposed(cwd: str) -> bool:
    """True if ``cwd`` is currently in the exposed set."""
    return str(Path(cwd).expanduser().resolve()) in _load_state_exposed()


def expose_workspace(cwd: str) -> dict:
    """Add ``cwd`` to the exposed set so the daemon starts observing it.

    Refuses if the workspace isn't granted — the widget's ``Enter`` path should
    route to enroll first for that case. Starts the daemon if it isn't running;
    reloads it if it is, so the SBConnector's in-memory ``_exposed`` reflects the
    new state.
    """
    if not C.is_enrolled():
        raise SBControlError("not enrolled — enroll this device with SecondBrain first")
    cwd_abs = str(Path(cwd).expanduser().resolve())
    if cwd_abs not in C.load_identity().granted_workspaces:
        raise SBControlError(
            f"workspace not granted by SecondBrain: {cwd_abs} — enroll first"
        )
    st = C.load_conn_state()
    exposed = set(st.get("exposed", []))
    if cwd_abs not in exposed:
        exposed.add(cwd_abs)
        st["exposed"] = sorted(exposed)
        C.save_conn_state(st)
    if is_running():
        reload()
    else:
        start()
    return status()


def unexpose_workspace(cwd: str) -> dict:
    """Remove ``cwd`` from the exposed set.

    Never stops the daemon — even if this leaves ``exposed`` empty, the daemon
    stays running (idle). Rationale: the user's mental model is that ``d``
    disconnects THIS workspace only; the daemon is a device-level detail. Auto-
    stopping on last-unexpose was surprising when other workspaces were still
    granted-but-not-observed (they might want to re-expose one immediately
    without needing to re-start the daemon).

    To fully stop the daemon, use ``stop()`` (Shift+D in the widget). Reloads on
    unexpose so the in-memory ``SBConnector._exposed`` reflects the change.
    """
    if not C.is_enrolled():
        return status()
    cwd_abs = str(Path(cwd).expanduser().resolve())
    st = C.load_conn_state()
    exposed = set(st.get("exposed", []))
    if cwd_abs not in exposed:
        return status()
    exposed.discard(cwd_abs)
    st["exposed"] = sorted(exposed)
    C.save_conn_state(st)
    if is_running():
        reload()  # daemon picks up new exposed set; stays running even if empty
    return status()


def unenroll_workspace(cwd: str) -> dict:
    """Remove ``cwd`` from the sidecar's granted_workspaces (client-side) and from
    the exposed set. SB's server-side grant is left untouched — a subsequent
    re-enroll can re-add it without a fresh pairing code if SB still trusts it,
    but from the client's perspective this workspace is now un-enrolled.

    Never stops the daemon (same rationale as :func:`unexpose_workspace`). Use
    :func:`stop` for device-level shutdown.
    """
    if not C.is_enrolled():
        return status()
    cwd_abs = str(Path(cwd).expanduser().resolve())
    ident = C.load_identity()
    changed = False
    if cwd_abs in ident.granted_workspaces:
        ident.granted_workspaces = [w for w in ident.granted_workspaces if w != cwd_abs]
        ident.save(C.device_key_path())
        changed = True
    st = C.load_conn_state()
    exposed = set(st.get("exposed", []))
    if cwd_abs in exposed:
        exposed.discard(cwd_abs)
        st["exposed"] = sorted(exposed)
        C.save_conn_state(st)
        changed = True
    if changed and is_running():
        reload()
    return status()


def get_base_url() -> str:
    return C.load_config()["base_url"]


def set_base_url(base_url: str) -> None:
    C.save_config(base_url)


def has_cf_creds() -> bool:
    return C.load_cf_creds() is not None


def get_cf_client_id() -> str:
    """Stored/env CF Access client id (not secret) for widget prefill; "" if unset."""
    creds = C.load_cf_creds()
    return creds["client_id"] if creds else ""


def get_device_name() -> str:
    """The device name the user chose at enroll time, for widget prefill on subsequent
    remotes in a shared-``~/.multiverse`` deployment. Empty if unenrolled or if the
    sidecar predates the ``device_name`` field."""
    if not C.is_enrolled():
        return ""
    return C.load_identity().device_name


def save_cf_creds(client_id: str, client_secret: str) -> None:
    C.save_cf_creds(client_id, client_secret)


def clear_cf_creds() -> None:
    C.clear_cf_creds()


def current_workspace_granted(cwd: str) -> bool:
    """True if ``cwd`` is among the device's granted workspaces (exposed to SB)."""
    if not C.is_enrolled():
        return False
    cwd_abs = str(Path(cwd).expanduser().resolve())
    return cwd_abs in C.load_identity().granted_workspaces
