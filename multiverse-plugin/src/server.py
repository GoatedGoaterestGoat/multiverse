"""Multiverse MCP server.

Hosts the agent-facing tools for the multiverse plugin:
  - context_info / context_fold   — selective context folding
  - list_agents                   — discover other agents in workspace
  - message_agent                 — send a persistent message to another
                                    agent (delivered via inbox + hook
                                    injection; recipient may use tools and
                                    reply by calling message_agent back)
  - ask_agent                     — quick ephemeral question to another
                                    agent (forked side-question, no tools,
                                    blocking with timeout)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Add parent to path for imports when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.session import find_session_path, parse_jsonl, format_context_info
from src.rewriter import fold_context
from src.sessions import (
    create_session,
    find_agent_by_name,
    list_named_agents_in_cwd,
)
from src.inbox import write_message_to_inbox
from src.ask_agents import AskTarget, ask_one

mcp = FastMCP("multiverse")

_DEFAULT_ASK_TIMEOUT = 60


def _write_control_action(action: dict) -> bool:
    """Write an action to the multiverse wrapper's control file.

    Returns True if running under the wrapper (action was written),
    False otherwise (manual mode).
    """
    control_path = os.environ.get("MULTIVERSE_CONTROL")
    if not control_path:
        return False
    try:
        with open(control_path, "w") as f:
            json.dump(action, f)
        return True
    except OSError:
        return False


@mcp.tool()
def context_info(session_id: str, cwd: str) -> str:
    """Get a lightweight table of contents of the current session context.

    Returns a numbered index of all conversation messages with their types,
    previews, and sizes. Use this to identify which message ranges to fold.

    Args:
        session_id: The current Claude Code session ID
        cwd: The current working directory
    """
    try:
        path = find_session_path(session_id, cwd)
        entries = parse_jsonl(path)
        return format_context_info(session_id, entries)
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error reading session: {e}"


@mcp.tool()
def context_fold(
    summary: str,
    start: int,
    end: int,
    session_id: str,
    cwd: str,
) -> str:
    """Fold (summarize) a range of messages in the current session context.

    Replaces messages from `start` to `end` (inclusive) with your summary.
    Creates a new session with the folded context. The original session is preserved.

    If running under the multiverse wrapper, the session resumes automatically.
    Otherwise, inform the user to run: claude --resume <new_session_id>

    Args:
        summary: Your summary of the folded content. Write this yourself — you know your context.
        start: Start message index (inclusive). Get indices from context_info. Must be >= 1.
        end: End message index (inclusive). Must be >= start.
        session_id: The current Claude Code session ID
        cwd: The current working directory
    """
    try:
        result = fold_context(
            session_id=session_id,
            cwd=cwd,
            start=start,
            end=end,
            summary=summary,
        )

        # Signal the multiverse wrapper to resume with folded context
        auto_resume = _write_control_action({
            "action": "resume_folded",
            "new_session_id": result["new_session_id"],
        })

        status = (
            f"Context folded successfully.\n"
            f"Removed {result['removed_count']} messages "
            f"(~{result['tokens_before']} tokens -> ~{result['tokens_after']} tokens).\n"
            f"New session: {result['new_session_id']}"
        )

        if auto_resume:
            return status + "\nSession will resume automatically."
        else:
            return status + f"\nRun: claude --resume {result['new_session_id']}"

    except ValueError as e:
        return f"Fold error: {e}"
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error during fold: {e}"


_DEFAULT_MAX_AGENTS = 15

_STATUS_PRIORITY = {
    "working": 0,
    "waiting": 0,
    "starting": 1,
    "idle": 1,
    "unknown": 2,
    "dormant": 3,
    "ended": 4,
}


@mcp.tool()
def list_agents(max_agents: int = _DEFAULT_MAX_AGENTS) -> str:
    """List all named agents in the current multiverse workspace.

    These are the agents you can talk to via inter-agent tools. The list is
    scoped to your workspace's working directory — agents in other
    workspaces are not visible.

    Status values:
      - idle / working / waiting / starting — currently running
      - ended    — was running, now stopped (still resumable)
      - dormant  — never wrapped by multiverse this run, but the underlying
                   transcript exists and is named

    Your own entry is marked with ``← this is you``. The list is sorted
    with you first, then live agents, then ended/dormant.

    Args:
        max_agents: Maximum entries to show before truncating with "and N
            more". Default 15 keeps the output compact. Pass a larger value
            (e.g. 100) if you need the full roster — your own entry is
            always kept visible regardless of this cap.
    """
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    if not cwd:
        return (
            "Error: MULTIVERSE_SESSION_CWD is not set — this tool requires "
            "running inside a multiverse-managed session."
        )

    self_name = os.environ.get("MULTIVERSE_SESSION_NAME") or ""

    try:
        agents = list_named_agents_in_cwd(cwd)
    except Exception as e:
        return f"Error listing agents: {e}"

    if not agents:
        return "(no named agents in this workspace)"

    # Sort: you first, then by liveness, then by name (stable display).
    agents.sort(
        key=lambda a: (
            a.name != self_name,
            _STATUS_PRIORITY.get(a.status, 9),
            a.name,
        )
    )

    total = len(agents)
    live_count = sum(1 for a in agents if a.status not in ("ended", "dormant"))
    cap = max(1, int(max_agents))

    # Truncate, but keep "you" in the visible slice no matter what.
    if total > cap:
        visible = agents[:cap]
        if self_name and not any(a.name == self_name for a in visible):
            you = next((a for a in agents if a.name == self_name), None)
            if you is not None:
                visible = [you] + visible[: cap - 1]
        hidden = total - len(visible)
    else:
        visible = agents
        hidden = 0

    summary = f"{total} agent(s) in workspace ({live_count} live)"

    lines = [summary, ""]
    name_width = max(len(a.name) for a in visible)
    for a in visible:
        marker = "  ← this is you" if a.name == self_name else ""
        lines.append(f"- {a.name.ljust(name_width)}  {a.status}{marker}")
    if hidden:
        lines.append(
            f"  … and {hidden} more not shown "
            f"(call again with max_agents={total} to see all)"
        )
    return "\n".join(lines)


@mcp.tool()
def message_agent(target_name: str, message: str) -> str:
    """Send a durable message to another named agent in your workspace.

    Use this for instructions, in-depth questions, or any communication
    where the recipient should be able to take action — read files, run
    commands, use tools, then reply at their own pace.

    Delivery: the message is dropped into the recipient's inbox and
    surfaces as a system-reminder injected into their next turn (or
    immediately on resume if they're dormant — they get auto-resumed). The
    message persists in the recipient's conversation history. This is
    fire-and-forget: the tool returns as soon as the message is delivered;
    you do NOT block on a reply.

    To reply to a message you received, simply call ``message_agent`` back
    at the original sender — there is no separate "respond" tool.

    For quick one-off questions where you just need a fact and don't want
    to disturb the target's main session, use ``ask_agent`` instead.

    Args:
        target_name: Name of the agent to message. Use ``list_agents`` to
            see who's available.
        message: The message text. Can be multi-line, can include
            instructions, code, anything.
    """
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    self_name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not self_name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME unset)."
        )

    if target_name == self_name:
        return "Error: cannot message yourself."

    target = find_agent_by_name(cwd, target_name)
    if target is None:
        return (
            f"Error: no agent named '{target_name}' in this workspace. "
            "Call list_agents to see who's available."
        )

    # IMPORTANT: write the inbox file BEFORE auto-resuming. Reverse order
    # races claude's SessionStart hook, which fires within milliseconds of
    # spawn — if it fires first, it sees an empty inbox and the message
    # sits unread until something else triggers a hook later.
    write_message_to_inbox(
        sender_name=self_name,
        recipient_name=target_name,
        recipient_cwd=cwd,
        message=message,
    )

    needs_resume = target.session is None or target.status == "ended"
    if needs_resume:
        try:
            _auto_resume(target, cwd)
        except Exception as e:  # noqa: BLE001
            return f"Error: failed to auto-resume '{target_name}': {e}"

    suffix = " (target was dormant; auto-resumed)" if needs_resume else ""
    return f"Message delivered to '{target_name}'{suffix}."


@mcp.tool()
def ask_agent(
    target_name: str,
    question: str,
    timeout_seconds: int = _DEFAULT_ASK_TIMEOUT,
) -> str:
    """Ask a single named agent a quick question and wait for the answer.

    Use this for ephemeral, fact-gathering questions: "what are you working
    on?", "did you implement feature X?", "do you remember the API key
    location?". The target answers from their existing context only — they
    have NO tools available, can't read files or run commands. If the
    answer requires investigation, send a ``message_agent`` instead.

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
    """
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    self_name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not self_name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session."
        )

    if target_name == self_name:
        return "Error: cannot ask yourself. Use your own context directly."

    target = find_agent_by_name(cwd, target_name)
    if target is None:
        return (
            f"Error: no agent named '{target_name}' in this workspace. "
            "Call list_agents to see who's available."
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


def _auto_resume(target, cwd: str) -> None:
    """Spawn a multiverse session that resumes the dormant/ended target.

    Mirrors the monitor's resume flow but headless. Reuses the target's
    existing name; if an ended multiverse session is still occupying the
    name slot, reap it first.
    """
    if target.claude_uuid is None:
        raise RuntimeError(
            f"target '{target.name}' has no tracked claude UUID — "
            "cannot resume programmatically"
        )
    if target.session is not None and target.status == "ended":
        from src.sessions import kill_session  # local import to avoid cycle
        kill_session(target.session)

    create_session(
        cwd=cwd,
        claude_args=["--resume", target.claude_uuid],
        resumed_from=target.claude_uuid,
        name=target.name,
    )


if __name__ == "__main__":
    mcp.run()
