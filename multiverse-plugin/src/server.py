"""Multiverse MCP server.

Hosts the agent-facing tools for the multiverse plugin:
  - context_info / context_fold        — selective context folding
  - list_agents                        — discover other agents in workspace
  - ask_agent / respond_to_agent       — inter-agent question/response
"""

from __future__ import annotations

import json
import os
import sys
import time
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
from src.inbox import (
    AskRecord,
    delete_ask,
    find_response_file,
    load_ask,
    new_ask_id,
    save_ask,
    write_question_to_inbox,
    write_response_to_inbox,
)

mcp = FastMCP("multiverse")

# Default timeout (seconds) for blocking ask_agent calls.
_DEFAULT_ASK_TIMEOUT = 600
_POLL_INTERVAL = 0.5


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
def ask_agent(
    target_name: str,
    question: str,
    wait_for_response: bool = False,
    timeout_seconds: int = _DEFAULT_ASK_TIMEOUT,
) -> str:
    """Send a question to another named agent in your workspace.

    The target agent receives your question as a notification (system-
    reminder) injected into their next turn. They can use any tools to
    investigate, then call ``respond_to_agent`` with their answer.

    The target agent's main session is involved (this is not a forked one-
    shot like ``/btw``). The full back-and-forth lives in the target's
    conversation history, and they can use Read, Bash, Grep, etc. as needed
    to answer.

    If the target is dormant, they're auto-resumed in a detached tmux
    session before the question is delivered.

    Modes:
      - ``wait_for_response=False`` (default, recommended): non-blocking.
        Returns immediately after the question is delivered to the target's
        inbox. The response, when it arrives, will be delivered back to YOU
        as a notification on your next turn.
      - ``wait_for_response=True``: blocks for up to ``timeout_seconds``
        (default 600s = 10min) waiting for the target to respond. Returns
        the response inline. If the timeout elapses, returns a "not yet
        responded" message — the response will still arrive as a
        notification when the target eventually answers.

    Args:
        target_name: Name of the agent to ask. Use ``list_agents`` to see
            available targets in your workspace.
        question: The question text to send.
        wait_for_response: If True, block waiting for a response.
        timeout_seconds: Max seconds to block (only used with
            ``wait_for_response=True``). Default 600.
    """
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    self_name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not self_name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME unset)."
        )

    if target_name == self_name:
        return "Error: cannot ask yourself. Use your own context directly."

    target = find_agent_by_name(cwd, target_name)
    if target is None:
        return (
            f"Error: no agent named '{target_name}' in this workspace. "
            "Call list_agents to see who's available."
        )

    # IMPORTANT: write the inbox file BEFORE auto-resuming, otherwise we
    # race claude's SessionStart hook (which fires within milliseconds of
    # spawn). If the hook fires first, it sees an empty inbox and the
    # question sits unread until UserPromptSubmit/Stop fires later.
    ask_id = new_ask_id()
    record = AskRecord(
        ask_id=ask_id,
        from_name=self_name,
        from_cwd=cwd,
        to_name=target_name,
        to_cwd=cwd,
        question=question,
        ts=time.time(),
        wait_for_response=wait_for_response,
    )
    save_ask(record)
    write_question_to_inbox(record)

    needs_resume = target.session is None or target.status == "ended"
    if needs_resume:
        try:
            _auto_resume(target, cwd)
        except Exception as e:  # noqa: BLE001 — surface anything as tool error
            return f"Error: failed to auto-resume '{target_name}': {e}"

    if not wait_for_response:
        suffix = " (target was dormant; auto-resumed)" if needs_resume else ""
        return (
            f"Question delivered to '{target_name}'{suffix}.\n"
            f"ask_id: {ask_id}\n"
            "You will be notified when they respond."
        )

    # Blocking: poll for the response file in our own inbox.
    deadline = time.time() + max(1, int(timeout_seconds))
    while time.time() < deadline:
        path = find_response_file(cwd, self_name, ask_id)
        if path is not None:
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                time.sleep(_POLL_INTERVAL)
                continue
            # Consume the response file so the deliver-inbox hook doesn't
            # also surface it later — we're delivering it inline right now.
            try:
                path.unlink()
            except OSError:
                pass
            delete_ask(ask_id)
            return (
                f"Response from '{data.get('from', target_name)}':\n\n"
                f"{data.get('body', '(empty response)')}"
            )
        time.sleep(_POLL_INTERVAL)

    return (
        f"'{target_name}' has not yet responded (waited "
        f"{int(timeout_seconds)}s).\n"
        f"ask_id: {ask_id}\n"
        "You will be notified when they respond — no need to ask again."
    )


@mcp.tool()
def respond_to_agent(ask_id: str, response: str) -> str:
    """Respond to a question received from another agent.

    Use the ``ask_id`` from the inbox notification you received. Your
    response will be delivered to the asking agent — either inline (if they
    used wait_for_response=True and are still waiting) or as a notification
    on their next turn (if they used the default non-blocking ask).

    Args:
        ask_id: The ask_id from the question notification you received.
        response: Your answer to the asker.
    """
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    self_name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not self_name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session."
        )

    record = load_ask(ask_id)
    if record is None:
        return (
            f"Error: no ask record found for ask_id={ask_id!r}. "
            "It may have already been responded to or expired."
        )
    if record.to_name != self_name:
        return (
            f"Error: ask_id={ask_id!r} was sent to '{record.to_name}', "
            f"not to you ('{self_name}'). You cannot respond on behalf of "
            "another agent."
        )

    write_response_to_inbox(
        asker_name=record.from_name,
        asker_cwd=record.from_cwd,
        responder_name=self_name,
        ask_id=ask_id,
        original_question=record.question,
        response_body=response,
    )
    delete_ask(ask_id)
    return f"Response delivered to '{record.from_name}' (ask_id={ask_id})."


def _auto_resume(target, cwd: str) -> None:
    """Spawn a multiverse session that resumes the dormant/ended target.

    Mirrors the monitor's resume flow but headless. The target's existing
    name is reused; collisions with an ended-but-still-recorded multiverse
    session are resolved by reaping that record first (the same kill-then-
    spawn step the dashboard's Enter-on-ended-row does).
    """
    if target.claude_uuid is None:
        raise RuntimeError(
            f"target '{target.name}' has no tracked claude UUID — "
            "cannot resume programmatically"
        )
    # If an ended multiverse session is occupying this name, reap it first
    # so create_session's name-collision check doesn't fire.
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
