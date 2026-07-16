#!/usr/bin/env python3
"""Persist Multiverse lifecycle state from Codex hook events."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


def _write_status(status_path: Path, status: str) -> None:
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({"status": status, "ts": int(time.time())}))
    except OSError:
        pass


def _set_native_name(session_id: str, name: str) -> None:
    """Best-effort native Codex rename through the app-server protocol."""
    codex_bin = os.environ.get("MULTIVERSE_CODEX_BIN", "codex")
    requests = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "multiverse", "version": "0.1.0"}
            },
        },
        {"method": "initialized"},
        {
            "id": 2,
            "method": "thread/name/set",
            "params": {"threadId": session_id, "name": name},
        },
    ]
    payload = "".join(json.dumps(item) + "\n" for item in requests)
    for attempt in range(3):
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                [codex_bin, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            output, _ = proc.communicate(payload, timeout=8)
            for line in output.splitlines():
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") == 2 and "error" not in response:
                    return
        except (OSError, subprocess.SubprocessError):
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
        time.sleep(0.4 * (attempt + 1))


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
            try:
                id_path.write_text(session_id + "\n")
            except OSError:
                pass

    # Write SessionStart's idle state after its native ID. The runner watches
    # for that ID as its readiness signal, so this ordering prevents an older
    # starting value from winning a startup race.
    status = STATUS_BY_EVENT.get(event_name)
    if status:
        _write_status(status_path, status)

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
        _set_native_name(sys.argv[2], sys.argv[3])
    else:
        _run_hook()
