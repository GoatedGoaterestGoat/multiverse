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

    If the daemon is already running when this is called (RE-enrollment to add a new
    workspace grant from an already-enrolled device), it's reloaded here — otherwise
    the running daemon holds the OLD identity/grants in memory and never first-connects
    the newly-added workspace. Callers don't have to remember; a single ``enroll()``
    both persists the new identity AND makes the daemon see it.
    """
    if cf_client_id and cf_client_secret:
        C.save_cf_creds(cf_client_id, cf_client_secret)
    C.save_config(base_url)
    want_abs = [str(Path(w).expanduser().resolve()) for w in want if w.strip()]
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

    # Post-enroll: if a daemon is running, it's holding the pre-enroll identity in
    # memory (SBConnector.__init__ captured it at start time). Reload so it picks up
    # the freshly-written sidecar — otherwise a newly-granted workspace never gets
    # its first-connect fired. If no daemon is running, this is a no-op; the caller
    # will call start() next as normal.
    if is_running():
        reload()

    return {
        "device_id": identity.device_id,
        "granted_workspaces": identity.granted_workspaces,
    }


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
