"""Ephemeral, per-workspace web sharing of the monitor dashboard.

This is the Option-C remote-access MVP: a dashboard-triggered share that
makes *one workspace's* monitor reachable from any browser, then tears
itself down. It is deliberately ephemeral and per-cwd — the user presses
a key in the TUI, this module spins up the stack, and a second press (or
monitor quit) spins it down.

Stack (all co-located on the box — every coordination primitive is local
tmux + local files under one ``$MULTIVERSE_ROOT``, so this CANNOT drive
sessions on a remote host):

    browser (xterm.js, served by ttyd)
        | wss, TLS terminated by the tunnel
    tunnel connector (outbound-only)            <- TunnelProvider, pluggable
        | http://127.0.0.1:<port>
    ttyd  -W  -i 127.0.0.1  -c mv:<token>       <- loopback-only, auth gate
        | local tmux
    tmux new-session -A -s mvweb-<hash> -c <cwd> bin/multiverse
        | a *dedicated* fresh monitor scoped to <cwd>

Why a dedicated web monitor (not the live local TUI): the remote user
gets independent navigation, and two monitors on one cwd already coexist
by design (cross-monitor O_EXCL on spawn-requests / compaction). Because
ttyd gives that monitor a real controlling PTY, its ``_attach_to``
(``with self.suspend(): tmux attach-session``) works in the browser —
the exact thing ``textual serve`` cannot do.

AUTHENTICATION (see the project docs / the WebShare design note):
  - ttyd is bound to 127.0.0.1 ONLY; the tunnel is the sole ingress and
    always terminates TLS.
  - The gate must cover the WEBSOCKET upgrade, not just the HTML page
    (the terminal stream *is* the WS). We never hand-roll a WS auth
    proxy; each backbone uses a turnkey gate that already covers WS:
      * QuickTunnel  -> ttyd basic-auth (``-c mv:<token>``). Browsers
        replay cached basic-auth creds on same-origin requests INCLUDING
        the WS handshake, so the token gates the terminal too. Over the
        tunnel's TLS this is acceptable for an ephemeral single-user
        share. Defense in depth: loopback bind + short-lived share +
        revoke-on-stop.
      * Cloudflare Access / oauth2-proxy backbones authenticate at the
        edge BEFORE traffic reaches the tunnel (covers WS), giving real
        per-user SSO identity. (providers added once the backbone is
        chosen — see TunnelProvider subclasses below.)
  - The per-share token is minted at trigger time, surfaced to the user
    in the TUI, and invalidated when the share stops (ttyd dies + a fresh
    token is required next time).

This module is HEADLESS (no Textual): the monitor calls start()/stop()/
status() and renders the result. All process handles are tracked on disk
so a monitor restart can still find (and stop) a running share.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from hashlib import blake2b
from pathlib import Path
from typing import Optional, Protocol

from .paths import multiverse_dir

# tmux session name prefix for the dedicated web monitor.
WEB_TMUX_PREFIX = "mvweb-"
# How long to wait for the tunnel provider to report a public URL.
TUNNEL_URL_TIMEOUT = 30.0


def _shares_dir() -> Path:
    return multiverse_dir() / "web_shares"


def share_id_for(cwd: str) -> str:
    """Stable short id for a workspace (one share per cwd)."""
    resolved = str(Path(cwd).expanduser().resolve())
    return blake2b(resolved.encode(), digest_size=6).hexdigest()


def _share_dir(share_id: str) -> Path:
    return _shares_dir() / share_id


def _record_path(share_id: str) -> Path:
    return _share_dir(share_id) / "record.json"


def web_tmux_name(share_id: str) -> str:
    return f"{WEB_TMUX_PREFIX}{share_id}"


@dataclass
class WebShare:
    """On-disk record of a running share. Serialized to record.json."""

    share_id: str
    cwd: str
    provider: str
    token: str
    ttyd_port: int
    ttyd_pid: int
    tunnel_pid: int
    web_tmux: str
    url: str
    created_at: float
    path: str = ""  # cloudflare slot label (URL path segment), "" if none


# ---------------------------------------------------------------------------
# Tunnel providers (the pluggable identity/exposure backbone)
# ---------------------------------------------------------------------------


class TunnelProvider(Protocol):
    """Brings an authenticated HTTPS+WS session from any browser to the
    loopback ttyd. Implementations spawn an outbound-only connector and
    report the public URL."""

    name: str
    edge_authenticated: bool  # True if the tunnel enforces auth at the edge (Access)

    def local_port(self) -> Optional[int]:
        """Fixed loopback port the connector routes to (ttyd must bind it so
        it matches the tunnel's ingress), or None to let us pick any free
        port (quick tunnel points ``--url`` at whatever we chose)."""
        ...

    def start(self, local_port: int, log_path: Path) -> tuple[int, str]:
        """Spawn the connector for ``http://127.0.0.1:<local_port>`` and
        return ``(pid, public_url)``. Must not block beyond URL discovery."""
        ...


_TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# cloudflared logs one of these once a named tunnel's edge connections are up.
_CF_READY_RE = re.compile(
    r"Registered tunnel connection|Connection [0-9a-f-]+ registered|"
    r"connection .*registered",
    re.IGNORECASE,
)


class QuickTunnelProvider:
    """Zero-setup, self-contained backbone: a cloudflared *quick tunnel*.

    ``cloudflared tunnel --url http://127.0.0.1:<port>`` dials out and
    prints a random ``https://<slug>.trycloudflare.com`` URL. No account,
    no domain. Identity is provided by the ttyd basic-auth token (see the
    module-level AUTH note); the quick-tunnel URL is public, so the token
    is the gate. Good for an ephemeral single-user share; upgrade to the
    Access / oauth2-proxy providers for real SSO.
    """

    name = "quick"
    edge_authenticated = False  # public URL -> the ttyd token is the ONLY gate

    def local_port(self) -> Optional[int]:
        return None  # any free port; --url points cloudflared at it

    def start(self, local_port: int, log_path: Path) -> tuple[int, str]:
        if not _which("cloudflared"):
            raise WebShareError(
                "cloudflared not found. Install it (brew install cloudflared "
                "/ apt-get install cloudflared) or pick a different backbone."
            )
        # Truncate (wb): the share dir is reused across stop/start for this
        # cwd, so an APPENDED log would let _scrape_url match a PREVIOUS run's
        # stale trycloudflare URL sitting at the top of the file. A fresh log
        # means the scraper only ever sees this run's URL.
        logf = open(log_path, "wb", buffering=0)
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--grace-period", "3s",
             "--url", f"http://127.0.0.1:{local_port}"],
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survive monitor exit; we kill explicitly
        )
        url = _scrape_url(log_path, _TRYCLOUDFLARE_RE, TUNNEL_URL_TIMEOUT, proc)
        return proc.pid, url


class NamedCloudflareProvider:
    """The device-auth backbone: a *named* cloudflared tunnel on your own
    domain, fronted by Cloudflare Access.

    Cryptographic device-only auth — mutual-TLS client certificate (which
    you can generate from your SSH key) and/or SSO — is enforced at
    Cloudflare's EDGE by a standing Access policy you configure ONCE (see
    docs/remote-access.md). The browser's TLS handshake terminates at the
    edge, which is the only place a client cert can be demanded; the quick
    tunnel cannot do this, which is why device auth requires a named tunnel
    + a zone. This provider just runs the connector for the pre-provisioned
    hostname — the per-share ttyd token (``-c mv:<token>``) is the second
    factor on top of the edge's device check.

    NOTE on cost: the Access POLICY is your choice in the Cloudflare
    dashboard, and this connector is identical either way. Free Zero Trust
    supports SSO / one-time-PIN identity policies (strong per-user auth, any
    browser, no install). True device-binding via bring-your-own-CA *mTLS*
    requires a PAID plan (Business+) — the free plan can't use a custom CA in
    Access. Same connector; pick the policy that matches your plan.

    Config via env (exported once into the monitor's environment, e.g. from
    ``$MULTIVERSE_ROOT/web-tunnel.env``):
        MULTIVERSE_WEB_TUNNEL_NAME  named-tunnel name or UUID (``cloudflared
                                    tunnel run <name>``)
        MULTIVERSE_WEB_HOSTNAME     public hostname your ingress maps to the
                                    local port (the Access app protects it)
        MULTIVERSE_WEB_LOCAL_PORT   loopback port that hostname routes to;
                                    ttyd binds here so it matches ingress
                                    (default 7681)
        MULTIVERSE_WEB_TUNNEL_CONFIG  absolute path to cloudflared config.yml,
                                    passed as ``--config`` (which must precede
                                    ``run``). Set this where the monitor's HOME
                                    differs from the shell that ran ``tunnel
                                    login``/``create`` (e.g. Beaker) — ``run``
                                    otherwise only auto-discovers
                                    ~/.cloudflared/config.yml under the
                                    monitor's HOME. ``credentials-file`` inside
                                    it must also be an absolute path.
        MULTIVERSE_WEB_TOKEN        whether this (edge-authenticated) backbone
                                    ALSO requires a ttyd basic-auth token as a
                                    second factor: on/1/true to enable; default
                                    OFF (Access is the gate). The quick backbone
                                    always uses a token regardless of this.
        MULTIVERSE_WEB_SLOTS        optional "label:port label:port ..." — path
                                    slots under the one hostname for concurrent
                                    multi-workspace shares (<host>/<label>/, ttyd
                                    served with -b /<label>). The cloudflared
                                    ingress needs a matching rule per slot. Unset
                                    -> one share at a time on
                                    MULTIVERSE_WEB_LOCAL_PORT.
    """

    name = "cloudflare"  # named-tunnel + Access; policy (SSO/mTLS) set in CF
    edge_authenticated = True  # Cloudflare Access gates every request at the edge

    def __init__(self) -> None:
        self.tunnel = os.environ.get("MULTIVERSE_WEB_TUNNEL_NAME", "").strip()
        self.hostname = os.environ.get("MULTIVERSE_WEB_HOSTNAME", "").strip()
        self.port = int(os.environ.get("MULTIVERSE_WEB_LOCAL_PORT", "7681"))
        # Optional absolute config.yml path for HOME-divergent deployments.
        self.config = os.environ.get("MULTIVERSE_WEB_TUNNEL_CONFIG", "").strip()

    def local_port(self) -> Optional[int]:
        # Must match the cloudflared ingress rule, so it's fixed, not random.
        return self.port

    def start(self, local_port: int, log_path: Path) -> tuple[int, str]:
        if not _which("cloudflared"):
            raise WebShareError("cloudflared not found (needed for the tunnel).")
        if not (self.tunnel and self.hostname):
            raise WebShareError(
                "cloudflare backbone needs MULTIVERSE_WEB_TUNNEL_NAME and "
                "MULTIVERSE_WEB_HOSTNAME set (one-time setup — see "
                "docs/remote-access.md)."
            )
        logf = open(log_path, "wb", buffering=0)  # fresh log per share (see Quick)
        # `cloudflared tunnel run <name>` only auto-discovers config.yml under
        # the running process's ~/.cloudflared. When the monitor's HOME differs
        # from the shell that set the tunnel up (the Beaker trap), pass an
        # absolute --config — which MUST come BEFORE the `run` subcommand.
        # --grace-period shortens cloudflared's drain-on-SIGTERM so stopping a
        # share releases it promptly (default is 30s). Must precede `run`.
        cmd = ["cloudflared", "tunnel", "--grace-period", "3s"]
        if self.config:
            cmd += ["--config", self.config]
        cmd += ["run", self.tunnel]
        proc = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait until the edge connections register, then hand back the
        # (already-known) hostname — no URL to scrape for a named tunnel.
        _wait_ready(log_path, _CF_READY_RE, TUNNEL_URL_TIMEOUT, proc)
        url = self.hostname
        if not url.startswith("http"):
            url = "https://" + url
        return proc.pid, url


# Selected by $MULTIVERSE_WEB_TUNNEL; defaults to the self-contained path.
# "cloudflare" is the named-tunnel + Access backbone (auth policy — free SSO
# or paid mTLS — is configured in Cloudflare, not here).
_PROVIDERS: dict[str, type] = {
    "quick": QuickTunnelProvider,
    "cloudflare": NamedCloudflareProvider,
    "cloudflare-mtls": NamedCloudflareProvider,  # back-compat alias
}


def _select_provider(name: Optional[str]) -> TunnelProvider:
    key = name or os.environ.get("MULTIVERSE_WEB_TUNNEL", "quick")
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise WebShareError(
            f"unknown tunnel backbone {key!r}; available: {sorted(_PROVIDERS)}"
        )
    return cls()


def configured_provider_name() -> str:
    """Name of the backbone that a new share would use (for display)."""
    return os.environ.get("MULTIVERSE_WEB_TUNNEL", "quick")


def token_default() -> bool:
    """Default for whether an edge-authenticated backbone (cloudflare) ALSO uses
    a ttyd token, from MULTIVERSE_WEB_TOKEN. Off unless explicitly enabled. The
    w-panel toggle overrides this per share; the quick backbone always uses a
    token regardless (it is the only gate)."""
    return os.environ.get("MULTIVERSE_WEB_TOKEN", "").strip().lower() in (
        "1", "on", "true", "yes",
    )


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")


def parse_slots() -> list[tuple[str, int]]:
    """Parse MULTIVERSE_WEB_SLOTS (``label:port label:port ...``) into
    ``(label, port)`` pairs. Each label is a path segment under the one
    cloudflare hostname (``https://<host>/<label>/``) routed to a ttyd on
    ``port``; the cloudflared ingress must have a matching rule per slot.
    Empty (unset) -> single fixed-port behavior (one cloudflare share)."""
    raw = os.environ.get("MULTIVERSE_WEB_SLOTS", "").strip()
    slots: list[tuple[str, int]] = []
    for tok in raw.split():
        label, sep, port = tok.rpartition(":")
        label = _slug(label)
        if sep and label and port.isdigit():
            slots.append((label, int(port)))
    return slots


def _active_cloudflare_slot_labels() -> set[str]:
    """Slot labels currently held by LIVE cloudflare shares (any workspace)."""
    used: set[str] = set()
    d = _shares_dir()
    if not d.is_dir():
        return used
    for sub in d.iterdir():
        rec = _read_record(sub.name)
        if (
            rec is not None
            and rec.provider == "cloudflare"
            and rec.path
            and _pid_alive(rec.ttyd_pid)
            and _pid_alive(rec.tunnel_pid)
        ):
            used.add(rec.path.strip("/"))
    return used


# ---------------------------------------------------------------------------
# Lifecycle: start / stop / status
# ---------------------------------------------------------------------------


class WebShareError(RuntimeError):
    pass


def status(cwd: str) -> Optional[WebShare]:
    """Return the live share for ``cwd``, or None. Reaps a stale record
    whose processes have died."""
    rec = _read_record(share_id_for(cwd))
    if rec is None:
        return None
    if not (_pid_alive(rec.ttyd_pid) and _pid_alive(rec.tunnel_pid)):
        # A leg died — clean up so the dashboard doesn't show a dead share.
        stop(cwd)
        return None
    return rec


def is_active(cwd: str) -> bool:
    return status(cwd) is not None


def start(
    cwd: str,
    *,
    provider: Optional[str] = None,
    writable: bool = True,
    use_token: Optional[bool] = None,
) -> WebShare:
    """Bring up the share stack for ``cwd`` and return its record.

    Idempotent: if a live share already exists it is returned unchanged.
    Requires ``ttyd`` and the chosen tunnel connector on PATH, and the
    same env parity the monitor runs with (MULTIVERSE_ROOT /
    MULTIVERSE_PLUGIN_ROOT / CLAUDE_CONFIG_DIR) — inherited automatically
    because the monitor calls this in-process.
    """
    existing = status(cwd)
    if existing is not None:
        return existing

    if not _which("ttyd"):
        raise WebShareError(
            "ttyd not found. Install it (brew install ttyd / apt-get install "
            "ttyd / static binary) — it is the browser terminal bridge."
        )
    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT")
    if not plugin_root:
        raise WebShareError(
            "MULTIVERSE_PLUGIN_ROOT is unset; cannot launch the web monitor. "
            "Start the monitor via bin/multiverse so env parity holds."
        )

    # Select the backbone up front: a named tunnel pins ttyd to a fixed
    # loopback port (so it matches the cloudflared ingress); the quick
    # tunnel lets us grab any free port and points --url at it.
    tun = _select_provider(provider)

    cwd_abs = str(Path(cwd).expanduser().resolve())
    sid = share_id_for(cwd_abs)
    sdir = _share_dir(sid)
    sdir.mkdir(parents=True, exist_ok=True)

    # Token-as-second-factor policy. For a backbone with NO edge auth (quick),
    # the ttyd token is the ONLY gate, so it is always on. For an edge-
    # authenticated backbone (cloudflare/Access), the token is an OPTIONAL second
    # factor: honor an explicit per-share `use_token` (the w-panel toggle), else
    # fall back to the MULTIVERSE_WEB_TOKEN default (off).
    if getattr(tun, "edge_authenticated", False):
        if use_token is None:
            use_token = token_default()
    else:
        use_token = True
    token = secrets.token_urlsafe(18) if use_token else ""

    # Slot assignment for the cloudflare backbone (path-based multi-workspace).
    # MULTIVERSE_WEB_SLOTS="label:port ..." declares path slots under the one
    # hostname; each live cloudflare share takes one. The path defaults to the
    # slot named after this workspace, else the first free slot. ttyd then serves
    # under that base-path (-b /<label>) and the URL is <hostname>/<label>/.
    # Unset -> single fixed-port behavior (one cloudflare share at a time).
    base_path = ""
    slot_label = ""
    slot_port: Optional[int] = None
    if getattr(tun, "edge_authenticated", False):
        slots = parse_slots()
        if slots:
            used = _active_cloudflare_slot_labels()
            free = [(lbl, p) for (lbl, p) in slots if lbl not in used]
            if not free:
                raise WebShareError(
                    f"all {len(slots)} cloudflare web slots are in use "
                    "(MULTIVERSE_WEB_SLOTS) — stop one first or add a slot."
                )
            want = _slug(Path(cwd_abs).name)
            slot_label, slot_port = next(
                ((l, p) for (l, p) in free if l == want), free[0]
            )
            base_path = "/" + slot_label

    fixed_port = tun.local_port()
    if slot_port is not None:
        port = slot_port            # the slot's port (matches its ingress rule)
    elif fixed_port is not None:
        port = fixed_port
    else:
        port = _free_loopback_port()
    web_tmux = web_tmux_name(sid)
    launcher = str(Path(plugin_root) / "bin" / "multiverse")

    # ttyd serves xterm.js and runs the monitor DIRECTLY as its PTY child —
    # deliberately NOT inside a `tmux new-session`. If the monitor ran inside
    # tmux, its attach path (`with self.suspend(): tmux attach-session`) would
    # be a NESTED attach ($TMUX set) and tmux would refuse it — the browser
    # would never attach (UI just refreshes). Running bare makes web attach
    # behave exactly like local attach (single layer; normal Ctrl-b d detach).
    # Trade-off: ttyd respawns the monitor on browser reconnect — fine, the
    # monitor is a stateless viewer and the claude sessions it manages persist.
    # Bound to loopback; the tunnel is the only ingress. When use_token is set
    # (always for non-edge-auth backbones; opt-in for cloudflare), ttyd's
    # basic-auth (-c) gates the HTTP page AND the WS upgrade.
    ttyd_cmd = [
        "ttyd",
        "-i", "127.0.0.1",
        "-p", str(port),
        "-t", "titleFixed=multiverse",
        # Ping every 30s: Cloudflare drops idle WebSockets at ~100s, but ttyd's
        # default ping is 300s — without this an idle terminal keeps dropping.
        "-P", "30",
    ]
    if base_path:
        ttyd_cmd += ["-b", base_path]  # serve under the slot's sub-path
    if use_token:
        ttyd_cmd += ["-c", f"mv:{token}"]
    if writable:
        ttyd_cmd.append("-W")
    ttyd_cmd.append(launcher)

    # Scope the in-browser monitor to the shared workspace via env: the ttyd
    # child's process cwd is the plugin root, not the workspace, so we can't
    # rely on bin/multiverse's `$PWD` default. (bin/multiverse respects a
    # pre-set MULTIVERSE_ORIG_CWD.)
    child_env = {**os.environ, "MULTIVERSE_ORIG_CWD": cwd_abs}
    ttyd_log = open(sdir / "ttyd.log", "wb", buffering=0)  # fresh per share
    ttyd_proc = subprocess.Popen(
        ttyd_cmd,
        stdout=ttyd_log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=child_env,
    )
    # Give ttyd a beat to bind (or fail fast).
    time.sleep(0.3)
    if ttyd_proc.poll() is not None:
        raise WebShareError(
            f"ttyd exited immediately (see {sdir / 'ttyd.log'})."
        )

    try:
        tunnel_pid, url = tun.start(port, sdir / "tunnel.log")
    except Exception:
        _kill(ttyd_proc.pid)
        _kill_tmux(web_tmux)
        raise

    if base_path:
        url = url.rstrip("/") + base_path + "/"  # <hostname>/<label>/

    rec = WebShare(
        share_id=sid,
        cwd=cwd_abs,
        provider=tun.name,
        token=token,
        ttyd_port=port,
        ttyd_pid=ttyd_proc.pid,
        tunnel_pid=tunnel_pid,
        web_tmux=web_tmux,
        url=url,
        created_at=time.time(),
        path=slot_label,
    )
    _write_record(rec)
    return rec


def stop(cwd: str) -> None:
    """Tear down the share for ``cwd`` and revoke its credential.

    Best-effort and idempotent: kills the tunnel, ttyd, and the web
    monitor tmux session, then removes the record. The token dies with
    ttyd; the next share mints a fresh one.
    """
    sid = share_id_for(cwd)
    rec = _read_record(sid)
    if rec is not None:
        _kill(rec.tunnel_pid)
        _kill(rec.ttyd_pid)
        _kill_tmux(rec.web_tmux)
    else:
        # No record but be defensive: kill a possibly-orphaned web tmux.
        _kill_tmux(web_tmux_name(sid))
    try:
        _record_path(sid).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_record(share_id: str) -> Optional[WebShare]:
    p = _record_path(share_id)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    try:
        return WebShare(**data)
    except TypeError:
        return None


def _write_record(rec: WebShare) -> None:
    p = _record_path(rec.share_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(rec), indent=2))
    os.replace(tmp, p)  # atomic


def _free_loopback_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _scrape_url(
    log_path: Path,
    pattern: re.Pattern,
    timeout: float,
    proc: subprocess.Popen,
) -> str:
    """Poll the connector's log for its public URL until it appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise WebShareError(
                f"tunnel connector exited before reporting a URL (see {log_path})."
            )
        try:
            m = pattern.search(log_path.read_text())
        except OSError:
            m = None
        if m:
            return m.group(0)
        time.sleep(0.25)
    _kill(proc.pid)
    raise WebShareError(
        f"tunnel did not report a URL within {timeout:.0f}s (see {log_path})."
    )


def _wait_ready(
    log_path: Path,
    pattern: re.Pattern,
    timeout: float,
    proc: subprocess.Popen,
) -> None:
    """Block until ``pattern`` appears in the connector log (a named tunnel's
    edge connections registering). Raises on early exit / timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise WebShareError(
                f"tunnel connector exited before becoming ready (see {log_path})."
            )
        try:
            if pattern.search(log_path.read_text()):
                return
        except OSError:
            pass
        time.sleep(0.25)
    _kill(proc.pid)
    raise WebShareError(
        f"tunnel was not ready within {timeout:.0f}s (see {log_path})."
    )


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


def _kill(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _kill_tmux(session: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _which(binary: str) -> bool:
    from shutil import which

    return which(binary) is not None
