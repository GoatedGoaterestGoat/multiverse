"""Waking an agent that is live on a different host.

``message_agent`` always writes the message to the recipient's inbox first —
that part already crosses hosts, because an inbox is just files on the shared
share and the recipient's own hooks deliver them. What does not cross is the
*nudge*: an idle agent has no hook pending, so locally we type a wake prompt
into its tmux pane. A tmux server is per-machine, so from another remote that
send-keys goes nowhere, and the message sits unread until the user happens to
type at that agent.

This module carries the nudge the only way anything crosses here: as a file.
The sender drops a request naming the session and the host expected to own
it; the monitor running ON that host claims it during its normal tick and
performs the local ping. No sockets, no daemon, no assumption that the two
remotes can reach each other — only that they see the same directory.

Two properties matter more than throughput:

*Ownership is revalidated at delivery, never trusted from the request.* The
recorded host is a hint about where to look, and a request is executed only
after re-reading the session from disk and confirming it is still ours, still
live, and still has a local tmux session. Ownership that moved leaves the
request pending for whoever owns it now.

*A wake is dropped only once it is known to be unnecessary.* Anything that
merely failed — a busy composer, a tmux error, a session mid-teardown — is
released back to pending, because the cost of a retried nudge is a redundant
"check your inbox" and the cost of a lost one is a message nobody reads.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .atomic_files import atomic_create_json
from .inbox import MULTIVERSE_DIR, pending_inbox_count

WAKE_REQUESTS_DIR = MULTIVERSE_DIR / "wake_requests"

#: Age at which a request that has ALREADY lost its meaning — one whose
#: recipient cannot be resolved to an inbox any more — is swept. Deliberately
#: not an expiry for live requests: an outage is exactly when a durable nudge
#: earns its keep, so a still-unread message keeps its request however long
#: its host stays down. See ``gc``.
MAX_AGE_SECONDS = 1800.0

#: Statuses whose sessions will never be typed into: the session is over or
#: being torn down. Their requests are retired by ``gc`` once the session goes.
_UNWAKEABLE = ("ended", "ending", "retiring", "retired")

#: Statuses that cannot take a wake prompt *yet*. A starting session has no
#: composer, one waiting on a permission modal cannot act on a prompt, and a
#: compacting one has had its pane killed. All three end, so the request is
#: left pending and tried again — which is the whole reason a sender files a
#: request for these states rather than being told the nudge was impossible.
_NOT_YET = ("starting", "waiting", "compacting")


@dataclass(frozen=True)
class WakeRequest:
    """One pending "please nudge this session" note on the shared share."""

    session_id: str
    host: str
    cwd: str
    name: str
    runtime: str
    tmux_name: str
    requested_at: float
    requested_by: Optional[str] = None

    @property
    def path(self) -> Path:
        return request_path(self.session_id)


def request_path(session_id: str) -> Path:
    return WAKE_REQUESTS_DIR / f"{session_id}.json"


def request_wake(session, *, requested_by: Optional[str] = None) -> bool:
    """Ask whichever host owns ``session`` to nudge it. True if newly filed.

    One pending request per session, so a burst of messages to the same
    recipient produces one nudge rather than a queue of identical ones — the
    agent reads its whole inbox in a single ``check_inbox`` anyway. A False
    return means a request was already waiting, which is success from the
    caller's point of view: the nudge is already coming.
    """
    return atomic_create_json(
        request_path(session.id),
        {
            "session_id": session.id,
            "host": session.host or "",
            "cwd": session.cwd,
            "name": session.name or "",
            "runtime": session.runtime,
            "tmux_name": session.tmux_name,
            "requested_at": time.time(),
            "requested_by": requested_by or "",
        },
    )


def _load(path: Path) -> Optional[WakeRequest]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    requested_at = data.get("requested_at")
    return WakeRequest(
        session_id=session_id,
        host=str(data.get("host") or ""),
        cwd=str(data.get("cwd") or ""),
        name=str(data.get("name") or ""),
        runtime=str(data.get("runtime") or ""),
        tmux_name=str(data.get("tmux_name") or ""),
        requested_at=float(requested_at) if isinstance(requested_at, (int, float)) else 0.0,
        requested_by=str(data.get("requested_by") or "") or None,
    )


def list_requests() -> list[WakeRequest]:
    """Every pending request on the share, oldest first. Never raises."""
    try:
        entries = sorted(WAKE_REQUESTS_DIR.iterdir())
    except OSError:
        return []
    found = [
        request for request in (_load(path) for path in entries if path.suffix == ".json")
        if request is not None
    ]
    found.sort(key=lambda request: request.requested_at)
    return found


def _resolve(request: WakeRequest) -> Optional[WakeRequest]:
    """The request re-derived from the session on disk, or None if unactionable.

    Everything used to aim keystrokes — cwd, name, runtime, tmux pane — is
    taken from the session record, never from the file. A request is a note
    saying "someone wants session X nudged"; its remaining fields are a
    snapshot from another machine that may be stale, and were it trusted, a
    request naming a pane that has since been reused would type a wake prompt
    into an unrelated agent's composer. Only the session id, and who asked,
    survive from the file.

    Returns None when this host should not act *now*: the session is gone,
    belongs to someone else, is being torn down, has no local pane, or is in a
    state that cannot receive a prompt yet. Its request is left pending, for
    whoever owns it or for the moment it can take one.
    """
    # Ownership is asked of ``sessions``, not derived here, so that every
    # host decision in the codebase goes through one authority and one
    # patchable name. Comparing ``session.host`` ourselves would answer a
    # question the rest of the code answers elsewhere, and quietly
    # disagree with it the moment either side changes.
    from .sessions import is_foreign_host, load_session, tmux_session_exists

    session = load_session(request.session_id)
    if session is None:
        return None
    if is_foreign_host(session):
        return None
    if session.status in _UNWAKEABLE or session.ending_marker_path.is_file():
        return None
    if session.status in _NOT_YET:
        return None
    if not session.name:
        return None
    if not tmux_session_exists(session.tmux_name):
        return None
    return WakeRequest(
        session_id=session.id,
        host=session.host or "",
        cwd=session.cwd,
        name=session.name,
        runtime=session.runtime,
        tmux_name=session.tmux_name,
        requested_at=request.requested_at,
        requested_by=request.requested_by,
    )


def claimable_wakes(*, now: Optional[float] = None) -> list[WakeRequest]:
    """Requests this host owns and can act on right now, re-derived from disk.

    Ownership comes from the session record, not from the request: the
    recorded host only says where to look. A session that moved, ended, or is
    being torn down is left alone — its request stays pending for its real
    owner, and ``gc`` removes it once there is nothing left to deliver.
    """
    resolved = (_resolve(request) for request in list_requests())
    return [request for request in resolved if request is not None]


def complete_wake(request: WakeRequest) -> None:
    """Drop a request whose nudge was delivered. Idempotent."""
    try:
        request.path.unlink()
    except OSError:
        pass


def release_wake(request: WakeRequest) -> None:
    """Leave a request pending after a failed attempt.

    Deliberately does nothing: the file was never moved aside, so "release"
    is the absence of a delete. It exists as a named call so a failed wake
    path reads as an explicit decision to retry rather than as a missing
    cleanup someone should add.
    """


def gc(*, max_age_seconds: float = MAX_AGE_SECONDS, now: Optional[float] = None) -> int:
    """Sweep requests that can no longer do any good. Returns how many.

    Removed: requests naming a session that no longer exists, and requests
    whose recipient's inbox is already empty — their message was collected by
    the recipient's own hooks, which is the outcome the nudge was chasing.

    Age alone never expires a live request. A remote that is down for an hour
    is precisely when a durable nudge earns its keep: the message is still
    unread, the session still exists, and the request is the only thing that
    will make its agent look when the host comes back. Age therefore only
    clears records that have already lost their meaning — no readable
    session, or a name we can no longer resolve an inbox for.
    """
    from .sessions import load_session

    moment = time.time() if now is None else now
    swept = 0
    for request in list_requests():
        session = load_session(request.session_id)
        if session is None:
            complete_wake(request)
            swept += 1
            continue
        name = session.name or request.name
        if not name:
            if moment - request.requested_at >= max_age_seconds:
                complete_wake(request)
                swept += 1
            continue
        try:
            outstanding = pending_inbox_count(session.cwd, name) > 0
        except OSError:
            continue  # unreadable share: decide nothing, retry next tick
        if not outstanding:
            complete_wake(request)
            swept += 1
    return swept


def relay_pending_wakes(deliver) -> int:
    """Nudge every session this host owns that someone asked us to wake.

    ``deliver(request) -> bool`` performs the host-local ping and reports
    whether it actually landed; the runtime-specific typing lives with the
    runtime, not here. Returns the number delivered. A raising or falsy
    delivery leaves the request pending for the next tick.

    Each ping runs under the same ``wake_delivery_lock`` a direct
    ``message_agent`` ping takes, and re-checks inside it, because the relay
    is a second writer to a surface that already had one: without the lock a
    local sender and this tick can both read "nothing outstanding", both type
    into the same pane, and hand the agent the double interruption the lock
    exists to prevent. Inside the lock, an empty inbox or an outstanding
    prompt means the nudge is already unnecessary, so the request is retired
    without typing anything at all.
    """
    from .inbox import wake_delivery_lock, wake_is_outstanding

    delivered = 0
    for claimed in claimable_wakes():
        with wake_delivery_lock(claimed.cwd, claimed.name):
            # Re-derived a second time, inside the lock. Acquiring it can
            # block behind a sender mid-ping, and a session can end, be torn
            # down, or change hands during that wait — so the pane about to
            # be typed into is confirmed to still be this session's here,
            # with nothing able to intervene between the check and the keys.
            request = _resolve(claimed)
            if request is None or request.tmux_name != claimed.tmux_name:
                release_wake(claimed)
                continue
            try:
                if pending_inbox_count(request.cwd, request.name) == 0:
                    # Their own hooks already collected it; nothing to nudge.
                    complete_wake(request)
                    continue
                if wake_is_outstanding(request.cwd, request.name):
                    # A prompt is already sitting in that pane unanswered.
                    complete_wake(request)
                    continue
            except OSError:
                release_wake(request)
                continue
            try:
                ok = deliver(request)
            except Exception:  # noqa: BLE001 — a monitor tick must never die here
                ok = False
        if ok:
            complete_wake(request)
            delivered += 1
        else:
            release_wake(request)
    return delivered
