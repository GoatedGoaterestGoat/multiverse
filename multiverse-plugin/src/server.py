"""Multiverse MCP server — agent-initiated selective context folding for Claude Code."""

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

mcp = FastMCP("context-fold")


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


if __name__ == "__main__":
    mcp.run()
