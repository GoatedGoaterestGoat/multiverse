#!/usr/bin/env python3
"""Persist Multiverse lifecycle state from Codex hook events."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
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


def _wait_for_response(
    messages: queue.Queue[str | None], request_id: int, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            line = messages.get(timeout=remaining)
        except queue.Empty:
            return False
        if line is None:
            return False
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue
        if response.get("id") == request_id:
            return "error" not in response and isinstance(response.get("result"), dict)


def _native_name_attempt(codex_bin: str, session_id: str, name: str) -> bool:
    """Set a name once, keeping stdio open until Codex acknowledges it."""
    proc: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    messages: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    def send(payload: dict) -> None:
        assert proc is not None and proc.stdin is not None
        proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    try:
        proc = subprocess.Popen(
            [codex_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if proc.stdin is None or proc.stdout is None:
            return False
        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "multiverse", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        if not _wait_for_response(messages, 1, 8):
            return False
        send({"method": "initialized"})
        send(
            {
                "id": 2,
                "method": "thread/name/set",
                "params": {"threadId": session_id, "name": name},
            }
        )
        return _wait_for_response(messages, 2, 8)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        proc.kill()
                        proc.wait(timeout=1)
                    except OSError:
                        pass
                    except subprocess.TimeoutExpired:
                        pass
            if reader is not None:
                reader.join(timeout=0.2)
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass


def _set_native_name(session_id: str, name: str) -> bool:
    """Best-effort native Codex rename through the app-server protocol."""
    codex_bin = os.environ.get("MULTIVERSE_CODEX_BIN", "codex")
    for attempt in range(8):
        if _native_name_attempt(codex_bin, session_id, name):
            return True
        if attempt < 7:
            time.sleep(min(0.4 * (2**attempt), 3.0))
    return False


def _emit_secondbrain_signal(event: dict, event_name: str) -> None:
    """Record cheap turn signals; the connector performs transcript expansion."""
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
    try:
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

        if event_name == "Stop":
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

    if event_name in ("UserPromptSubmit", "Stop"):
        _emit_secondbrain_signal(event, event_name)

    if event_name != "SessionStart":
        return
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return

    name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if name:
        try:
            subprocess.Popen(
                [sys.executable, __file__, "--set-name", session_id, name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--set-name":
        raise SystemExit(0 if _set_native_name(sys.argv[2], sys.argv[3]) else 1)
    else:
        _run_hook()
