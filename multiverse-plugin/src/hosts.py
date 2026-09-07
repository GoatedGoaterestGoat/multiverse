"""Machine identity and proof-of-life for a shared ``MULTIVERSE_ROOT``.

Multiverse's coordination surface is the filesystem, so a workspace whose
``~/.multiverse`` lives on network storage is already shared by every remote
that mounts it: the same sessions, inboxes and registries, seen from several
machines at once. What is NOT shared is a tmux server. Each remote can only
see, sign, and type into its own — so "``tmux has-session`` says no" means
"not here", never "not anywhere", and every guard that forgets the difference
reaps or duplicates a session that is alive on another remote.

Two questions follow from that, and this module answers both:

* **Whose is it?** ``current_host()`` — stamped into a session at creation
  and compared later. The answer has to be stable across reboots, networks
  and container restarts, which a hostname is not.
* **Is it still there?** ``heartbeat_age()`` — only a machine that can see a
  session running refreshes its heartbeat, so a fresh one means some machine
  still can. That answer needs no identity at all, which matters because
  identity is the part that breaks: two containers routinely report the same
  hostname, and no comparison of host ids can then tell them apart.

A stale heartbeat is evidence of uncertainty, not permission to act: it
never authorizes taking over or reaping a foreign session, because "the
share went quiet" and "the machine is gone" look identical from here. Only
the user, through the dashboard's explicit force-kill, decides that a remote
is really gone.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

#: How often a machine that can see a session refreshes its heartbeat. Small,
#: because this interval also bounds the delay before a genuinely dead LOCAL
#: session can be reaped: until its last heartbeat goes stale, a host cannot
#: tell its own moments-old signal from another machine's.
HEARTBEAT_INTERVAL_SECONDS = 15.0

#: Four times the write interval, which tolerates one missed write plus clock
#: skew between machines sharing the mount. Below that, a slow tick on a busy
#: remote reads as death; far above it, a preempted container stays "alive" on
#: the dashboard long enough to matter.
STALE_HEARTBEAT_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 4

HEARTBEAT_FILENAME = "heartbeat"

#: ``host_state`` vocabulary, shared by the dashboard, the MCP registry and
#: the SecondBrain connector so all three describe a row the same way.
HOST_LOCAL = "local"
HOST_FOREIGN = "foreign"
HOST_FOREIGN_STALE = "foreign_stale"


def current_host() -> str:
    """Identity of the machine this process is running on.

    ``MULTIVERSE_HOST_ID`` wins when set, because a hostname is not a stable
    machine identity and this value decides whether a session looks foreign.
    macOS reports a different one depending on the network it is attached to
    (mDNS ``Silicon23.local`` vs DHCP ``MacBookPro.lan`` for the same laptop),
    and a container usually gets a fresh random name on every start — so with
    bare hostnames a session can look foreign on the very machine running it,
    or two different remotes can look like the same one. Pinning one id per
    machine removes both failures.

    ``create_session`` forwards the resolved value into each session's tmux
    environment, so a session records the id its own host was configured
    with rather than whatever the dashboard process happened to resolve.

    This rule is deliberately duplicated in two bootstrap paths that run
    before, or without, this package being importable — they must keep
    matching it:

      * ``bin/multiverse-codex-session`` — ``${MULTIVERSE_HOST_ID:-$(hostname)}``
      * ``plugins/multiverse-codex/scripts/session-hook.py`` — same rule, kept
        free of the ``sessions`` import tree

    Change the rule here and both need the same change, or a wrapper will
    decide a session is foreign while the dashboard decides it is ours.
    """
    explicit = os.environ.get("MULTIVERSE_HOST_ID", "").strip()
    if explicit:
        return explicit
    return socket.gethostname()


def host_is_foreign(host: str | None) -> bool:
    """True when ``host`` names a machine other than this one.

    An unset host — a session predating the field, or ``current_host()``
    failing at creation — counts as local, which is what every guard in
    ``sessions`` assumed before this helper existed.
    """
    return bool(host) and host != current_host()


def heartbeat_path(session_dir: Path) -> Path:
    return Path(session_dir) / HEARTBEAT_FILENAME


def touch_heartbeat(session_dir: Path) -> None:
    """Record that this session is alive, and WHICH machine can see that.

    Only a machine that can currently SEE the session running may write one —
    its own tmux server holds it, or it IS the session's wrapper process. That
    rule is what gives the file meaning: a fresh heartbeat says "some machine
    can see this session alive", which is exactly the fact no amount of local
    tmux inspection can establish about another remote.

    The recorded host id is diagnostic only. Reading it to decide whose
    heartbeat this is does not work: two containers routinely report the same
    hostname, and then every machine finds its own id in every heartbeat and
    dismisses real proof of life as its own echo. Ask "can I see this session
    myself?" instead — if not, the heartbeat is not ours, whatever it says.

    Failures are swallowed: a session must never die because its proof of life
    could not be written.
    """
    path = heartbeat_path(session_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(current_host())
    except OSError:
        pass


def heartbeat_host(session_dir: Path) -> str | None:
    """Which machine last vouched for this session, or None if unrecorded."""
    try:
        signature = heartbeat_path(session_dir).read_text().strip()
    except OSError:
        return None
    return signature or None


def heartbeat_age(session_dir: Path, *, now: float | None = None) -> float | None:
    """Seconds since this session last proved it was alive, or None.

    None means "never recorded one": a session started before heartbeats
    existed, or one whose host cannot write them. ``heartbeat_is_stale``
    folds that into stale — see there for why — but the raw distinction
    survives here for callers that want to say "unknown" rather than "old".
    """
    try:
        mtime = heartbeat_path(session_dir).stat().st_mtime
    except OSError:
        return None
    return max((time.time() if now is None else now) - mtime, 0.0)


def heartbeat_is_stale(
    session_dir: Path, *, now: float | None = None, limit: float | None = None
) -> bool:
    """True when this session has not proved itself alive recently enough.

    A missing heartbeat counts as stale, not as unmeasured. The tempting
    alternative — "never wrote one, so we cannot say" — is what makes a dead
    remote's rows report ``working`` forever, which is the exact failure this
    exists to end: with no local tmux to consult and no heartbeat to read,
    nothing else about a foreign session ever changes again. Stale means only
    "do not believe this row", never "act on it".
    """
    age = heartbeat_age(session_dir, now=now)
    threshold = STALE_HEARTBEAT_SECONDS if limit is None else limit
    return age is None or age >= threshold


def host_state(
    host: str | None, session_dir: Path, *, now: float | None = None
) -> str:
    """How much this row's owning machine can be believed: the shared vocabulary.

    ``local`` — ours; local tmux and PIDs are authoritative, so nothing here
    is needed to judge it. ``foreign`` — someone else's and recently alive.
    ``foreign_stale`` — someone else's and gone quiet, so its status is the
    last thing a machine we cannot reach happened to write. Callers must
    render the difference and refuse to act on it; only the user's explicit
    force-kill treats a stale row as dead.
    """
    if not host_is_foreign(host):
        return HOST_LOCAL
    return HOST_FOREIGN_STALE if heartbeat_is_stale(session_dir, now=now) else HOST_FOREIGN


def touch_heartbeat_throttled(session_dir: Path, *, now: float | None = None) -> bool:
    """Refresh the heartbeat only if it has aged past the write interval.

    Lets a per-tick caller (a dashboard confirming its own sessions are alive)
    call this every pass without writing to network storage every pass.
    """
    age = heartbeat_age(session_dir, now=now)
    if age is not None and age < HEARTBEAT_INTERVAL_SECONDS:
        return False
    touch_heartbeat(session_dir)
    return True
