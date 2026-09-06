"""Inter-agent message inbox.

A single on-disk surface:

  ~/.multiverse/inbox/<encoded-cwd>/<recipient-name>/<message-id>.json
                                          — pending message file. Written
                                             atomically by message_agent
                                             (one tool call = one file).
                                             Consumed (deleted) by the
                                             deliver-inbox hook after a
                                             single delivery as system-
                                             reminder content.

Files are written via tmp+rename so the deliver-inbox hook never sees a
partial file mid-write.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Optional

from .atomic_files import atomic_write_json, exclusive_file_lock
from .paths import encode_cwd as _encode_cwd, multiverse_dir


MULTIVERSE_DIR = multiverse_dir()
INBOX_DIR = MULTIVERSE_DIR / "inbox"

# How long a staged (claimed but uncommitted) message waits before the
# next claimant assumes its deliverer died and re-adopts it. Delivery
# itself takes milliseconds; see _readopt_stale_claims for why this is
# age-based rather than pid-based.
_STALE_CLAIM_SECONDS = 60.0


def agent_inbox_dir(cwd: str, name: str) -> Path:
    return INBOX_DIR / _encode_cwd(cwd) / name


def new_message_id() -> str:
    return str(uuid.uuid4())


def write_message_to_inbox(
    sender_name: str,
    recipient_name: str,
    recipient_cwd: str,
    message: str,
    sender_cwd: str = "",
) -> Path:
    """Drop a message into the recipient's inbox.

    ``sender_cwd`` should be set only for cross-workspace messages (when the
    sender's workspace differs from the recipient's). It's recorded as
    ``from_cwd`` so the delivered message can tell the recipient which
    workspace to reply into (``message_agent(..., working_directory=...)``).
    Omit it for same-workspace messages — a plain ``from`` name is enough.
    """
    message_id = new_message_id()
    inbox_file = agent_inbox_dir(recipient_cwd, recipient_name) / f"{message_id}.json"
    payload: dict = {
        "message_id": message_id,
        "from": sender_name,
        "body": message,
        "ts": time.time(),
    }
    if sender_cwd:
        payload["from_cwd"] = sender_cwd
    atomic_write_json(inbox_file, payload)
    return inbox_file


def list_pending_messages(cwd: str, name: str) -> list[dict]:
    """List pending inbox messages for an agent, sorted by ts ascending.

    Read-only — does NOT consume anything. Used only by the dashboard's
    "peek" (``i`` key), which explicitly must not clear the inbox. Any
    caller that intends to deliver-and-clear must use
    ``claim_pending_messages`` instead, not this + a separate delete —
    see that function's docstring for why.
    """
    d = agent_inbox_dir(cwd, name)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for path in d.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["__path"] = str(path)
        out.append(data)
    out.sort(key=lambda d: d.get("ts", 0))
    return out


def claim_pending_messages(cwd: str, name: str) -> list[dict]:
    """Atomically claim (read + delete) all pending messages for an agent.

    Safe under concurrent callers — e.g. two processes that both ended up
    live under the same ``(cwd, name)`` (a duplicate-session bug, not
    something this function can prevent, but one it must not compound).
    Without this, a naive "read all, then delete all" sequence has a real
    window where two concurrent readers both see the same still-present
    file before either deletes it, and both deliver it — the same message
    injected twice, each at its own hook-fire's slightly different
    timestamp.

    The claim step is a rename to a claimant-unique path, NOT a bare
    ``unlink()`` race — measured directly under heavy thread contention,
    two racing ``unlink()`` calls on the same path both reported success
    often enough to matter (not a theoretical concern), while a rename of
    the same shared source to two DIFFERENT claimant-unique destinations
    never double-succeeded across thousands of trials. ``rename()``
    requires the source to still exist, so only one racer's rename can
    ever land; every other racer's rename fails with ``FileNotFoundError``
    the instant the winner's rename completes, and they drop the message
    without returning it. Once renamed to our own uniquely-named path, no
    other caller even knows that name, so reading + deleting it afterward
    is no longer contested.

    Claiming STAGES rather than deletes. Each returned dict carries a
    ``__claim_path`` pointing at its staged file, which survives until
    the caller confirms delivery via ``commit_claimed_messages``. A
    process that dies between claiming and delivering therefore loses
    nothing — the staged file is re-adopted by the next claim (see
    ``_readopt_stale_claims``).

    That ordering matters: deleting at claim time put an unbounded
    amount of fallible work (reading hook stdin, logging, rendering,
    writing the response) between the destroy and the hand-over, and
    deliver-inbox swallows exceptions with ``sys.exit(0)`` so hooks
    never crash claude. Any failure in that window destroyed the
    messages, exited success, and told nobody.

    Returns the claimed messages, sorted by ts ascending.
    """
    d = agent_inbox_dir(cwd, name)
    if not d.is_dir():
        return []
    _readopt_stale_claims(d)
    claimant = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    claimed: list[dict] = []
    for path in d.glob("*.json"):
        claim_path = path.with_name(path.name + f".claim-{claimant}")
        try:
            path.rename(claim_path)
        except OSError:
            # Another concurrent claimant won the race for this file —
            # they're the one delivering it, not us.
            continue
        try:
            data = json.loads(claim_path.read_text())
        except (OSError, json.JSONDecodeError):
            # Corrupt or unreadable: drop it outright. Staging it would
            # strand a message no caller can ever deliver or commit, and
            # _readopt_stale_claims would then re-adopt it every 60s
            # forever.
            try:
                claim_path.unlink()
            except OSError:
                pass
            continue
        data["__claim_path"] = str(claim_path)
        claimed.append(data)
    claimed.sort(key=lambda m: m.get("ts", 0))
    return claimed


def commit_claimed_messages(messages: list[dict]) -> None:
    """Delete the staged files for messages whose delivery is confirmed.

    Call this AFTER the hand-over actually happened — the hook's
    response written to stdout, or the tool's result returned. Before
    that point the staged files are the only copy.

    Commits exactly what was staged, by path, rather than clearing the
    inbox directory: a concurrent ``message_agent`` may have written new
    messages during delivery, and those were never handed to anyone.
    """
    for m in messages:
        if not isinstance(m, dict):
            continue
        raw = m.get("__claim_path")
        if not raw:
            continue
        try:
            Path(raw).unlink()
        except OSError:
            pass


def release_claimed_messages(cwd: str, name: str, basenames: list[str]) -> int:
    """Return named staged files to the unclaimed pool immediately.

    ``claim_pending_messages`` stages by renaming, and the staged name is
    invisible to ``list_pending_messages`` / ``pending_inbox_count``. If the
    hand-over then fails, ``_readopt_stale_claims`` only re-adopts the files
    after ``_STALE_CLAIM_SECONDS`` — a minute during which the sender is
    waiting on a reply for a message nothing will deliver.

    A deliverer that KNOWS its delivery failed does not have to wait out that
    window: it can undo its own claim at once. Staleness recovery stays as the
    backstop for the case this cannot cover, a deliverer that dies outright.

    Accepts staged basenames (as handed back by ``claim-inbox``) rather than
    paths, and ignores anything that is not one: this is reachable from a
    subprocess bridge, and a rename driven by unvalidated argv is not something
    to hand a local caller.
    """
    d = agent_inbox_dir(cwd, name)
    if not d.is_dir():
        return 0
    released = 0
    for basename in basenames:
        if (
            not basename
            or Path(basename).name != basename
            or ".json.claim-" not in basename
        ):
            continue
        claim_path = d / basename
        original = claim_path.with_name(claim_path.name.split(".claim-")[0])
        try:
            claim_path.rename(original)
        except OSError:
            continue
        released += 1
    return released


def _readopt_stale_claims(d: Path) -> None:
    """Return abandoned staged files to the unclaimed pool.

    A staged file older than ``_STALE_CLAIM_SECONDS`` belonged to a
    deliverer that died between claiming and committing. Renaming it
    back to its original name makes the next claim loop pick it up.

    Staleness is judged by age, deliberately not by whether the
    claimant's pid is still alive: pid reuse makes that check
    unreliable (this codebase already hit exactly that in the
    tree-build tail loop — a recycled pid kept a dead worker looking
    alive forever, fixed in 5437f39), and here the consequence of
    getting it wrong would be permanent message loss.

    The threshold is enormous relative to the work it covers — delivery
    is a few file reads and one write — so a false positive needs a
    deliverer wedged for a full minute. And a false positive costs a
    duplicate delivery, which is strictly better than a message
    delivered never.
    """
    now = time.time()
    for claim_path in d.glob("*.json.claim-*"):
        try:
            if now - claim_path.stat().st_mtime < _STALE_CLAIM_SECONDS:
                continue
        except OSError:
            continue
        original = claim_path.with_name(claim_path.name.split(".claim-")[0])
        try:
            claim_path.rename(original)
        except OSError:
            pass


def pending_inbox_count(cwd: str, name: str) -> int:
    """Cheap count for TUI display. Doesn't parse files."""
    d = agent_inbox_dir(cwd, name)
    if not d.is_dir():
        return 0
    try:
        return sum(1 for _ in d.glob("*.json"))
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Outstanding-wake tracking
#
# Delivery is always active: every message_agent call to a live agent types a
# wake prompt ("check your inbox") into its terminal. Sending three messages to
# a busy agent therefore queued three prompts — but check_inbox drains the
# WHOLE inbox in one go, so prompts 2 and 3 are pure waste: they burn a turn
# each to find an empty inbox.
#
# A marker records "we typed a prompt this agent hasn't consumed yet". While it
# is set, further messages are still written to the inbox but no new prompt is
# typed — the outstanding one collects them all when it fires.
# ---------------------------------------------------------------------------

WAKE_PENDING_DIR = MULTIVERSE_DIR / "wake_pending"


def _wake_marker_path(cwd: str, name: str) -> Path:
    return WAKE_PENDING_DIR / _encode_cwd(cwd) / name


@contextmanager
def wake_delivery_lock(cwd: str, name: str):
    """Serialize check→ping→mark for one recipient, across processes.

    Every sender runs in its own MCP server process, and this workspace
    routinely has several messaging at once. Without a lock, two senders can
    both read "no outstanding prompt", both ping, and the agent gets exactly
    the double interruption this is meant to prevent.

    The lock spans the terminal I/O rather than just the marker write. A
    reserve-then-ping scheme (mark first, ping after) would be cheaper but is
    unsafe: if the winner's ping then fails, the loser has already skipped, and
    the message sits with no prompt behind it at all. Holding the lock across
    the ping means the loser re-checks *after* the winner's outcome is known —
    a failed ping leaves the marker unset, so the loser pings for real.

    Best-effort: if the lock can't be taken (odd filesystem, no fcntl), the
    body still runs. The failure mode is a redundant prompt, which is just the
    pre-existing behaviour, never a dropped message.

    Only ACQUISITION failure is swallowed. Wrapping the ``yield`` in a
    ``try/except OSError`` instead would catch exceptions raised by the
    protected body — thrown back in at the yield point — and then yield a
    second time, which a @contextmanager cannot do ("generator didn't stop
    after throw()"). That turned any tmux OSError during delivery into a
    misleading RuntimeError. ExitStack keeps acquisition failure and body
    failure separate: exactly one yield either way, and body exceptions
    propagate untouched.
    """
    path = _wake_marker_path(cwd, name).with_name(f"{name}.lock")
    with ExitStack() as stack:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(exclusive_file_lock(path))
        except OSError:
            pass  # unlocked fallback — see above
        yield


def mark_wake_sent(cwd: str, name: str) -> None:
    """Record that a wake prompt was typed into ``name``'s terminal.

    Call only after the keystrokes actually landed. A failed ping must leave
    the marker unset, or the failure would suppress every later ping too.
    """
    path = _wake_marker_path(cwd, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()))
    except OSError:
        pass  # best-effort: worst case we send a redundant prompt


def clear_wake_sent(cwd: str, name: str) -> None:
    """Record that ``name`` consumed its inbox, so the next message may ping."""
    try:
        _wake_marker_path(cwd, name).unlink(missing_ok=True)
    except OSError:
        pass


def wake_is_outstanding(cwd: str, name: str) -> bool:
    """Whether a typed wake prompt is still waiting to be acted on.

    Self-heals on an empty inbox: a marker only means anything while there is
    something left to collect. Without that, an agent killed after being pinged
    would keep its marker forever and never be pinged again.
    """
    if not _wake_marker_path(cwd, name).exists():
        return False
    if pending_inbox_count(cwd, name) == 0:
        clear_wake_sent(cwd, name)
        return False
    return True


def _fmt_lines(text: str, prefix: str = "> ") -> list[str]:
    if not text:
        return [f"{prefix}(empty)"]
    return [f"{prefix}{line}" for line in text.splitlines() or [text]]


def _format_one_message(m: dict, idx: int, total: int) -> list[str]:
    """Render a single message: separator, header lines, body. Shared by
    both delivery paths so the per-message layout stays in lockstep."""
    from_name = m.get("from", "?")
    from_cwd = m.get("from_cwd")  # present only for cross-workspace messages
    body = m.get("body", "")
    ts = m.get("ts", 0)
    ts_str = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
    )
    header = f"[{idx}/{total}] From agent: {from_name}"
    lines = ["─" * 56, header, f"        received: {ts_str}"]
    if from_cwd:
        lines.append(f"        in workspace: {from_cwd}")
        lines.append(
            f'        (to reply, use working_directory="{from_cwd}")'
        )
    lines += ["", *_fmt_lines(body), ""]
    return lines


def render_messages_as_system_reminder(messages: list[dict]) -> str:
    """Hook delivery format: push-style, wrapped in ``<system-reminder>``.

    Used by ``scripts/deliver-inbox.py`` because hook injection drops the
    content into the conversation as a synthetic user message (via
    ``additionalContext`` or Stop's ``decision: "block"`` reason). The
    ``<system-reminder>`` wrapper tells the model "this is out-of-band, not
    a real user turn" and the "You have received N new messages" header
    frames it as a push notification.
    """
    n = len(messages)
    plural = "s" if n != 1 else ""
    parts: list[str] = ["<system-reminder>"]
    parts.append(
        f"[Multiverse Inter-Agent] You have received {n} new "
        f"message{plural} from other agents (each is labelled with its "
        "sender; messages from another workspace also show that workspace)."
    )
    parts.append("")
    for i, m in enumerate(messages, 1):
        parts.extend(_format_one_message(m, i, n))
    parts.append("─" * 56)
    parts.append(
        "You may use any tools (Read, Bash, Grep, etc.) to act on these "
        "messages. To reply to a sender, call the `message_agent` tool "
        "with their name as the target (for a sender shown 'in workspace …', "
        "pass that path as `working_directory`). Replies are not required — "
        "only respond if it's useful."
    )
    parts.append("</system-reminder>")
    return "\n".join(parts)


def render_messages_as_tool_result(messages: list[dict]) -> str:
    """check_inbox tool-result format: pull-style, plain text.

    The agent explicitly called ``check_inbox`` and is awaiting a tool
    result — so wrapping in ``<system-reminder>`` (which signals
    "unsolicited push") would be a mixed signal, and the
    "You have received N new messages" header would be off-tone. Same
    per-message layout as the hook path; different framing.
    """
    n = len(messages)
    plural = "s" if n != 1 else ""
    parts: list[str] = [f"{n} pending inbox message{plural}:", ""]
    for i, m in enumerate(messages, 1):
        parts.extend(_format_one_message(m, i, n))
    parts.append("─" * 56)
    parts.append(
        "To reply to any sender, call `message_agent` with their name as "
        "the target (for a sender shown 'in workspace …', pass that path as "
        "`working_directory`). Replies are not required — only respond if "
        "it's useful."
    )
    return "\n".join(parts)
