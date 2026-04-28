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
  - spawn_agent                   — start a brand-new subagent in the
                                    workspace with the calling agent's
                                    instruction as its first user message
                                    (user confirms+names via a monitor
                                    modal)
"""

from __future__ import annotations

import json
import os
import subprocess
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
    existing_names_in_cwd,
    find_agent_by_name,
    list_named_agents_in_cwd,
)
from src.inbox import write_message_to_inbox
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


def _record_superseded_uuid(old_uuid: str) -> None:
    """Append the pre-fold claude UUID to this multiverse session's meta.json.

    Lets the dashboard render the old JSONL as `Name (folded HH:MM)` and
    keeps it filtered out of the inter-agent registry. No-op when not running
    under the wrapper (manual `claude --plugin-dir` mode has no meta.json to
    update — the user resumes the new session id manually).
    """
    if not old_uuid:
        return
    control_path = os.environ.get("MULTIVERSE_CONTROL")
    if not control_path:
        return
    state_dir = Path(control_path).parent
    meta_path = state_dir / "meta.json"
    try:
        with open(meta_path, "r") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(meta, dict):
        return
    superseded = meta.get("superseded_uuids")
    if not isinstance(superseded, list):
        superseded = []
    superseded.append({"uuid": old_uuid, "folded_at": time.time()})
    meta["superseded_uuids"] = superseded
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(meta, f)
        os.replace(tmp, meta_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


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

        # Mark the pre-fold JSONL as a ghost in this session's meta.json
        # before signaling resume — the dashboard reads meta.json on its
        # next refresh.
        _record_superseded_uuid(session_id)

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
                claude_args=[instruction],
                name=chosen,
            )
        except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
            return f"Error: failed to create session: {e}"

        return f"Spawned agent with name '{chosen}'."
    finally:
        cleanup_request(req_id)


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
