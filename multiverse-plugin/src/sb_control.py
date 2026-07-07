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
    state against the live singleton lock, so a crashed daemon reads ``stopped``."""
    if not C.is_enrolled():
        return {
            "state": "unenrolled",
            "enrolled": False,
            "running": False,
            "detail": "",
            "base_url": C.load_config()["base_url"],
            "has_cf_creds": C.load_cf_creds() is not None,
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
    """Run the one-time ``/enroll`` handshake and persist the device identity.

    Blocking (bounded) — call from a worker thread. Saves the endpoint URL and any CF
    creds first so the enroll dial itself can pass the edge gate. Returns
    ``{device_id, granted_workspaces}``.
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
