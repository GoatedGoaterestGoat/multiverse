"""Multiverse monitor — Textual TUI for orchestrating multiple claude sessions.

Each claude process runs inside a detached tmux session. The monitor tracks
them via per-session files under ``~/.multiverse/sessions/<id>/``:

  meta.json    — cwd, created_at, claude_args (written on spawn)
  status.json  — {"status": ..., "ts": ...} (written by hook scripts)
  control.json — existing context-fold signaling channel

Attach = ``tmux attach-session`` run while the Textual app is suspended.
Detach with normal ``Ctrl-b d`` and you land back in the monitor.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static


STATE_DIR = Path(os.path.expanduser("~/.multiverse/sessions"))
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
TMUX_PREFIX = "multiverse-"


def short_id() -> str:
    return secrets.token_hex(4)


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _extract_user_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                return str(block.get("text", ""))
    return ""


def _extract_preview(path: Path, max_lines: int = 80, max_chars: int = 80) -> str:
    try:
        with open(path, "r") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if raw.get("type") != "user":
                    continue
                msg = raw.get("message") or {}
                if msg.get("role") != "user":
                    continue
                text = _extract_user_text(msg.get("content")).strip()
                if not text or text.startswith("<"):
                    continue
                text = text.replace("\n", " ")
                if len(text) > max_chars:
                    text = text[: max_chars - 1] + "…"
                return text
    except OSError:
        return ""
    return "(no user message)"


def list_claude_sessions_in(cwd: str) -> list[dict]:
    """Return claude sessions under ``cwd``, newest first.

    Each dict has: uuid, preview, mtime, size.
    """
    cwd = cwd.strip()
    if not cwd:
        return []
    project_dir = CLAUDE_PROJECTS_DIR / _encode_cwd(cwd)
    if not project_dir.is_dir():
        return []
    out: list[dict] = []
    for path in project_dir.glob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        out.append(
            {
                "uuid": path.stem,
                "preview": _extract_preview(path),
                "mtime": st.st_mtime,
                "size": st.st_size,
            }
        )
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


@dataclass
class Session:
    id: str
    cwd: str
    created_at: float
    status: str = "unknown"
    status_ts: float = 0.0
    resumed_from: Optional[str] = None

    @property
    def tmux_name(self) -> str:
        return f"{TMUX_PREFIX}{self.id}"

    @property
    def dir(self) -> Path:
        return STATE_DIR / self.id

    @property
    def status_path(self) -> Path:
        return self.dir / "status.json"

    @property
    def control_path(self) -> Path:
        return self.dir / "control.json"

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load_session(session_id: str) -> Optional[Session]:
    d = STATE_DIR / session_id
    meta = _read_json(d / "meta.json")
    if meta is None:
        return None
    s = Session(
        id=session_id,
        cwd=meta.get("cwd", "?"),
        created_at=meta.get("created_at", 0.0),
        resumed_from=meta.get("resumed_from"),
    )
    st = _read_json(s.status_path)
    if st:
        s.status = st.get("status", "unknown")
        s.status_ts = float(st.get("ts", 0) or 0)
    return s


def list_sessions() -> list[Session]:
    if not STATE_DIR.exists():
        return []
    out: list[Session] = []
    for entry in sorted(STATE_DIR.iterdir()):
        if not entry.is_dir():
            continue
        s = load_session(entry.name)
        if s is not None:
            out.append(s)
    out.sort(key=lambda s: s.created_at)
    return out


def tmux_session_exists(name: str) -> bool:
    r = subprocess.run(
        ["tmux", "has-session", "-t", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def create_session(
    cwd: str,
    claude_args: list[str],
    resumed_from: Optional[str] = None,
) -> Session:
    sid = short_id()
    d = STATE_DIR / sid
    d.mkdir(parents=True, exist_ok=True)

    cwd_abs = str(Path(cwd).expanduser().resolve())
    if not Path(cwd_abs).is_dir():
        raise ValueError(f"Not a directory: {cwd_abs}")

    now = time.time()
    meta: dict[str, Any] = {
        "cwd": cwd_abs,
        "created_at": now,
        "claude_args": claude_args,
    }
    if resumed_from:
        meta["resumed_from"] = resumed_from
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "status.json").write_text(json.dumps({"status": "starting", "ts": now}))
    (d / "control.json").write_text("")

    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT")
    if not plugin_root:
        raise RuntimeError("MULTIVERSE_PLUGIN_ROOT not set — launch via bin/multiverse")
    inner = str(Path(plugin_root) / "bin" / "multiverse-session")

    inner_cmd = shlex.join([inner, *claude_args])

    tmux_name = f"{TMUX_PREFIX}{sid}"
    cmd = [
        "tmux", "new-session", "-d",
        "-s", tmux_name,
        "-c", cwd_abs,
        "-e", f"MULTIVERSE_SESSION_ID={sid}",
        "-e", f"MULTIVERSE_CONTROL={d / 'control.json'}",
        "-e", f"MULTIVERSE_STATUS={d / 'status.json'}",
        "-e", f"MULTIVERSE_PLUGIN_ROOT={plugin_root}",
        inner_cmd,
    ]
    subprocess.run(cmd, check=True)
    return Session(
        id=sid,
        cwd=cwd_abs,
        created_at=now,
        status="starting",
        status_ts=now,
        resumed_from=resumed_from,
    )


def kill_session(s: Session) -> None:
    if tmux_session_exists(s.tmux_name):
        subprocess.run(
            ["tmux", "kill-session", "-t", s.tmux_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        shutil.rmtree(s.dir)
    except OSError:
        pass


STATUS_STYLES = {
    "idle": "dim",
    "working": "bold yellow",
    "waiting": "bold red",
    "starting": "cyan",
    "ended": "dim strike",
    "unknown": "dim",
}


class NewSessionScreen(ModalScreen[Optional[str]]):
    """Prompt the user for a working directory for a new session."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, default_cwd: str) -> None:
        super().__init__()
        self.default_cwd = default_cwd

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("New session — working directory (Enter to confirm, Esc to cancel):"),
            Input(value=self.default_cwd, id="new-cwd"),
            id="new-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#new-cwd", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ResumeSessionScreen(ModalScreen[Optional[tuple[str, str]]]):
    """Browse claude sessions under a cwd and pick one to resume."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, default_cwd: str) -> None:
        super().__init__()
        self.default_cwd = default_cwd
        self.sessions_info: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Resume claude session — edit cwd, then pick a session:"),
            Input(value=self.default_cwd, id="resume-cwd"),
            DataTable(cursor_type="row", zebra_stripes=True, id="resume-table"),
            Static("Enter on a row to resume · Esc to cancel", id="resume-hint"),
            id="resume-dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one("#resume-table", DataTable)
        table.add_columns("UUID", "Preview", "Updated")
        self._populate()
        if self.sessions_info:
            table.focus()
            try:
                table.move_cursor(row=0)
            except Exception:
                pass
        else:
            self.query_one("#resume-cwd", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "resume-cwd":
            self._populate()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Tab focus from the cwd input into the session table.
        if event.input.id == "resume-cwd" and self.sessions_info:
            self.query_one("#resume-table", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value if event.row_key is not None else None
        if not key:
            return
        cwd = self.query_one("#resume-cwd", Input).value.strip()
        self.dismiss((cwd, str(key)))

    def _populate(self) -> None:
        cwd = self.query_one("#resume-cwd", Input).value
        self.sessions_info = list_claude_sessions_in(cwd)
        table = self.query_one("#resume-table", DataTable)
        table.clear()
        now = time.time()
        for info in self.sessions_info:
            preview = info["preview"] or "(empty)"
            table.add_row(
                info["uuid"][:8],
                preview,
                _ago(now - info["mtime"]),
                key=info["uuid"],
            )

    def action_cancel(self) -> None:
        self.dismiss(None)


class MonitorApp(App):
    CSS = """
    Screen {
        layers: base modal;
    }
    DataTable {
        height: 1fr;
    }
    #new-dialog {
        width: 80;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #resume-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #resume-dialog DataTable {
        height: 1fr;
        margin-top: 1;
    }
    #resume-hint {
        color: $text-muted;
        padding-top: 1;
        height: 1;
    }
    """

    BINDINGS = [
        Binding("n", "new_session", "New"),
        Binding("R", "resume_session", "Resume"),
        Binding("enter", "attach", "Attach"),
        Binding("k", "kill", "Kill"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, claude_args: list[str]) -> None:
        super().__init__()
        self.claude_args = claude_args
        self.sessions: list[Session] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        table = DataTable(cursor_type="row", zebra_stripes=True)
        table.add_columns("ID", "Status", "CWD", "Updated")
        yield table
        yield Footer()

    def on_mount(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.title = "Multiverse Monitor"
        self.sub_title = "n new · R resume · enter attach · k kill · r refresh · q quit"
        self.refresh_sessions()
        self.set_interval(1.0, self.refresh_sessions)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_attach()

    def refresh_sessions(self) -> None:
        self.sessions = list_sessions()
        for s in self.sessions:
            if s.status not in ("ended",) and not tmux_session_exists(s.tmux_name):
                s.status = "ended"
        self._redraw_table()

    def _redraw_table(self) -> None:
        table = self.query_one(DataTable)
        prev_cursor = table.cursor_row
        table.clear()
        now = time.time()
        for s in self.sessions:
            style = STATUS_STYLES.get(s.status, "dim")
            status_cell = f"[{style}]{s.status}[/]"
            updated = _ago(now - s.status_ts) if s.status_ts else "-"
            cwd = s.cwd
            if len(cwd) > 48:
                cwd = "..." + cwd[-45:]
            table.add_row(s.id, status_cell, cwd, updated, key=s.id)
        if self.sessions:
            row = max(0, min(prev_cursor or 0, len(self.sessions) - 1))
            try:
                table.move_cursor(row=row)
            except Exception:
                pass

    def _current_session(self) -> Optional[Session]:
        if not self.sessions:
            return None
        table = self.query_one(DataTable)
        idx = table.cursor_row or 0
        if 0 <= idx < len(self.sessions):
            return self.sessions[idx]
        return None

    def action_new_session(self) -> None:
        default = os.environ.get("MULTIVERSE_ORIG_CWD") or os.getcwd()

        def cb(result: Optional[str]) -> None:
            if not result:
                return
            try:
                create_session(result, self.claude_args)
            except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
                self.notify(f"Spawn failed: {e}", severity="error")
                return
            self.refresh_sessions()
            self.notify("Session spawned")

        self.push_screen(NewSessionScreen(default), cb)

    def action_resume_session(self) -> None:
        default = os.environ.get("MULTIVERSE_ORIG_CWD") or os.getcwd()

        def cb(result: Optional[tuple[str, str]]) -> None:
            if not result:
                return
            cwd, uuid = result
            # De-dup: if a live multiverse session already wraps this claude
            # uuid, attach to that instance instead of spawning a fork.
            self.refresh_sessions()
            for i, s in enumerate(self.sessions):
                if (
                    s.resumed_from == uuid
                    and s.status != "ended"
                    and tmux_session_exists(s.tmux_name)
                ):
                    table = self.query_one(DataTable)
                    try:
                        table.move_cursor(row=i)
                    except Exception:
                        pass
                    self.notify(f"Already running as {s.id} — attaching")
                    self.action_attach()
                    return
            try:
                create_session(cwd, ["--resume", uuid], resumed_from=uuid)
            except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
                self.notify(f"Resume failed: {e}", severity="error")
                return
            self.refresh_sessions()
            self.notify(f"Resumed {uuid[:8]}")

        self.push_screen(ResumeSessionScreen(default), cb)

    def action_attach(self) -> None:
        s = self._current_session()
        if s is None:
            return
        if not tmux_session_exists(s.tmux_name):
            self.notify(f"Session {s.id} is not running", severity="warning")
            return
        with self.suspend():
            subprocess.run(["tmux", "attach-session", "-t", s.tmux_name])
        self.refresh_sessions()

    def action_kill(self) -> None:
        s = self._current_session()
        if s is None:
            return
        kill_session(s)
        self.notify(f"Killed {s.id}")
        self.refresh_sessions()

    def action_refresh(self) -> None:
        self.refresh_sessions()


def _ago(delta: float) -> str:
    if delta < 0:
        return "-"
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta / 60)}m"
    return f"{int(delta / 3600)}h"


def main() -> None:
    MonitorApp(claude_args=sys.argv[1:]).run()


if __name__ == "__main__":
    main()
