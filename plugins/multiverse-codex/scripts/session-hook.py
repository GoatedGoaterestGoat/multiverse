#!/usr/bin/env python3
"""Persist Multiverse lifecycle state from Codex hook events."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path


STATUS_BY_EVENT = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PermissionRequest": "waiting",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Stop": "idle",
}
_SCRIPT = "codex-session-hook"


def _write_status(status_path: Path, status: str) -> None:
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{status_path.name}.",
            suffix=".tmp",
            dir=status_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"status": status, "ts": int(time.time())}, handle)
            os.replace(temp_path, status_path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        pass


def _write_text(path: Path, value: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(value)
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        pass


def _emit_secondbrain_signal(event: dict, event_name: str) -> None:
    """Record cheap turn/tool signals; the connector performs transcript expansion."""
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD", "")
    agent = os.environ.get("MULTIVERSE_SESSION_NAME", "")
    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT", "")
    if not cwd or not agent or not plugin_root:
        return
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)
    try:
        from src import codex_transcript, sb_notify
    except Exception:  # noqa: BLE001 - hooks must never block a Codex turn
        return

    raw_turn_id = event.get("turn_id")
    turn_id = raw_turn_id if isinstance(raw_turn_id, str) and raw_turn_id else None
    raw_session_id = event.get("session_id")
    session_id = (
        raw_session_id
        if isinstance(raw_session_id, str) and raw_session_id
        else None
    )
    try:
        # Subagent hooks carry the parent Multiverse session environment but do not
        # represent a dashboard-addressable agent. Attributing their approval modal to
        # the parent would make answer_permission_prompt type into the wrong TUI.
        if event.get("agent_id"):
            return

        if event_name == "UserPromptSubmit":
            raw_text = event.get("prompt")
            if not isinstance(raw_text, str) or not raw_text:
                sb_notify.log_hook_outcome(
                    _SCRIPT, "empty-prompt", cwd=cwd, agent=agent
                )
                return
            text = codex_transcript.clean_user_text(raw_text)
            if not text.strip():
                sb_notify.log_hook_outcome(
                    _SCRIPT, "filter-synthetic-prompt", cwd=cwd, agent=agent
                )
                return
            sb_notify.record_user_prompt_signal(
                cwd, agent, text, turn_id=turn_id
            )
            sb_notify.log_hook_outcome(
                _SCRIPT,
                "recorded-user-prompt",
                cwd=cwd,
                agent=agent,
                detail=f"turn_id={turn_id or '<unset>'} text_len={len(text)}",
            )
            return

        if event_name == "PreToolUse":
            tool_use_id = event.get("tool_use_id")
            tool_name = event.get("tool_name")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                return
            if not isinstance(tool_name, str) or not tool_name:
                return
            sb_notify.record_pending_tool(
                cwd,
                agent,
                tool_use_id,
                tool_name,
                event.get("tool_input"),
                runtime="codex",
                session_id=session_id,
                turn_id=turn_id,
            )
            sb_notify.log_hook_outcome(
                _SCRIPT,
                "recorded-pending-tool",
                cwd=cwd,
                agent=agent,
                detail=f"turn_id={turn_id or '<unset>'} tool_use_id={tool_use_id}",
            )
            return

        if event_name == "PermissionRequest":
            tool_name = event.get("tool_name")
            if (
                not session_id
                or not turn_id
                or not isinstance(tool_name, str)
                or not tool_name
            ):
                return
            candidate = sb_notify.pending_codex_tool_for_permission(
                cwd,
                agent,
                session_id,
                turn_id,
                tool_name,
                event.get("tool_input"),
            )
            if not candidate:
                sb_notify.log_hook_outcome(
                    _SCRIPT,
                    "permission-correlation-miss",
                    cwd=cwd,
                    agent=agent,
                    detail=f"turn_id={turn_id} tool_name={tool_name}",
                )
                return
            tool_use_id = str(candidate.get("tool_use_id") or "")
            if not tool_use_id:
                return
            sb_notify.record_permission_request_signal(
                cwd,
                agent,
                tool_use_id,
                tool_name,
                event.get("tool_input"),
                codex_turn_id=turn_id,
                await_user_confirmation=True,
            )
            sb_notify.log_hook_outcome(
                _SCRIPT,
                "recorded-permission-candidate",
                cwd=cwd,
                agent=agent,
                detail=f"turn_id={turn_id} tool_use_id={tool_use_id}",
            )
            return

        if event_name == "PostToolUse":
            tool_use_id = event.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                return
            had_user_prompt = sb_notify.had_permission_prompt(tool_use_id)
            if had_user_prompt:
                sb_notify.record_permission_decision_signal(
                    cwd,
                    agent,
                    tool_use_id,
                    "allow",
                    str(event.get("tool_name") or ""),
                    event.get("tool_input"),
                    event.get("tool_response"),
                )
            else:
                # PermissionRequest runs before Codex's automatic reviewer. Reaching
                # PostToolUse without the connector confirming a live approval overlay
                # means the request was auto-approved, so suppress the unrelayed
                # SecondBrain request candidate.
                sb_notify.delete_permission_request_signal_for_tool(tool_use_id)
            sb_notify.cleanup_pending_tool(tool_use_id)
            sb_notify.log_hook_outcome(
                _SCRIPT,
                "completed-tool",
                cwd=cwd,
                agent=agent,
                detail=f"turn_id={turn_id or '<unset>'} tool_use_id={tool_use_id}",
            )
            return

        if event_name == "Stop":
            # Auto-review denial has no PostToolUse. Drop its unconfirmed request
            # candidate before sweeping actual user-facing prompts as denied.
            discarded = sb_notify.discard_unconfirmed_codex_permissions(
                cwd, agent, turn_id=turn_id
            )
            if discarded:
                sb_notify.log_hook_outcome(
                    _SCRIPT,
                    "discarded-auto-reviewed-permissions",
                    cwd=cwd,
                    agent=agent,
                    detail=f"count={discarded}",
                )
            # A denied/cancelled approval never reaches PostToolUse. Once Codex stops
            # the turn, any marked candidate left behind is therefore the denied half
            # of the permission pair, matching Claude's Stop-sweep semantics.
            closed = sb_notify.sweep_unresolved_permissions(cwd, agent)
            if closed:
                sb_notify.log_hook_outcome(
                    _SCRIPT,
                    "closed-denied-permissions",
                    cwd=cwd,
                    agent=agent,
                    detail=f"count={closed}",
                )
            message = event.get("last_assistant_message")
            if not isinstance(message, str) or not message.strip():
                sb_notify.log_hook_outcome(
                    _SCRIPT, "empty-response", cwd=cwd, agent=agent
                )
                return
            sb_notify.record_response_signal(cwd, agent, turn_id=turn_id)
            sb_notify.log_hook_outcome(
                _SCRIPT,
                "recorded-response",
                cwd=cwd,
                agent=agent,
                detail=f"turn_id={turn_id or '<unset>'}",
            )
    except Exception as exc:  # noqa: BLE001 - hooks must never block a Codex turn
        sb_notify.log_hook_outcome(
            _SCRIPT, "exception", cwd=cwd, agent=agent, detail=str(exc)[:200]
        )


def _run_hook() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return

    status_value = os.environ.get("MULTIVERSE_STATUS")
    if not status_value:
        return
    status_path = Path(status_value)
    event_name = event.get("hook_event_name")
    if event_name == "SessionStart":
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            id_path = Path(
                os.environ.get("MULTIVERSE_RUNTIME_SESSION_ID_PATH")
                or status_path.parent / "runtime_session_id"
            )
            _write_text(id_path, session_id + "\n")

    # Write SessionStart's idle state after its native ID. The runner watches
    # for that ID as its readiness signal, so this ordering prevents an older
    # starting value from winning a startup race.
    status = STATUS_BY_EVENT.get(event_name)
    if status:
        _write_status(status_path, status)

    if event_name in (
        "UserPromptSubmit",
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "Stop",
    ):
        _emit_secondbrain_signal(event, event_name)

if __name__ == "__main__":
    _run_hook()
