"""Multiverse MCP server.

Hosts the agent-facing tools for the multiverse plugin:
  - list_agents                   — discover other agents in workspace
  - message_agent                 — send a persistent message to another
                                    agent (delivered via inbox + hook
                                    injection; recipient may use tools and
                                    reply by calling message_agent back)
  - ask_agent                     — quick ephemeral question to another
                                    agent (forked side-question, no tools,
                                    blocking with timeout)
  - spawn_agent                   — start a brand-new subagent in the
                                    workspace with the calling agent's
                                    instruction as its first user message
                                    (user confirms+names via a monitor
                                    modal)
  - check_inbox / monitor_inbox   — read and wait on the calling agent's
                                    inbox
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Add parent to path for imports when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sessions import (
    create_session,
    existing_names_in_cwd,
    find_agent_by_name,
    foreign_host_still_reported_live,
    kill_session,
    list_named_agents_in_cwd,
)
from src.inbox import (
    claim_pending_messages,
    pending_inbox_count,
    render_messages_as_tool_result,
    write_message_to_inbox,
)
from src.interagent_log import (
    log_inbox_delivered,
    log_message_sent,
    log_notification_routed,
)
from src.compact_state import is_locked
from src.ask_agents import AskTarget, ask_one
from src.names import pick_random_name
from src.spawn_requests import (
    cleanup_request,
    new_request_id,
    wait_for_resolution,
    write_resolution,
    write_spawn_request,
)

mcp = FastMCP("multiverse")

_DEFAULT_ASK_TIMEOUT = 60
_DEFAULT_SPAWN_TIMEOUT = 300
_DEFAULT_MONITOR_INBOX_TIMEOUT = 300
_MONITOR_INBOX_POLL_INTERVAL = 1.0

# Status written to this session's status.json while monitor_inbox is parked,
# so the dashboard and list_agents show the agent as waiting-for-messages
# rather than busy. Mirrors set-status.sh's JSON shape.
_LISTENING_STATUS = "listening"


def _write_session_status(status: str) -> None:
    """Write a status value to this session's status.json (best-effort).

    The MCP server inherits MULTIVERSE_STATUS from the claude process the
    monitor spawned. No-op outside the wrapper (env var unset). Matches the
    ``{"status": ..., "ts": ...}`` shape set-status.sh writes, so hook-driven
    and tool-driven status updates are interchangeable.
    """
    status_path = os.environ.get("MULTIVERSE_STATUS")
    if not status_path:
        return
    try:
        with open(status_path, "w") as f:
            json.dump({"status": status, "ts": int(time.time())}, f)
    except OSError:
        pass


# Prompt typed into an idle/working agent's tmux pane to wake it for a
# push-notified message. The actual message content is injected separately
# by the recipient's UserPromptSubmit → deliver-inbox.py hook; this is just
# the trigger that makes the turn happen.
_WAKE_PROMPT = "Please check your inbox — a new inter-agent message just arrived."


def _client_attached(tmux_name: str) -> bool:
    """True if a user is attached to this session's tmux pane.

    We never send-keys into an attached pane (it would clobber whatever the
    user is typing), so a push notification falls back to normal inbox
    delivery when this returns True.
    """
    try:
        r = subprocess.run(
            ["tmux", "list-clients", "-t", tmux_name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _wake_via_tmux(tmux_name: str) -> bool:
    """Type the wake prompt + Enter into a session's tmux pane.

    Two send-keys calls: ``-l`` (literal) for the text so no character is
    interpreted as a tmux key name, then a separate ``Enter`` to submit.
    Returns False on any tmux error.
    """
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_name, "-l", _WAKE_PROMPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_name, "Enter"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _auto_resume(target, cwd: str) -> None:
    """Spawn a multiverse session that resumes a dormant/ended target.

    Headless mirror of the monitor's resume flow: reuse the target's name,
    reaping an ended-but-still-recorded multiverse session first so
    create_session's name-collision check doesn't fire. SessionStart +
    bin/multiverse-session's inbox-prime then deliver the pending message
    without any user action.
    """
    if target.claude_uuid is None:
        raise RuntimeError(
            f"target '{target.name}' has no tracked claude UUID — "
            "cannot resume programmatically"
        )
    if target.session is not None and target.status == "ended":
        if foreign_host_still_reported_live(target.session):
            raise RuntimeError(
                f"'{target.name}' was created on host "
                f"'{target.session.host}' and its status there still says "
                "it's active — refusing to auto-resume as a new session "
                "here to avoid creating a duplicate that would share its "
                "inbox"
            )
        kill_session(target.session)
    create_session(
        cwd=cwd,
        claude_args=["--resume", target.claude_uuid],
        resumed_from=target.claude_uuid,
        name=target.name,
    )


_DEFAULT_MAX_AGENTS = 15

_STATUS_PRIORITY = {
    "working": 0,
    "waiting": 0,
    "listening": 1,
    "starting": 1,
    "idle": 1,
    "unknown": 2,
    "dormant": 3,
    "ended": 4,
}


@mcp.tool()
def list_agents(
    max_agents: int = _DEFAULT_MAX_AGENTS,
    working_directory: str = "",
) -> str:
    """List all named agents in your multiverse workspace.

    These are the agents you can talk to via inter-agent tools. By default
    the list is scoped to your own workspace's working directory. Pass
    ``working_directory`` to instead list agents in a different workspace —
    useful for discovering agents you can ``ask_agent`` across workspaces.

    Status values:
      - idle / working / waiting / starting — currently running
      - ended    — was running, now stopped (still resumable)
      - dormant  — never wrapped by multiverse this run, but the underlying
                   transcript exists and is named

    Your own entry is marked with ``← this is you`` (only when listing your
    own workspace). The list is sorted with you first, then live agents,
    then ended/dormant.

    Args:
        max_agents: Maximum entries to show before truncating with "and N
            more". Default 15 keeps the output compact. Pass a larger value
            (e.g. 100) if you need the full roster — your own entry is
            always kept visible regardless of this cap.
        working_directory: Optional absolute path to another workspace's
            working directory. When omitted, lists agents in your own
            workspace. When provided, lists agents in that workspace
            instead; the ``← this is you`` marker is suppressed.
    """
    self_cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    self_name = os.environ.get("MULTIVERSE_SESSION_NAME") or ""

    if working_directory:
        try:
            cwd = str(Path(working_directory).expanduser().resolve())
        except (OSError, RuntimeError) as e:
            return f"Error: invalid working_directory '{working_directory}': {e}"
    else:
        if not self_cwd:
            return (
                "Error: MULTIVERSE_SESSION_CWD is not set — this tool requires "
                "running inside a multiverse-managed session."
            )
        cwd = self_cwd

    self_cwd_abs = ""
    if self_cwd:
        try:
            self_cwd_abs = str(Path(self_cwd).expanduser().resolve())
        except (OSError, RuntimeError):
            self_cwd_abs = self_cwd
    in_own_workspace = bool(self_cwd_abs) and cwd == self_cwd_abs

    # Suppress the "← this is you" marker when listing another workspace.
    self_name_for_marker = self_name if in_own_workspace else ""

    try:
        agents = list_named_agents_in_cwd(cwd)
    except Exception as e:
        return f"Error listing agents: {e}"

    if not agents:
        if in_own_workspace or not working_directory:
            return "(no named agents in this workspace)"
        return f"(no named agents in workspace '{cwd}')"

    # Sort: you first, then by liveness, then by name (stable display).
    agents.sort(
        key=lambda a: (
            a.name != self_name_for_marker,
            _STATUS_PRIORITY.get(a.status, 9),
            a.name,
        )
    )

    total = len(agents)
    live_count = sum(1 for a in agents if a.status not in ("ended", "dormant"))
    cap = max(1, int(max_agents))

    # Truncate, but keep "you" in the visible slice no matter what
    # (only relevant when listing your own workspace).
    if total > cap:
        visible = agents[:cap]
        if self_name_for_marker and not any(
            a.name == self_name_for_marker for a in visible
        ):
            you = next(
                (a for a in agents if a.name == self_name_for_marker), None
            )
            if you is not None:
                visible = [you] + visible[: cap - 1]
        hidden = total - len(visible)
    else:
        visible = agents
        hidden = 0

    if in_own_workspace or not working_directory:
        summary = f"{total} agent(s) in workspace ({live_count} live)"
    else:
        summary = (
            f"{total} agent(s) in workspace '{cwd}' ({live_count} live)"
        )

    lines = [summary, ""]
    name_width = max(len(a.name) for a in visible)
    for a in visible:
        marker = "  ← this is you" if a.name == self_name_for_marker else ""
        lines.append(f"- {a.name.ljust(name_width)}  {a.status}{marker}")
    if hidden:
        lines.append(
            f"  … and {hidden} more not shown "
            f"(call again with max_agents={total} to see all)"
        )
    return "\n".join(lines)


@mcp.tool()
def message_agent(
    target_name: str,
    message: str,
    push_notification: bool = False,
    push_notifications: bool = False,
    working_directory: str = "",
) -> str:
    """Send a durable message to another named agent.

    Use this for instructions, in-depth questions, or any communication
    where the recipient should be able to take action — read files, run
    commands, use tools, then reply at their own pace.

    By default the target is looked up in your own workspace. Pass
    ``working_directory`` (an absolute path) to message an agent in a
    DIFFERENT workspace — use ``list_agents(working_directory=...)`` to
    discover targets there. A cross-workspace message records your own
    workspace with it, so the delivered message tells the recipient to
    reply with ``working_directory=<your workspace>``.

    The message is always dropped into the recipient's inbox and persists
    in their conversation history once delivered. To reply to a message you
    received, call ``message_agent`` back at the original sender (passing
    ``working_directory`` if the message says the sender is in another
    workspace) — there is no separate "respond" tool. For a quick fact you
    don't want to persist in the target's session, use ``ask_agent``.

    DELIVERY TIMING is controlled by ``push_notification``:

    ``push_notification=False`` (default) — passive delivery. The message
    waits in the inbox and is delivered on the recipient's next natural
    turn boundary (Stop / UserPromptSubmit / SessionStart hook). A dormant
    recipient stays dormant — the message sits on disk (dashboard shows a
    📩 badge) and is delivered only when the USER next resumes that session.
    Use this for non-urgent messages where it's fine to wait for the
    recipient (or the user) to get to it.

    ``push_notification=True`` — active delivery: wake the recipient now,
    no user needed. Behavior depends on the recipient's state:
      - dormant/ended → the agent is auto-resumed (started for you) and
        processes the message on startup.
      - idle or working, with NO user attached to its terminal → it's
        pinged via its terminal to check its inbox immediately.
      - idle or working, but a user IS attached to its terminal → falls
        back to passive delivery (we won't disrupt the attached user); the
        message still arrives on the recipient's next turn.
      - listening (parked in ``monitor_inbox``) → it's already watching its
        inbox and picks the message up within a second; no wake needed.
      - compacting → the agent is locked (its terminal is killed and even
        the user can't interact with it during compaction), so it is NOT
        woken; the message queues and is delivered once compaction finishes
        and the session resumes.
    Use this for time-sensitive messages or when you're blocked waiting on
    the recipient to act.

    The tool result tells you exactly what happened (woke a dormant agent,
    pinged a live one, fell back to passive delivery, etc.).

    Args:
        target_name: Name of the agent to message. Use ``list_agents`` to
            see who's available.
        message: The message text. Can be multi-line, can include
            instructions, code, anything.
        push_notification: If True, actively wake the recipient to deliver
            now (see above). If False (default), leave it in the inbox for
            passive delivery on their next turn / user-initiated resume.
        push_notifications: Accepted alias for ``push_notification`` (either
            spelling works; they're OR'd together).
        working_directory: Optional absolute path to another workspace. When
            omitted, the target is looked up in your own workspace. When
            provided, the target (and any auto-resume / inbox delivery)
            happens in that workspace instead.
    """
    self_cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    self_name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not self_cwd or not self_name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME unset)."
        )

    # Resolve the workspace the target lives in — our own by default.
    if working_directory:
        try:
            cwd = str(Path(working_directory).expanduser().resolve())
        except (OSError, RuntimeError) as e:
            return f"Error: invalid working_directory '{working_directory}': {e}"
    else:
        cwd = self_cwd

    try:
        self_cwd_abs = str(Path(self_cwd).expanduser().resolve())
    except (OSError, RuntimeError):
        self_cwd_abs = self_cwd
    in_own_workspace = cwd == self_cwd_abs

    if in_own_workspace and target_name == self_name:
        return "Error: cannot message yourself."

    target = find_agent_by_name(cwd, target_name)
    if target is None:
        if in_own_workspace:
            return (
                f"Error: no agent named '{target_name}' in this workspace. "
                "Call list_agents to see who's available."
            )
        return (
            f"Error: no agent named '{target_name}' in workspace '{cwd}'. "
            f"Call list_agents(working_directory='{cwd}') to see who's "
            "available."
        )

    # Always write the inbox file first — and, critically, BEFORE any
    # auto-resume, so the resumed agent's SessionStart hook sees it. For a
    # cross-workspace message, stamp our own workspace so the recipient
    # knows where to reply.
    inbox_file = write_message_to_inbox(
        sender_name=self_name,
        recipient_name=target_name,
        recipient_cwd=cwd,
        message=message,
        sender_cwd="" if in_own_workspace else self_cwd_abs,
    )
    # Accept either spelling — models frequently pluralize "notification".
    push = bool(push_notification) or bool(push_notifications)

    log_message_sent(
        sender=self_name,
        recipient=target_name,
        cwd=cwd,
        push_notification=push,
        message_id=inbox_file.stem,
        message=message,
    )

    is_dormant = target.session is None or target.status == "ended"

    reply_hint = (
        " If you're expecting a reply, finish any other work first, then "
        "call monitor_inbox to wait for it (it blocks), or check_inbox later."
    )

    # A compacting agent is LOCKED: its tmux pane is killed for the duration
    # of the compaction and even the user can't attach/interact. Never wake
    # it (no auto-resume, no terminal ping) — queue the message for passive
    # delivery regardless of push, to be picked up when compaction finishes
    # and the session resumes. Checked against the compaction lock directly
    # (authoritative) rather than the status string.
    if target.claude_uuid and is_locked(target.claude_uuid):
        log_notification_routed(
            sender=self_name, recipient=target_name, cwd=cwd,
            route="passive_fallback_compacting",
        )
        return (
            f"Message queued for '{target_name}' — it's currently compacting "
            "(locked), so the message will be delivered once compaction "
            "finishes and the session resumes." + reply_hint
        )

    if not push:
        if is_dormant:
            return (
                f"Message queued for '{target_name}' (dormant — it will be "
                "delivered when the user next resumes that session)." + reply_hint
            )
        return (
            f"Message queued for '{target_name}'. It will be delivered on the "
            "recipient's next turn." + reply_hint
        )

    # push enabled — actively wake the recipient.
    if is_dormant:
        try:
            _auto_resume(target, cwd)
        except Exception as e:  # noqa: BLE001 — surface as tool error text
            log_notification_routed(
                sender=self_name, recipient=target_name, cwd=cwd,
                route="auto_resume", success=False,
            )
            return (
                f"Message queued for '{target_name}', but auto-resume failed "
                f"({e}). It will be delivered when the session is next "
                "resumed." + reply_hint
            )
        log_notification_routed(
            sender=self_name, recipient=target_name, cwd=cwd,
            route="auto_resume", success=True,
        )
        return (
            f"Woke '{target_name}' (it was dormant) and delivered your "
            "message — it will process it on startup." + reply_hint
        )

    status = target.status

    if status == "listening":
        log_notification_routed(
            sender=self_name, recipient=target_name, cwd=cwd,
            route="already_listening",
        )
        return (
            f"Delivered to '{target_name}' — it was already listening on its "
            "inbox and will pick the message up within a second." + reply_hint
        )

    if status in ("idle", "working"):
        tmux_name = target.session.tmux_name
        if _client_attached(tmux_name):
            log_notification_routed(
                sender=self_name, recipient=target_name, cwd=cwd,
                route="passive_fallback_client_attached",
            )
            return (
                f"Message queued for '{target_name}' — a user is attached to "
                "its terminal, so it falls back to passive delivery (delivered "
                "on the recipient's next turn) rather than a terminal ping."
                + reply_hint
            )
        woke = _wake_via_tmux(tmux_name)
        log_notification_routed(
            sender=self_name, recipient=target_name, cwd=cwd,
            route="wake_tmux", success=woke,
        )
        if woke:
            return (
                f"Delivered to '{target_name}' and pinged its terminal to "
                f"check its inbox now (it was {status})." + reply_hint
            )
        return (
            f"Message queued for '{target_name}', but the terminal ping "
            "failed — it will be delivered on the recipient's next turn."
            + reply_hint
        )

    # starting / unknown / other live states — let the normal hooks deliver.
    return (
        f"Message queued for '{target_name}' (status: {status}). It will be "
        "delivered on the recipient's next turn." + reply_hint
    )


@mcp.tool()
def check_inbox() -> str:
    """Read pending inbox messages now, without waiting for a hook to fire.

    Normally inbox messages are delivered automatically as a system-reminder
    on your next ``SessionStart`` / ``UserPromptSubmit`` / ``Stop`` hook. If
    you want to check mid-turn — say, you just sent a ``message_agent`` and
    suspect a reply has arrived, or you're in a long-running task and want
    to drain anything that piled up — call this.

    Returns either a system-reminder block (identical to the hook-delivered
    format) containing every pending message, OR the literal string
    ``"You have no pending inbox messages."`` when the inbox is empty.

    Calling this **clears your inbox** — exactly like a hook delivery. So
    don't call it without intending to act on the messages.
    """
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME unset)."
        )

    # Claims (reads + atomically deletes) — safe if a duplicate-session
    # process is also polling this same inbox; see claim_pending_messages.
    messages = claim_pending_messages(cwd, name)
    if not messages:
        return "You have no pending inbox messages."

    log_inbox_delivered(
        recipient=name, cwd=cwd, hook_event="check_inbox_tool", messages=messages
    )
    return render_messages_as_tool_result(messages)


@mcp.tool()
async def monitor_inbox(
    timeout_seconds: int = _DEFAULT_MONITOR_INBOX_TIMEOUT,
) -> str:
    """Block until an inbox message arrives, then signal you to read it.

    Use this when you've finished your turn but are expecting replies — e.g.
    you just sent ``message_agent`` to one or more agents and want to wait
    for their responses instead of ending your turn blind. This tool parks
    until the first message lands in your inbox (or the timeout elapses),
    so you don't have to busy-loop on ``check_inbox``.

    This tool does NOT consume or return the message contents — it only
    waits and tells you something arrived. After it returns with a new
    message, call ``check_inbox`` to actually read (and clear) your inbox.

    If you already have pending messages when you call this, it returns
    immediately telling you to check — it never makes you wait when there's
    already something to read.

    The user can interrupt this wait at any time (Esc) to prompt you — the
    call ends and your turn resumes with their input.

    Args:
        timeout_seconds: Max seconds to wait. Default 300 (5 minutes). On
            timeout, returns a "no new messages" notice — you can call
            again to keep waiting, or move on.
    """
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME unset)."
        )

    if pending_inbox_count(cwd, name) > 0:
        return (
            "You already have pending inbox messages. Call check_inbox to "
            "read them."
        )

    # Show "listening" while parked so the user (and other agents via
    # list_agents) can tell this agent is waiting for messages, not busy.
    # The finally restores "working" — the turn continues after we return
    # (the agent will call check_inbox / act), and on a user interrupt the
    # Stop hook follows up with "idle".
    _write_session_status(_LISTENING_STATUS)
    try:
        # Async sleep (not time.sleep) so a user interrupt — which the MCP
        # layer surfaces as request cancellation — propagates CancelledError
        # through the await and stops this loop immediately, instead of
        # leaking a worker thread that keeps polling until the timeout.
        timeout = max(1, int(timeout_seconds))
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(_MONITOR_INBOX_POLL_INTERVAL)
            if pending_inbox_count(cwd, name) > 0:
                return (
                    "You have a new inbox message. Call check_inbox to read it."
                )

        return (
            f"No new inbox messages arrived within {timeout}s. Call "
            "monitor_inbox again to keep waiting, or proceed without a reply."
        )
    finally:
        _write_session_status("working")


@mcp.tool()
def ask_agent(
    target_name: str,
    question: str,
    timeout_seconds: int = _DEFAULT_ASK_TIMEOUT,
    working_directory: str = "",
) -> str:
    """Ask a single named agent a quick question and wait for the answer.

    Use this for ephemeral, fact-gathering questions: "what are you working
    on?", "did you implement feature X?", "do you remember the API key
    location?". The target answers from their existing context only — they
    have NO tools available, can't read files or run commands. If the
    answer requires investigation, send a ``message_agent`` instead.

    By default the target is looked up in your own workspace. Pass
    ``working_directory`` to ask an agent in a different workspace —
    cross-workspace questions are allowed (read-only, ephemeral). Use
    ``list_agents(working_directory=...)`` to discover targets there.

    Mechanism: spawns a forked, one-shot ``claude -p`` subprocess against
    the target's session (same approach as the monitor's ``a`` keybinding,
    but for a single agent). The target's main session is NOT involved —
    no notification, no entry in their conversation history. The question
    is purely sidechannel.

    This call BLOCKS until the target responds or the timeout elapses.

    Args:
        target_name: Name of the agent to ask. Use ``list_agents``.
        question: The question text. Keep it focused — the target can only
            answer from what they already know.
        timeout_seconds: Max seconds to wait. Default 60.
        working_directory: Optional absolute path to another workspace's
            working directory. When omitted, looks up the target in your
            own workspace. When provided, looks up the target in that
            workspace instead.
    """
    self_cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    self_name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not self_cwd or not self_name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session."
        )

    if working_directory:
        try:
            cwd = str(Path(working_directory).expanduser().resolve())
        except (OSError, RuntimeError) as e:
            return f"Error: invalid working_directory '{working_directory}': {e}"
    else:
        cwd = self_cwd

    try:
        self_cwd_abs = str(Path(self_cwd).expanduser().resolve())
    except (OSError, RuntimeError):
        self_cwd_abs = self_cwd
    in_own_workspace = cwd == self_cwd_abs

    if in_own_workspace and target_name == self_name:
        return "Error: cannot ask yourself. Use your own context directly."

    target = find_agent_by_name(cwd, target_name)
    if target is None:
        if in_own_workspace:
            return (
                f"Error: no agent named '{target_name}' in this workspace. "
                "Call list_agents to see who's available."
            )
        return (
            f"Error: no agent named '{target_name}' in workspace '{cwd}'. "
            f"Call list_agents(working_directory='{cwd}') to see who's available."
        )
    if target.claude_uuid is None:
        return (
            f"Error: '{target_name}' has no tracked claude UUID yet "
            "(may have just started). Try again in a moment."
        )

    timeout = max(1, int(timeout_seconds))
    result = ask_one(
        question=question,
        target=AskTarget(
            name=target.name, cwd=target.cwd, claude_uuid=target.claude_uuid
        ),
        timeout=float(timeout),
    )

    if result.error:
        if "timed out" in result.error:
            return f"'{target_name}' did not respond within {timeout}s."
        return f"Error asking '{target_name}': {result.error}"
    return result.response or f"'{target_name}' returned an empty response."


@mcp.tool()
def spawn_agent(instruction: str, name: str = "") -> str:
    """Spawn a brand-new subagent in your workspace with a starting task.

    The ``instruction`` you pass becomes the subagent's first user message —
    the subagent starts a fresh session and acts on it immediately.

    The subagent has NO knowledge of your prior conversation. If context
    from your conversation is relevant to the task, include a targeted
    summary directly in the instruction. Be deliberate — the subagent only
    sees what you put here.

    User flow: a naming widget opens in the multiverse monitor showing your
    instruction and a suggested name. The user can accept, edit the name,
    or reject. This tool blocks until they respond (or a 5-minute timeout).
    If rejected or timed out, no subagent is created.

    Args:
        instruction: The first user message for the new agent. Can be
            multi-line, can include task context, code, a summary of your
            own prior context — anything the subagent should see to start
            its work.
        name: Optional preferred name for the new agent. The user sees this
            pre-filled in the naming widget and can accept or edit it. If
            empty or already taken in this workspace, a random name is
            suggested instead.
    """
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    self_name = os.environ.get("MULTIVERSE_SESSION_NAME")
    caller_sid = os.environ.get("MULTIVERSE_SESSION_ID")
    if not cwd or not self_name or not caller_sid:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME / "
            "MULTIVERSE_SESSION_ID unset)."
        )

    if not instruction or not instruction.strip():
        return "Error: instruction cannot be empty."

    existing = existing_names_in_cwd(cwd)
    requested = (name or "").strip()
    if requested and requested not in existing:
        default_name = requested
    else:
        default_name = pick_random_name(existing) or ""
    if not default_name:
        return (
            "Error: could not pick a default name — the workspace name "
            "pool may be exhausted. Kill some ended sessions and retry."
        )

    req_id = new_request_id()
    caller_tmux = f"multiverse-{caller_sid}"

    try:
        write_spawn_request(
            req_id=req_id,
            from_name=self_name,
            cwd=cwd,
            instruction=instruction,
            default_name=default_name,
            caller_tmux_session=caller_tmux,
            timeout_seconds=_DEFAULT_SPAWN_TIMEOUT,
        )
    except OSError as e:
        return f"Error: failed to write spawn request: {e}"

    # Nudge any user attached to the caller's pane. Best-effort — if the
    # tmux session doesn't exist or tmux isn't on PATH, skip silently.
    try:
        subprocess.run(
            [
                "tmux", "display-message", "-t", caller_tmux,
                f"'{self_name}' wants to spawn a subagent — "
                "detach (Ctrl-b d) to approve in the monitor.",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    res = wait_for_resolution(req_id, _DEFAULT_SPAWN_TIMEOUT)

    try:
        if res is None:
            # Timed out before the user (or any monitor) resolved. Write a
            # terminal resolution so any still-open modal dismisses itself.
            write_resolution(req_id, status="timeout", reason="timeout")
            return (
                f"Spawn request timed out after {_DEFAULT_SPAWN_TIMEOUT}s "
                "with no user response in the monitor. No subagent was "
                "created."
            )

        status = res.get("status")
        if status != "accepted":
            reason = res.get("reason") or status or "rejected"
            return f"Spawn request rejected by user ({reason}). No subagent was created."

        chosen = (res.get("name") or "").strip()
        if not chosen:
            return "Error: resolution contained an empty name — no subagent created."

        try:
            create_session(
                cwd=cwd,
                claude_args=[],
                name=chosen,
                # Pass the instruction off-band via a state-dir file rather
                # than as a CLI arg. This avoids: shell-escape bloat for
                # apostrophe-heavy / long instructions, the multi-stage
                # tmux→sh→multiverse-session re-parse, and any ARG_MAX
                # surprises. multiverse-session reads the file and hands
                # the content to claude as a positional argv entry.
                initial_prompt=instruction,
            )
        except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
            return f"Error: failed to create session: {e}"

        return f"Spawned agent with name '{chosen}'."
    finally:
        cleanup_request(req_id)


if __name__ == "__main__":
    mcp.run()
