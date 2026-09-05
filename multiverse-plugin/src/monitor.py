"""Multiverse monitor — Textual TUI for orchestrating runtime sessions.

Each runtime process runs inside a detached tmux session. The monitor tracks
them via per-session files under ``~/.multiverse/sessions/<id>/``:

  meta.json    — cwd, runtime, created_at, runtime args (written on spawn)
  status.json  — {"status": ..., "ts": ...} (written by hook scripts)
  claude_uuid / runtime_session_id — native runtime session identifier

The dashboard also surfaces dormant, named Claude and Codex sessions that are
not currently wrapped by a Multiverse session. These can be resumed directly
with Enter.

Attach = ``tmux attach-session`` run through a local terminal-probe relay while
the Textual app is suspended. Detach with normal ``Ctrl-b d`` and you land back
in the monitor.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional

from rich.markup import escape
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    ProgressBar,
    RadioButton,
    RadioSet,
    RichLog,
    Static,
)

from .ask_agents import AskResult, AskTarget, ask_all, ask_one
from .auto_build import is_enabled as auto_build_enabled, toggle as auto_build_toggle
from .compact_runner import CompactRunner, State as CompactState
from .compact_state import acquire_lock, release_lock
from .full_jsonl import (
    full_jsonl_path,
    list_orphan_fulls,
    live_jsonl_path,
    live_stats,
    last_real_usage_total_cached,
    live_stats_cached,
    minimum_compacted_tokens,
    structural_tokens,
    structural_tokens_cached,
    sync_full,
    total_token_count,
    total_turn_count,
)
from .paths import multiverse_dir
from .pi_transcript import delete_pi_session, list_pi_sessions_in
from .inbox import list_pending_messages, pending_inbox_count
from .names import pick_random_name, successor_name as pick_successor_name
from .sessions import (
    RUNTIME_CLAUDE,
    RUNTIME_CODEX,
    RUNTIME_PI,
    RUNTIMES,
    STATE_DIR,
    TMUX_PREFIX,
    DashboardRow,
    Session,
    all_session_names_in_cwd,
    build_dashboard_rows,
    create_session,
    create_retired_session_record,
    current_host,
    delete_codex_thread,
    existing_names_in_cwd,
    foreign_host_still_reported_live,
    kill_session,
    list_claude_sessions_in,
    list_codex_sessions_in,
    list_sessions,
    mark_session_ending,
    remove_retired_session_record,
    strip_fold_suffix,
    tmux_session_exists,
    wrapped_runtime_session_ids,
)
from .spawn_requests import (
    list_pending_requests,
    read_resolution,
    request_path,
    write_resolution,
)
from .tmux_attach import attach_tmux_session
from . import groups
from . import web_share
from . import sb_control


STATUS_STYLES = {
    "idle": "dim",
    "working": "bold yellow",
    "waiting": "bold red",
    "listening": "bold cyan",
    "starting": "cyan",
    "ending": "bold red",
    "ended": "dim strike",
    "retired": "bold blue strike",
    "dormant": "blue",
    "compacting": "bold magenta",
    "unknown": "dim",
}

# Keys that move the table cursor. Pressing one ends a shift-selection.
_CURSOR_MOVE_KEYS = frozenset(
    {"up", "down", "home", "end", "pageup", "pagedown", "ctrl+home", "ctrl+end"}
)

# Which status a folder header reports when its members disagree: the one
# most likely to need the user. Lower rank wins.
_GROUP_STATUS_RANK = {
    "waiting": 0,
    "compacting": 1,
    "working": 2,
    "listening": 3,
    "starting": 4,
    "ending": 5,
    "idle": 6,
    "dormant": 7,
    "ended": 8,
    # Retired agents are handed off and never coming back, so a folder
    # full of them should not out-rank one holding anything still usable.
    "retired": 9,
    "unknown": 10,
}

# Statuses where the user is allowed to launch a compaction. Working/
# waiting/starting/compacting are excluded — the session is busy or
# already locked.
_COMPACTABLE_STATUSES = {"idle", "ended", "retired", "dormant"}
_ENDING_MIN_VISIBLE_SECONDS = 0.25
_HANDOFF_TIMEOUT_SECONDS = 300.0
# How long the monitor keeps an approved spawn on file waiting for the
# agent to actually create it, and how long it waits after the request
# file disappears before giving up on the row showing up.
_SPAWN_FILING_TTL = 300.0
_SPAWN_FILING_GRACE = 30.0

_HANDOFF_PROMPT = """You are being retired due to context-length limitations, and your responsibilities will be inherited by a successor agent.

Produce a concise, self-contained handoff context for your successor using only what you know from this conversation.

Include:

- General project background and important constraints
- Your responsibilities and completed work
- Currently open tasks, blockers, and unresolved decisions
- Full paths to important files, scripts, directories, and assets
- Important experience and lessons you learned
- Anything else that you feel is important for your successor agent to know

Preserve the concrete details needed to continue the work without repeating completed tasks. Clearly distinguish completed work from open work. Output only the handoff context."""


def _successor_prompt(predecessor_name: str, handoff_context: str) -> str:
    return f"""You are the successor to the retired agent "{predecessor_name}" in this workspace. You now own their responsibilities and outstanding work.

Your predecessor has provided you the following historical project context as well as your responsibilities:

{handoff_context.strip()}

Verify the current workspace and repository state before making changes, because files or external state may have changed since the report was produced. Preserve existing user work and do not repeat work that is already complete.

If you need clarification about prior decisions, reasoning, or experience, you may use:

ask_agent(target_name="{predecessor_name}", question="...")

The predecessor is retired: it cannot receive message_agent calls or new prompts, and it cannot use tools. ask_agent consults only its frozen context.

Begin by reconciling this report with the current workspace state. Then continue the outstanding work autonomously. Surface any decision that genuinely requires the user, and clearly report blockers rather than inventing missing context."""


class _SpawnAttachGuard:
    """Background watcher while the user is attached to a tmux session.

    Polls ``~/.multiverse/spawn_requests/`` every second for pending requests
    created by the exact tmux session being guarded. When one appears, finds
    the tmux client process launched by this monitor and detaches that exact
    client to return the user to the naming modal (Textual's event loop is
    suspended during attach, so the normal tick can't fire).

    Filtering by ``caller_tmux_session`` is important when several monitors
    are attached in one workspace. A request from one agent must not detach
    unrelated clients, especially abandoned terminals whose output queues
    can block the shared tmux server during detach.

    ``forced_detach`` is set to True when we issue the detach so the
    caller can schedule a reattach once the queue has fully drained.
    """

    _POLL_INTERVAL = 1.0

    def __init__(self, cwd: str, tmux_session: str) -> None:
        self.cwd = cwd
        self.tmux_session = tmux_session
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client_pid: Optional[int] = None
        self.forced_detach = False

    def set_client_pid(self, pid: int) -> None:
        """Identify the tmux attach client owned by this monitor."""
        self._client_pid = pid

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                pending = [
                    request
                    for request in list_pending_requests(self.cwd)
                    if request.get("caller_tmux_session") == self.tmux_session
                ]
            except Exception:
                pending = []
            if pending and self._detach_client():
                self.forced_detach = True
                return  # one detach is all we need
            if self._stop.wait(self._POLL_INTERVAL):
                return

    def _detach_client(self) -> bool:
        """Detach this monitor's client, retrying later on races or tmux errors."""
        if self._client_pid is None:
            return False
        try:
            listed = subprocess.run(
                [
                    "tmux",
                    "list-clients",
                    "-F",
                    "#{client_pid}\t#{client_name}\t#{session_name}",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            client_name = None
            for line in listed.stdout.splitlines():
                fields = line.split("\t", 2)
                if len(fields) != 3:
                    continue
                pid, name, session = fields
                if pid == str(self._client_pid) and session == self.tmux_session:
                    client_name = name
                    break
            if client_name is None:
                return False
            subprocess.run(
                ["tmux", "detach-client", "-t", client_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=True,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False


@dataclass
class SpawnDecision:
    """What the user chose in :class:`SpawnRequestScreen`.

    Distinct from a ``None`` dismissal, which means the modal was closed
    externally (another monitor resolved the request first) rather than by a
    person — only a real rejection can carry a reason to send back.
    """

    accepted: bool
    name: str = ""
    runtime: str = ""
    reason: str = ""


class SpawnRequestScreen(ModalScreen[Optional[SpawnDecision]]):
    """Approval widget for an agent-initiated spawn request.

    Returns a :class:`SpawnDecision` for either choice the user makes, or
    ``None`` when dismissed externally (another monitor resolved the request
    first) — in that case ``write_resolution`` no-ops via O_EXCL.

    Rejecting can carry a short explanation, which is handed back to the
    requesting agent as its tool result. Without it the agent only learns
    "rejected", and its usual next move is to ask again — the reason is what
    lets it adjust or drop the idea instead.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Create", show=False, priority=True),
    ]

    _PREVIEW_CHARS = 280

    def __init__(self, cwd: str, request: dict) -> None:
        super().__init__()
        self.cwd = cwd
        self.request = request
        seeded = (request.get("default_name") or "").strip()
        names = all_session_names_in_cwd(cwd)
        if not seeded or seeded in names:
            seeded = pick_random_name(names) or ""
        self.default_name = seeded
        requested_runtime = request.get("caller_runtime")
        self.caller_runtime = (
            requested_runtime
            if requested_runtime in (RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_PI)
            else RUNTIME_CLAUDE
        )

    def compose(self) -> ComposeResult:
        from_name = self.request.get("from", "?")
        instruction = self.request.get("instruction", "") or ""
        preview = instruction[: self._PREVIEW_CHARS]
        if len(instruction) > self._PREVIEW_CHARS:
            preview = preview.rstrip() + "…"

        yield Vertical(
            Static(
                f"[bold]'{escape(from_name)}'[/] ({self.caller_runtime}) wants to "
                "spawn a subagent:",
                id="spawn-header",
            ),
            Static(
                f"[dim italic]{escape(preview) or '(empty instruction)'}[/]",
                id="spawn-preview",
            ),
            Static("Name the new agent:", id="spawn-prompt"),
            Input(value=self.default_name, id="spawn-name"),
            Static("Child runtime:", id="spawn-runtime-prompt"),
            RadioSet(
                RadioButton(
                    "Claude",
                    value=self.caller_runtime == RUNTIME_CLAUDE,
                    id="spawn-runtime-claude",
                ),
                RadioButton(
                    "Codex",
                    value=self.caller_runtime == RUNTIME_CODEX,
                    id="spawn-runtime-codex",
                ),
                RadioButton(
                    "pi",
                    value=self.caller_runtime == RUNTIME_PI,
                    id="spawn-runtime-pi",
                ),
                id="spawn-runtime",
            ),
            Static("Reason, if rejecting (optional):", id="spawn-reject-prompt"),
            Input(
                placeholder="Sent back to the agent — e.g. why, or what to do instead",
                id="spawn-reject-reason",
            ),
            Static("", id="spawn-error"),
            Static(
                "Tab to move · Enter to accept · Esc to reject "
                "(Enter in the reason box rejects too)",
                id="spawn-hint",
            ),
            id="spawn-dialog",
        )

    def on_mount(self) -> None:
        inp = self.query_one("#spawn-name", Input)
        inp.focus()
        try:
            inp.cursor_position = len(inp.value)
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "spawn-reject-reason":
            self.action_cancel()
            return
        self._try_submit()

    def action_submit(self) -> None:
        # Enter is bound at screen level with priority, so it fires even while
        # the reason box has focus. Typing a rejection and pressing Enter must
        # reject, not silently create the agent you were declining.
        focused = self.focused
        if focused is not None and getattr(focused, "id", None) == "spawn-reject-reason":
            self.action_cancel()
            return
        self._try_submit()

    def _reject_reason(self) -> str:
        try:
            return self.query_one("#spawn-reject-reason", Input).value.strip()
        except Exception:
            # Dismissed before compose finished (external resolve).
            return ""

    def _try_submit(self) -> None:
        name = self.query_one("#spawn-name", Input).value.strip()
        err = self.query_one("#spawn-error", Static)
        if not name:
            err.update("[red]Name cannot be empty.[/]")
            return
        if name in all_session_names_in_cwd(self.cwd):
            err.update(
                f"[red]'{escape(name)}' is already used in this workspace.[/]"
            )
            return
        if self.query_one("#spawn-runtime-pi", RadioButton).value:
            runtime = RUNTIME_PI
        elif self.query_one("#spawn-runtime-codex", RadioButton).value:
            runtime = RUNTIME_CODEX
        else:
            runtime = RUNTIME_CLAUDE
        self.dismiss(SpawnDecision(accepted=True, name=name, runtime=runtime))

    def action_cancel(self) -> None:
        self.dismiss(SpawnDecision(accepted=False, reason=self._reject_reason()))


class HandoffSuccessorScreen(ModalScreen[Optional[tuple[str, str]]]):
    """Review the hand-off report, then name the successor.

    A hand-off is the one spawn the user cannot sanity-check anywhere
    else: the predecessor wrote the report to its own successor, and once
    the successor starts, that report is its entire inherited memory. So
    it gets the same treatment as an agent-initiated spawn — the text is
    on screen, in full, before anything is created — rather than the
    in-row draft `n` uses, where there is nothing to read.

    Returns ``(name, runtime)``, or ``None`` if the user backed out.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Create", show=False, priority=True),
    ]

    def __init__(
        self,
        cwd: str,
        predecessor: str,
        predecessor_runtime: str,
        handoff_context: str,
        default_name: str,
    ) -> None:
        super().__init__()
        self.cwd = cwd
        self.predecessor = predecessor
        self.predecessor_runtime = (
            predecessor_runtime
            if predecessor_runtime in (RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_PI)
            else RUNTIME_CLAUDE
        )
        self.handoff_context = handoff_context
        self.default_name = default_name

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                f"[bold]'{escape(self.predecessor)}'[/] is retiring. Its "
                "successor inherits this report:",
                id="handoff-header",
            ),
            VerticalScroll(
                Static(
                    escape(self.handoff_context) or "[dim](empty report)[/]",
                    id="handoff-context",
                ),
                id="handoff-context-scroll",
            ),
            Static("Name the successor:", id="handoff-prompt"),
            Input(value=self.default_name, id="handoff-name"),
            Static("Successor runtime:", id="handoff-runtime-prompt"),
            RadioSet(
                RadioButton(
                    "Claude",
                    value=self.predecessor_runtime == RUNTIME_CLAUDE,
                    id="handoff-runtime-claude",
                ),
                RadioButton(
                    "Codex",
                    value=self.predecessor_runtime == RUNTIME_CODEX,
                    id="handoff-runtime-codex",
                ),
                RadioButton(
                    "pi",
                    value=self.predecessor_runtime == RUNTIME_PI,
                    id="handoff-runtime-pi",
                ),
                id="handoff-runtime",
            ),
            Static("", id="handoff-error"),
            Static(
                "Tab to move · Enter to create · Esc to cancel "
                "(nothing is retired until you create)",
                id="handoff-hint",
            ),
            id="handoff-dialog",
        )

    def on_mount(self) -> None:
        inp = self.query_one("#handoff-name", Input)
        inp.focus()
        try:
            inp.cursor_position = len(inp.value)
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_submit()

    def action_submit(self) -> None:
        self._try_submit()

    def _try_submit(self) -> None:
        name = self.query_one("#handoff-name", Input).value.strip()
        err = self.query_one("#handoff-error", Static)
        if not name:
            err.update("[red]Name cannot be empty.[/]")
            return
        if name in all_session_names_in_cwd(self.cwd):
            # The predecessor keeps its name when it retires, so this also
            # catches "just call it the same thing".
            err.update(
                f"[red]'{escape(name)}' is already used in this workspace.[/]"
            )
            return
        if self.query_one("#handoff-runtime-pi", RadioButton).value:
            runtime = RUNTIME_PI
        elif self.query_one("#handoff-runtime-codex", RadioButton).value:
            runtime = RUNTIME_CODEX
        else:
            runtime = RUNTIME_CLAUDE
        self.dismiss((name, runtime))

    def action_cancel(self) -> None:
        self.dismiss(None)


class NameInputScreen(ModalScreen[Optional[str]]):
    """Name-only modal used by the resume flow.

    Validates non-empty and uniqueness within ``cwd`` before dismissing.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, cwd: str, default_name: str, reason: str) -> None:
        super().__init__()
        self.cwd = cwd
        self.default_name = default_name
        self.reason = reason

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.reason, id="name-reason"),
            Input(value=self.default_name, id="name-input"),
            Static("", id="name-error"),
            Static("Esc to go back", id="name-hint"),
            id="name-dialog",
        )

    def on_mount(self) -> None:
        inp = self.query_one("#name-input", Input)
        inp.focus()
        # Place cursor at end of prefilled name for easy editing
        try:
            inp.cursor_position = len(inp.value)
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        err = self.query_one("#name-error", Static)
        if not name:
            err.update("[red]Name cannot be empty.[/]")
            return
        if name in all_session_names_in_cwd(self.cwd):
            err.update(
                f"[red]'{escape(name)}' is already used in this workspace. "
                "Choose another.[/]"
            )
            return
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Yes/no confirmation before deleting a dormant native session.

    Returns ``True`` on confirm (Enter), ``False`` on cancel (Esc).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    def __init__(self, display_name: str, runtime: str = RUNTIME_CLAUDE) -> None:
        super().__init__()
        self.display_name = display_name
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        description = (
            "This permanently deletes its Codex conversation data. "
            if self.runtime == RUNTIME_CODEX
            else "This permanently deletes its JSONL from disk. "
        )
        yield Vertical(
            Static(
                f"Delete dormant session [bold]'{escape(self.display_name)}'[/]?",
                id="confirm-delete-header",
            ),
            Static(
                description + "This cannot be undone.",
                id="confirm-delete-body",
            ),
            Static("Enter to confirm · Esc to cancel", id="confirm-delete-hint"),
            id="confirm-delete-dialog",
        )

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


class ConfirmForceKillScreen(ModalScreen[bool]):
    """Confirmation before force-killing a session created on a different
    host. Used when the shared-storage host guard would normally block —
    e.g. after a container/machine gets replaced and its sessions never
    got a chance to write a clean ``ended`` status.

    Returns ``True`` on confirm (Enter), ``False`` on cancel (Esc).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    def __init__(
        self, display_name: str, other_host: str, current: str
    ) -> None:
        super().__init__()
        self.display_name = display_name
        self.other_host = other_host
        self.current = current

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                f"Force-kill session [bold]'{escape(self.display_name)}'[/]?",
                id="confirm-force-header",
            ),
            Static(
                f"Session was created on host [bold]{self.other_host}[/] and "
                f"its durable status there is not [italic]ended[/]. "
                f"You're on [bold]{self.current}[/].\n\n"
                "Normal kill is blocked because the session might still be "
                "alive on that other host — killing it from here would only "
                "delete its bookkeeping (inviting a duplicate later) without "
                "actually stopping the process there.\n\n"
                "Confirm only if you're sure that host is gone (container "
                "recycled, machine dead, etc.). This will remove the local "
                "state files here.",
                id="confirm-force-body",
            ),
            Static("Enter to force-kill · Esc to cancel", id="confirm-force-hint"),
            id="confirm-force-dialog",
        )

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


class ResumeSessionScreen(
    ModalScreen[Optional[tuple[str, str, Optional[str]]]]
):
    """Browse runtime sessions in the monitor's cwd and pick one to resume.

    Hides sessions already wrapped by a live or ended multiverse session —
    those are surfaced on the dashboard itself.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, cwd: str, hide_ids: set[tuple[str, str]]) -> None:
        super().__init__()
        self.cwd = cwd
        self.hide_ids = hide_ids
        self.sessions_info: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Resume session — pick one:"),
            Static(f"[dim]cwd: {self.cwd}[/]"),
            DataTable(cursor_type="row", zebra_stripes=True, id="resume-table"),
            Static("Enter on a row to resume · Esc to go back", id="resume-hint"),
            id="resume-dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one("#resume-table", DataTable)
        table.add_columns("Runtime", "ID", "Name", "Preview", "Updated")
        self._populate()
        table.focus()
        if self.sessions_info:
            try:
                table.move_cursor(row=0)
            except Exception:
                pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Stop the event so it doesn't bubble to MonitorApp's own
        # on_data_table_row_selected, which would call action_primary()
        # on the dashboard-focused row and double-attach.
        event.stop()
        key = event.row_key.value if event.row_key is not None else None
        if not key:
            return
        runtime: Optional[str] = None
        session_id: Optional[str] = None
        session_name: Optional[str] = None
        for info in self.sessions_info:
            if info["row_key"] == key:
                runtime = info["runtime"]
                session_id = info["id"]
                session_name = info.get("name")
                break
        if runtime and session_id:
            self.dismiss((runtime, session_id, session_name))

    def _populate(self) -> None:
        all_sessions: list[dict] = []
        for info in list_claude_sessions_in(self.cwd):
            session_id = info["uuid"]
            all_sessions.append(
                {
                    **info,
                    "runtime": RUNTIME_CLAUDE,
                    "id": session_id,
                    "row_key": f"{RUNTIME_CLAUDE}:{session_id}",
                }
            )
        for info in list_codex_sessions_in(self.cwd, include_unnamed=True):
            session_id = info["id"]
            all_sessions.append(
                {
                    **info,
                    "runtime": RUNTIME_CODEX,
                    "row_key": f"{RUNTIME_CODEX}:{session_id}",
                }
            )
        for info in list_pi_sessions_in(self.cwd, include_unnamed=True):
            session_id = info["id"]
            all_sessions.append(
                {
                    **info,
                    "runtime": RUNTIME_PI,
                    "row_key": f"{RUNTIME_PI}:{session_id}",
                }
            )
        all_sessions.sort(key=lambda info: info["mtime"], reverse=True)
        self.sessions_info = [
            info
            for info in all_sessions
            if (info["runtime"], info["id"]) not in self.hide_ids
        ]
        table = self.query_one("#resume-table", DataTable)
        table.clear()
        now = time.time()
        for info in self.sessions_info:
            preview = info["preview"] or "(empty)"
            session_name = info.get("name")
            name_cell = session_name if session_name else "[dim](unnamed)[/]"
            table.add_row(
                info["runtime"],
                info["id"][:8],
                name_cell,
                preview,
                _ago(now - info["mtime"]),
                key=info["row_key"],
            )

    def action_cancel(self) -> None:
        self.dismiss(None)


class AskQuestionScreen(ModalScreen[Optional[str]]):
    """Prompt for a temporary question to fan out across all named agents."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, num_targets: int) -> None:
        super().__init__()
        self.num_targets = num_targets

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"Ask all {self.num_targets} named agent(s) a temporary question."),
            Static(
                "Responses are one-shot and not remembered by the agents.",
                id="ask-blurb",
            ),
            Static(
                "[yellow]⚠ Heads up:[/] this fans out one forked API call per "
                f"agent ({self.num_targets} call(s)) with full context each — "
                "can burn a lot of tokens. Use sparingly.",
                id="ask-warning",
            ),
            Input(placeholder="Your question...", id="ask-input"),
            Static("Enter to ask · Esc to go back", id="ask-hint"),
            id="ask-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#ask-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        q = event.value.strip()
        if q:
            self.dismiss(q)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AskResultsScreen(ModalScreen[None]):
    """Scrollable display of per-agent responses from an ask-all."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, question: str, results: list[AskResult]) -> None:
        super().__init__()
        self.question = question
        self.results = results

    def compose(self) -> ComposeResult:
        blocks: list[Static] = [
            Static(
                f"[bold]Q:[/] {escape(self.question)}",
                id="ask-results-question",
            ),
        ]
        if not self.results:
            blocks.append(Static("[dim](no targets)[/]"))
        for r in self.results:
            header = (
                f"[bold yellow]━━━ {escape(r.name)}[/] "
                f"[dim]({r.elapsed:.1f}s)[/]"
            )
            blocks.append(Static(header))
            if r.error:
                blocks.append(Static(f"[red]error:[/] {escape(r.error)}"))
            elif r.response:
                blocks.append(Static(escape(r.response)))
            else:
                blocks.append(Static("[dim](empty)[/]"))
        blocks.append(Static("Esc to go back", id="ask-results-hint"))
        yield VerticalScroll(*blocks, id="ask-results-dialog")

    def action_close(self) -> None:
        self.dismiss(None)


class InboxViewerScreen(ModalScreen[None]):
    """Read-only view of an agent's pending inbox.

    Triggered by ``i`` from the dashboard. Shows whatever messages are
    currently sitting in ``<multiverse_root>/inbox/<encoded-cwd>/<name>/``
    without consuming them — the agent's hooks (or ``check_inbox`` tool)
    still get to deliver them normally.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("i", "close", "Close"),
    ]

    def __init__(self, agent_name: str, messages: list[dict]) -> None:
        super().__init__()
        self.agent_name = agent_name
        # Snapshot — we don't refresh while open. Caller already filtered
        # for non-empty.
        self.messages = messages

    def compose(self) -> ComposeResult:
        n = len(self.messages)
        plural = "s" if n != 1 else ""
        blocks: list[Static] = [
            Static(
                f"[bold]Inbox for '{escape(self.agent_name)}'[/] — {n} "
                f"pending message{plural}",
                id="inbox-view-header",
            ),
        ]
        for i, m in enumerate(self.messages, 1):
            from_name = m.get("from", "?")
            # from_cwd is present only on cross-workspace messages; show the
            # sender's workspace only in that case (same-workspace messages
            # don't carry it, so nothing extra is rendered).
            from_cwd = m.get("from_cwd")
            body = m.get("body", "")
            ts = m.get("ts", 0)
            ts_str = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                if ts
                else "?"
            )
            origin = f"from {escape(str(from_name))}"
            if from_cwd:
                origin += f" [dim](in workspace {escape(str(from_cwd))})[/]"
            blocks.append(
                Static(
                    f"\n[bold yellow]━━━ [{i}/{n}] {origin}[/]  "
                    f"[dim]({ts_str})[/]"
                )
            )
            # CRITICAL: render body with markup=False. The message body is
            # arbitrary text from another agent (LaTeX, code, etc.) and
            # routinely contains `[...]` sequences that Rich would try to
            # parse as markup tags — `\linewidth]{...}` style content
            # crashes with MarkupError. The body is shown as plain text.
            if body:
                blocks.append(Static(body, markup=False))
            else:
                blocks.append(Static("[dim](empty body)[/]"))
        blocks.append(
            Static(
                "Esc / i / q to close · read-only — messages remain in inbox "
                "and will be delivered to the agent on its next hook fire",
                id="inbox-view-hint",
            )
        )
        yield VerticalScroll(*blocks, id="inbox-view-dialog")

    def action_close(self) -> None:
        self.dismiss(None)


class WebShareScreen(ModalScreen[None]):
    """Start / stop an ephemeral web share for this workspace.

    Pressing ``w`` opens this panel. When off, Enter launches a
    browser-reachable terminal into a *dedicated* monitor for this cwd
    (ttyd on loopback → your configured tunnel + auth), and shows the URL +
    per-share token. When on, ``s`` tears it down. The share runs as
    detached processes, so it survives this monitor quitting — that's the
    point (keep controlling from a laptop after you close the local TUI);
    any later monitor for this cwd shows it's ON and can stop it.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "start", "Start"),
        Binding("q", "start_quick", "Quick"),
        Binding("t", "toggle_token", "Token"),
        Binding("s", "stop", "Stop"),
    ]

    def __init__(self, cwd: str) -> None:
        super().__init__()
        self.cwd = cwd
        self._busy = False
        # Per-share ttyd-token toggle for an edge-authenticated backbone
        # (cloudflare). Seeded from MULTIVERSE_WEB_TOKEN; flip with `t`. Ignored
        # for the quick backbone, which always uses a token.
        self._token_on = web_share.token_default()

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                "[bold]Web share[/] — remote browser control of this workspace",
                id="web-title",
            ),
            Static("", id="web-body"),
            Static("", id="web-hint"),
            id="web-dialog",
        )

    def on_mount(self) -> None:
        self._refresh_panel()

    def _refresh_panel(self) -> None:
        body = self.query_one("#web-body", Static)
        hint = self.query_one("#web-hint", Static)
        share = web_share.status(self.cwd)
        if share is None:
            backbone = web_share.configured_provider_name()
            if backbone != "quick":
                tok = "[green]on[/]" if self._token_on else "[dim]off[/]"
                extra = f"\nttyd token (for [b]{backbone}[/]): {tok}  ([b]t[/] toggles)"
            else:
                extra = ""  # quick always uses a token
            body.update(
                "Status: [dim]off[/]\n\n"
                "Starting exposes a browser terminal into a monitor for "
                "[bold]this workspace only[/], behind your configured tunnel "
                "and auth.\n"
                "[yellow]⚠ This is a remote shell into a full-tool-access "
                "agent — keep it short-lived and stop it when done.[/]\n\n"
                f"Default backbone: [bold]{backbone}[/]" + extra
            )
            if backbone != "quick":
                hint.update(
                    f"[b]Enter[/] start ([b]{backbone}[/]) · [b]t[/] token "
                    "on/off · [b]q[/] quick · [b]Esc[/] cancel"
                )
            else:
                hint.update("[b]Enter[/] start ([b]quick[/]) · [b]Esc[/] cancel")
        else:
            if share.token:
                auth = f"User:  [bold]mv[/]\nToken: [bold]{share.token}[/]"
            else:
                auth = "Auth:  [bold]edge SSO only[/] (no ttyd token)"
            body.update(
                "Status: [bold green]ON[/]\n\n"
                f"URL:   [bold]{share.url}[/]\n"
                f"{auth}\n"
                f"Backbone: {share.provider}\n\n"
                "[dim]Open the URL on an authorized device. Edge auth (SSO / "
                "device, if configured) is enforced at the tunnel before this.[/]"
            )
            hint.update("[b]s[/] stop share · [b]Esc[/] close (share keeps running)")

    def action_start(self) -> None:
        self._do_start(None)  # default/configured backbone

    def action_start_quick(self) -> None:
        self._do_start("quick")  # always-available token-only quick tunnel

    def action_toggle_token(self) -> None:
        # Per-share ttyd-token toggle; only meaningful for an edge-auth backbone
        # (quick always uses a token). No-op while busy or a share is live.
        if self._busy or web_share.status(self.cwd) is not None:
            return
        if web_share.configured_provider_name() == "quick":
            return
        self._token_on = not self._token_on
        self._refresh_panel()

    def _do_start(self, provider: Optional[str]) -> None:
        if self._busy or web_share.status(self.cwd) is not None:
            return
        self._busy = True
        label = provider or web_share.configured_provider_name()
        self.query_one("#web-body", Static).update(
            f"[cyan]Starting share ({label})… bringing up the tunnel (~10–30s).[/]"
        )
        self.query_one("#web-hint", Static).update("[dim]please wait…[/]")
        self._change_worker(start=True, provider=provider, use_token=self._token_on)

    def action_stop(self) -> None:
        if self._busy or web_share.status(self.cwd) is None:
            return
        self._busy = True
        self.query_one("#web-body", Static).update("[cyan]Stopping share…[/]")
        self.query_one("#web-hint", Static).update("[dim]please wait…[/]")
        self._change_worker(start=False)

    @work(thread=True, group="web_share")
    def _change_worker(
        self, start: bool, provider: Optional[str] = None,
        use_token: Optional[bool] = None,
    ) -> None:
        err: Optional[str] = None
        try:
            if start:
                web_share.start(self.cwd, provider=provider, use_token=use_token)
            else:
                web_share.stop(self.cwd)
        except Exception as exc:  # surface tunnel/ttyd failures to the panel
            err = str(exc)
        self.app.call_from_thread(self._after_change, err)

    def _after_change(self, err: Optional[str]) -> None:
        self._busy = False
        if err:
            self.query_one("#web-body", Static).update(f"[red]Failed:[/] {err}")
            self.query_one("#web-hint", Static).update("[b]Esc[/] close")
            return
        self._refresh_panel()

    def action_close(self) -> None:
        if self._busy:
            return
        self.dismiss(None)


class SecondBrainScreen(ModalScreen[None]):
    """Enroll / connect / disconnect this device to the SecondBrain apprentice (the ``S`` key).

    SecondBrain (SB) is a separate, locally-hosted apprentice that watches the user across
    all their Multiverse workspaces and — within earned trust — acts on their behalf. This
    device streams perception events to SB over one authenticated WebSocket, dialing *out*
    (SB is behind NAT). The connector is device-level and host-aware singleton — it survives
    this monitor quitting, so SB keeps perceiving.

    One form serves both flows. Enrollment fields (pairing code + fingerprint) are needed
    only the first time (or to re-enroll / re-grant); a plain reconnect ignores them.

      Enter  — enroll+connect if this device isn't enrolled, else connect
      e      — (re-)enroll using the form (needs pairing code + fingerprint)
      d      — disconnect (stop the connector daemon)
      Esc    — close (the daemon keeps running)
    """

    #: Open with nothing focused. Textual's default ("" inherits the app's "*") would
    #: grab the first focusable widget — the endpoint URL box — which puts a cursor in
    #: a field the user usually doesn't want to edit and swallows the single-key
    #: bindings below until they escape out of it. Empty string disables auto-focus
    #: outright (``None`` would inherit, not disable); the user clicks into a field
    #: when they actually want one.
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "primary", "Enroll/Connect (this workspace)"),
        Binding("e", "reenroll", "Re-enroll"),
        Binding("d", "disconnect", "Disconnect (this workspace)"),
        Binding("D", "stop_device", "Stop device daemon"),
        Binding("u", "unenroll", "Un-enroll this workspace"),
        Binding("r", "reload", "Reload daemon"),
    ]

    def __init__(self, cwd: str) -> None:
        super().__init__()
        self.cwd = cwd
        self._busy = False

    def compose(self) -> ComposeResult:
        import socket as _socket

        st = sb_control.status()
        yield Vertical(
            Static("[bold]SecondBrain[/] — connect this device to your apprentice", id="sb-title"),
            Static("", id="sb-status"),
            VerticalScroll(
                Static("[dim]Endpoint (SB base URL — wss:// through the tunnel, or "
                       "ws://127.0.0.1:8765 same-box):[/]"),
                Input(value=st.get("base_url", ""), id="sb-url"),
                Static("[dim]— Enrollment only (first time / re-grant) —[/]"),
                Static("[dim]Pairing code (from `sb-pair` on the SB host):[/]"),
                Input(placeholder="XXXX-XXXX-XXXX-XXXX", id="sb-code"),
                Static("[dim]SB server fingerprint (pin, from `sb-pair`):[/]"),
                Input(placeholder="XXXX-XXXX-XXXX-XXXX", id="sb-fingerprint"),
                Static("[dim]Device name:[/]"),
                Input(value=(sb_control.get_device_name() or _socket.gethostname()), id="sb-name"),
                Static("[dim]Workspaces to expose (comma-separated abs paths):[/]"),
                # Prefill with the union of what's already granted + the current cwd.
                # Otherwise a re-enroll from a not-yet-granted workspace would send
                # want=[current_cwd] only — SB might respond by REPLACING the grants
                # with just that one, silently dropping previously-granted workspaces
                # like droid_pipeline. Users can still edit to remove any they don't
                # want; but the default is additive, not destructive.
                Input(
                    value=", ".join(sorted(set(
                        list(st.get("granted_workspaces") or []) + [self.cwd]
                    ))),
                    id="sb-workspaces",
                ),
                Static("[dim]— Cloudflare Access service token (wss:// only; blank = keep/none) —[/]"),
                # Deliberately NOT prefilling client_id from storage: our validation
                # is both-or-neither, so a prefilled id + empty secret would force
                # the user to hand-delete the id every time they open the widget
                # just to keep existing creds. Leave both blank to keep; fill both
                # to rotate.
                Input(placeholder="CF-Access-Client-Id (leave blank to keep stored)",
                      id="sb-cf-id"),
                Input(placeholder="CF-Access-Client-Secret (leave blank to keep stored)",
                      password=True, id="sb-cf-secret"),
                id="sb-form",
            ),
            Static("", id="sb-hint"),
            id="sb-dialog",
        )

    def on_mount(self) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        st = sb_control.status()
        state = st.get("state", "stopped")
        colors = {
            "connected": "green", "connecting": "cyan", "reconnecting": "yellow",
            "locked": "yellow", "stopped": "dim", "unenrolled": "red", "error": "red",
        }
        color = colors.get(state, "white")
        lines = [f"Status: [{color}]{state}[/]"]
        if st.get("detail"):
            lines[0] += f"  [dim]({st['detail']})[/]"
        if st.get("enrolled"):
            granted = st.get("granted_workspaces", [])
            exposed = st.get("exposed_workspaces", [])
            # This workspace's state — the primary thing the user needs to see.
            if self.cwd in exposed:
                here = "[green]✓ granted + observed[/]"
            elif self.cwd in granted:
                here = "[yellow]granted but not observed[/] — press [b]Enter[/] to observe"
            else:
                here = "[red]not enrolled[/] — press [b]e[/] with pairing code + fingerprint"
            lines.append(f"This workspace: {here}")
            lines.append(
                f"Device: [bold]{st.get('device_id', '?')}[/] · "
                f"{len(granted)} granted · {len(exposed)} observed"
            )
            lines.append(
                f"CF token: {'[green]stored[/]' if st.get('has_cf_creds') else '[dim]none[/]'}"
            )
            if st.get("code_stale"):
                lines.append(
                    "[yellow]⚠ On-disk code is newer than the running daemon "
                    "— press [b]r[/b] to reload.[/]"
                )
        else:
            lines.append("[dim]Not enrolled yet — fill the form and press Enter to enroll.[/]")
        self.query_one("#sb-status", Static).update("\n".join(lines))

        running = st.get("running")
        granted = st.get("granted_workspaces", [])
        exposed = st.get("exposed_workspaces", [])
        this_exposed = self.cwd in exposed
        this_granted = self.cwd in granted
        if not st.get("enrolled"):
            hint = "[b]Enter[/] enroll & observe this workspace · [b]Esc[/] cancel"
        elif this_exposed:
            hint = (
                "[b]d[/] disconnect this workspace · [b]u[/] un-enroll this workspace · "
                "[b]e[/] re-enroll · [b]D[/] (shift+d) stop entire daemon · "
                "[b]r[/] reload · [b]Esc[/] close (daemon keeps running)"
            )
        elif this_granted:
            hint = (
                "[b]Enter[/] observe this workspace · [b]u[/] un-enroll · "
                "[b]e[/] re-enroll · [b]r[/] reload · [b]Esc[/] close"
            )
        else:
            hint = "[b]e[/] enroll this workspace · [b]Esc[/] close"
        # If daemon isn't running for something, tighten the note
        if not running and this_exposed:
            hint = "[b]Enter[/] start daemon · " + hint
        self.query_one("#sb-hint", Static).update(hint)

    # ---- field helpers -------------------------------------------------------------

    def _val(self, wid: str) -> str:
        return self.query_one(f"#sb-{wid}", Input).value.strip()

    def _cf_from_fields(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (client_id, client_secret, error). Both-or-neither; blank => keep existing."""
        cid, csecret = self._val("cf-id"), self.query_one("#sb-cf-secret", Input).value
        if not cid and not csecret:
            return None, None, None
        if cid and csecret:
            return cid, csecret, None
        return None, None, "Enter BOTH CF Access fields, or leave both blank."

    # ---- actions -------------------------------------------------------------------

    def action_primary(self) -> None:
        if self._busy:
            return
        st = sb_control.status()
        exposed = st.get("exposed_workspaces", [])
        granted = st.get("granted_workspaces", [])
        running = st.get("running")
        if not st.get("enrolled"):
            self._start_worker("enroll")
            return
        if self.cwd in exposed:
            if running:
                self.notify("This workspace is already observed — press d to disconnect it.")
                return
            # Exposed but daemon died — start it back up. This is the common case after
            # a daemon crash / container restart / etc: state.json still lists us as
            # exposed, but no live daemon is emitting events for us.
            self._start_worker("expose")
            return
        if self.cwd in granted:
            # Granted but not exposed → expose (per-workspace connect)
            self._start_worker("expose")
        else:
            # Not granted → needs enroll to add this workspace
            self._start_worker("enroll")

    def action_reenroll(self) -> None:
        if not self._busy:
            self._start_worker("enroll")

    def action_disconnect(self) -> None:
        """Per-workspace: unexpose the current cwd. Daemon keeps running for other
        exposed workspaces; stops entirely only if this was the last one."""
        if not self._busy:
            self._start_worker("unexpose")

    def action_stop_device(self) -> None:
        """Device-level: stop the whole daemon regardless of exposed set."""
        if not self._busy:
            self._start_worker("stop_device")

    def action_unenroll(self) -> None:
        """Remove the current cwd from the sidecar's granted_workspaces + exposed set."""
        if not self._busy:
            self._start_worker("unenroll")

    def action_reload(self) -> None:
        if not self._busy:
            self._start_worker("reload")

    def _start_worker(self, op: str) -> None:
        url = self._val("url")
        # URL only required for ops that dial out (enroll dial; start reads config)
        if op in ("enroll",) and not url:
            self._set_error("Endpoint URL is required.")
            return
        cid, csecret, cf_err = self._cf_from_fields()
        if cf_err:
            self._set_error(cf_err)
            return
        code = fingerprint = ""
        name = self._val("name") or ""
        workspaces: list[str] = []
        if op == "enroll":
            code, fingerprint = self._val("code"), self._val("fingerprint")
            if not code or not fingerprint:
                self._set_error("Pairing code and fingerprint are required to enroll.")
                return
            workspaces = [w.strip() for w in self._val("workspaces").replace(",", " ").split()]
        self._busy = True
        self.query_one("#sb-hint", Static).update("[cyan]working… (this can take a few seconds)[/]")
        self._run(op, url, code, fingerprint, name, workspaces, cid, csecret)

    @work(thread=True, group="secondbrain")
    def _run(
        self, op: str, url: str, code: str, fingerprint: str, name: str,
        workspaces: list, cid: Optional[str], csecret: Optional[str],
    ) -> None:
        err: Optional[str] = None
        try:
            if cid and csecret:
                sb_control.save_cf_creds(cid, csecret)
            if op == "enroll":
                # Additive on the client — sidecar merges with prior grants.
                # After enroll, expose the CURRENT workspace so it starts being
                # observed (without touching other workspaces' exposure state).
                sb_control.enroll(
                    base_url=url, code=code, fingerprint=fingerprint,
                    name=name, want=workspaces,
                )
                sb_control.expose_workspace(self.cwd)
            elif op == "expose":
                if name:
                    sb_control.save_device_name(name)
                sb_control.expose_workspace(self.cwd)
            elif op == "unexpose":
                sb_control.unexpose_workspace(self.cwd)
            elif op == "unenroll":
                sb_control.unenroll_workspace(self.cwd)
            elif op == "stop_device":
                sb_control.stop()
            elif op == "reload":
                if name:
                    sb_control.save_device_name(name)
                sb_control.reload()
        except Exception as exc:  # surface failures to the panel
            err = str(exc)
        self.app.call_from_thread(self._after, err)

    def _after(self, err: Optional[str]) -> None:
        self._busy = False
        if err:
            self._set_error(err)
            return
        self._refresh_status()

    def _set_error(self, msg: str) -> None:
        self.query_one("#sb-hint", Static).update(f"[red]{msg}[/]")

    def action_close(self) -> None:
        if not self._busy:
            self.dismiss(None)


_SPARK_LEVELS = "▁▂▃▄▅▆▇█"


def _stub_summary(tree: dict) -> bool:
    """True if this cached tree is a constructor 'aborted' stub — the
    root wraps the un-merged leaves as-is, so root.tokens ≈ leaf sum
    and treating it as a real summary would inflate the session-wide
    'min' estimate.

    Exact match against the constructor's literal placeholder, not a
    substring check: a real summary can legitimately mention "aborted;
    uncollapsed" as content without being an unbuilt stub itself."""
    if tree.get("type") != "node":
        return False
    summary = tree.get("summary", "")
    return summary == "<construction aborted; uncollapsed frontier>"


def _sparkline(values: list[int], width: int = 80) -> str:
    """Compact per-turn sparkline. Bins ``values`` into ``width`` buckets
    (mean per bucket), normalizes to 8 Unicode block heights."""
    if not values:
        return ""
    n = len(values)
    if n <= width:
        # One column per turn.
        m = max(values) or 1
        return "".join(_SPARK_LEVELS[min(7, int(7 * v / m))] for v in values)
    # Bin down: average per bucket.
    binned: list[float] = []
    for i in range(width):
        lo = int(i * n / width)
        hi = int((i + 1) * n / width)
        if hi <= lo:
            hi = lo + 1
        chunk = values[lo:hi]
        binned.append(sum(chunk) / len(chunk))
    m = max(binned) or 1.0
    return "".join(_SPARK_LEVELS[min(7, int(7 * v / m))] for v in binned)


class ChooseSchedulerScreen(ModalScreen[Optional[dict]]):
    """Post-summarize picker: scheduler + budget + live histogram.

    Dismissed with the chosen config or ``None`` (Esc — leaves the
    session summarize-only, no live rewrite).

    Config shape returned:
      {"scheduler": "proportional" | "recency_boosted" | "last_k_raw",
       "budget": <int tokens>,
       # optional, present only for the parameterized schedulers:
       "k": <int>,                       # last_k_raw
       "recency_ratio": <float>,         # recency_boosted
       "recency_half_life": <float>,     # recency_boosted
      }
    """

    BINDINGS = [Binding("escape", "cancel", "Summarize-only exit")]

    # Reasonable defaults for scheduler params. Placeholders reflect these.
    _DEFAULT_K = 3
    _DEFAULT_RATIO = 3.0
    _DEFAULT_HALF_LIFE = 10.0

    # Rough token cost of the "[N image(s) omitted…]" marker emitted
    # when a turn's user-prompt images are dropped to fit the budget.
    # Matches what priv_session inserts in turn_to_rows.
    _IMAGE_OMISSION_MARKER_TOKENS = 15

    # allocate_budget's image_budget_fraction (fraction of the post-
    # text-floor remainder allowed for kept images). Duplicated here
    # because the picker reimplements the allocation flow rather than
    # calling allocate_budget with a Forest object.
    _IMAGE_BUDGET_FRACTION = 0.5

    def __init__(
        self,
        claude_uuid: str,
        cwd: str,
        display_name: str,
        forest_stats: list[tuple[str, int, int, int, int, int]],
        default_budget: int,
        full_tokens: int = 0,
        full_turns: int = 0,
        live_tokens: int = 0,
        live_turns: int = 0,
        structural_tokens: int = 0,
    ) -> None:
        super().__init__()
        self.claude_uuid = claude_uuid
        self.cwd = cwd
        self.display_name = display_name
        # forest_stats: [(turn_id, leaves, prompt_text, prompt_image,
        #                 min_tok, alloc_weight), ...] in chain order.
        # leaves = full raw leaf sum (bounds, verbatim last_k tail);
        # alloc_weight = leaves minus evictable tool-result output (the
        # scheduler's proportional weight, so verbose tool output doesn't
        # command an outsized budget slice).
        self.forest_stats = forest_stats
        self.default_budget = max(1, default_budget)
        # Kept for the always-visible stats line above the picker so
        # the user doesn't lose sight of what "full" / "live" mean.
        self.full_tokens = full_tokens
        self.full_turns = full_turns
        self.live_tokens = live_tokens
        self.live_turns = live_turns
        # Structural tokens — system prompt, tool schemas, memory files,
        # skills, agents, MCP server definitions. Derived from the last
        # recorded API call's usage delta (see full_jsonl.structural_tokens).
        # Session-wide fixed cost, non-compactable: rolls into
        # ``input_floor`` alongside user-prompt text and images so the
        # scheduler allocates the OUTPUT budget against a realistic
        # ceiling.
        self.structural_tokens = structural_tokens
        self._scheduler = "proportional"
        self._k = self._DEFAULT_K
        self._ratio = self._DEFAULT_RATIO
        self._half_life = self._DEFAULT_HALF_LIFE
        # Binary image handling: True → include ALL user-prompt images
        # verbatim in the input floor; False → drop every image, emit a
        # short marker in its place. Default True: no surprise data
        # loss on first run.
        self._include_images = True

    def compose(self) -> ComposeResult:
        text_floor = sum(
            text for _, _, text, _, _, _ in self.forest_stats
        )
        image_floor = sum(
            img for _, _, _, img, _, _ in self.forest_stats
        )
        n_img_turns = sum(
            1 for _, _, _, img, _, _ in self.forest_stats if img > 0
        )
        marker_floor = n_img_turns * self._IMAGE_OMISSION_MARKER_TOKENS

        live_line = ""
        if self.live_tokens or self.live_turns:
            pct = ""
            if self.full_tokens:
                pct = (
                    f" ({100.0 * self.live_tokens / self.full_tokens:.1f}%"
                    f" of full)"
                )
            live_line = (
                f"\n[dim]Live pre-compaction: {self.live_tokens:,}t · "
                f"{self.live_turns} turn(s){pct}[/]"
            )
        # Structural (system prompt + tool schemas + memory files + skills
        # + agents + MCP definitions) leads the input-floor line since
        # it's typically the dominant section by far and it's the piece
        # the user has no direct control over. Then text (user prompts),
        # then images. Only rendered when > 0 (freshly-compacted sessions
        # can't derive structural until their first post-compact API
        # call — display collapses to just text-and-images in that
        # transient case).
        prompt_line = "\n[dim]Prompts (always kept):"
        parts: list[str] = []
        if self.structural_tokens:
            parts.append(f"system prompt ~{self.structural_tokens:,}t")
        parts.append(f"text {text_floor:,}t")
        if image_floor:
            parts.append(
                f"images {image_floor:,}t across {n_img_turns} "
                f"turn(s) (or ~{marker_floor:,}t if dropped)"
            )
        prompt_line += " " + "  ·  ".join(parts) + "[/]"

        # VerticalScroll, not Vertical: this dialog stacks a multi-line
        # header, the scheduler radio set, an image toggle, three
        # labelled params each with a paragraph of help, the budget
        # input, the per-turn preview and the hint. That comfortably
        # exceeds a terminal's height, and a plain Vertical simply clips
        # the overflow with no way to reach it — the budget input and
        # preview became unreachable on shorter terminals.
        yield VerticalScroll(
            Static(
                f"Compact: [bold]{escape(self.display_name)}[/]\n"
                f"[dim]Full (uncompacted): {self.full_tokens:,}t · "
                f"{self.full_turns} turn(s)[/]"
                f"{live_line}"
                f"{prompt_line}",
                id="choose-title",
            ),
            Static("Scheduler:", id="choose-sched-label"),
            RadioSet(
                RadioButton("proportional", value=True, id="sched-proportional"),
                RadioButton("recency_boosted", id="sched-recency_boosted"),
                RadioButton("last_k_raw", id="sched-last_k_raw"),
                id="choose-scheduler",
            ),

            # Binary image toggle: keep all verbatim or drop all.
            Checkbox(
                "Include user-prompt images verbatim",
                value=True,
                id="choose-include-images",
            ),
            Static(
                "[dim]Images can add significant tokens per turn. "
                "Uncheck to drop ALL user-prompt images across the "
                "session and replace each with a short omission "
                "marker. Assistant-side allocation and the sparkline "
                "reflect only output tokens either way.[/]",
                id="choose-images-desc",
            ),

            # last_k_raw params — hidden unless that scheduler is picked.
            Static("k", id="choose-k-label"),
            Input(
                value=str(self._DEFAULT_K),
                id="choose-k",
                placeholder=f"integer, default {self._DEFAULT_K}",
            ),
            Static(
                "[dim]How many most-recent turns keep their full raw "
                "content (verbatim). Everything before that is "
                "compacted, with the remaining budget split "
                "proportionally by turn size.[/]",
                id="choose-k-desc",
            ),

            # recency_boosted params — hidden unless that scheduler is picked.
            Static("recency_ratio", id="choose-ratio-label"),
            Input(
                value=str(self._DEFAULT_RATIO),
                id="choose-ratio",
                placeholder=f"float, default {self._DEFAULT_RATIO}",
            ),
            Static(
                "[dim]Peak recency boost applied to the newest turn "
                "(vs the pure-proportional baseline). 1.0 = no "
                "boost. 3.0 = newest turn's tokens count up to 3× "
                "as heavily per-token as very old ones; older turns "
                "settle smoothly toward the baseline. In shorter "
                "sessions the effective newest-vs-oldest ratio is "
                "a bit under this value. Big-but-old turns still "
                "outweigh tiny recent ones because proportional "
                "stays as the floor.[/]",
                id="choose-ratio-desc",
            ),

            Static("recency_half_life", id="choose-hl-label"),
            Input(
                value=str(self._DEFAULT_HALF_LIFE),
                id="choose-hl",
                placeholder=f"float, default {self._DEFAULT_HALF_LIFE}",
            ),
            Static(
                "[dim]Turns from newest where the recency boost has "
                "decayed halfway toward pure proportional. Larger "
                "values mean the boost persists further back "
                "through the conversation.[/]",
                id="choose-hl-desc",
            ),

            Static("Budget (tokens):", id="choose-budget-label"),
            Input(
                value=str(self.default_budget),
                id="choose-budget",
                placeholder=f"e.g. {self.default_budget:,} for ~50% of full",
            ),
            Static("", id="choose-preview"),
            Static(
                "[dim]Enter (in Budget) to compact  ·  "
                "Esc to exit without compacting (session stays "
                "summarize-only)[/]",
                id="choose-hint",
            ),
            id="choose-dialog",
        )

    def on_mount(self) -> None:
        self._apply_param_visibility()
        self._refresh_preview()
        try:
            self.query_one("#choose-budget", Input).focus()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- scheduler-driven param visibility -----------------

    # Which param triples belong to each scheduler. Anything not in the
    # current scheduler's set gets its display flag flipped off.
    _PARAM_WIDGETS: dict[str, tuple[str, ...]] = {
        "k": ("#choose-k-label", "#choose-k", "#choose-k-desc"),
        "ratio": ("#choose-ratio-label", "#choose-ratio",
                  "#choose-ratio-desc"),
        "hl": ("#choose-hl-label", "#choose-hl", "#choose-hl-desc"),
    }
    _SCHED_PARAMS: dict[str, set[str]] = {
        "proportional": set(),
        "recency_boosted": {"ratio", "hl"},
        "last_k_raw": {"k"},
    }

    def _apply_param_visibility(self) -> None:
        needed = self._SCHED_PARAMS.get(self._scheduler, set())
        for key, wids in self._PARAM_WIDGETS.items():
            show = key in needed
            for wid in wids:
                try:
                    self.query_one(wid).display = show
                except Exception:  # noqa: BLE001
                    pass

    # ------ live preview -------------------------------------------------

    def _current_budget(self) -> int:
        try:
            raw = self.query_one("#choose-budget", Input).value
            return max(1, int(raw.replace(",", "").replace("_", "").strip()))
        except (ValueError, AttributeError):
            return self.default_budget

    def _current_k(self) -> int:
        try:
            raw = self.query_one("#choose-k", Input).value
            return max(1, int(raw.strip()))
        except (ValueError, AttributeError):
            return self._DEFAULT_K

    def _current_half_life(self) -> float:
        try:
            raw = self.query_one("#choose-hl", Input).value
            v = float(raw.strip())
            return v if v > 0 else self._DEFAULT_HALF_LIFE
        except (ValueError, AttributeError):
            return self._DEFAULT_HALF_LIFE

    def _current_ratio(self) -> float:
        try:
            raw = self.query_one("#choose-ratio", Input).value
            v = float(raw.strip())
            # ratio must be >= 1.0 (1.0 = pure proportional); clamp.
            return max(1.0, v)
        except (ValueError, AttributeError):
            return self._DEFAULT_RATIO

    def _compute_input_floor(self) -> tuple[int, int, int, int]:
        """Return ``(input_floor, text_floor, image_floor, structural_floor)``.

        ``input_floor`` is the total non-compactable input cost:
        user-prompt text (always kept) + user-prompt images (verbatim
        or one omission marker per image-bearing turn, depending on the
        checkbox) + the session's structural tokens (system prompt,
        tools, memory, skills, agents, MCP definitions — never in the
        JSONL but always in every API request). The scheduler operates
        on ``total_budget − input_floor``.
        """
        text_floor = sum(
            text for _, _, text, _, _, _ in self.forest_stats
        )
        if self._include_images:
            image_floor = sum(
                img for _, _, _, img, _, _ in self.forest_stats
            )
        else:
            image_floor = sum(
                self._IMAGE_OMISSION_MARKER_TOKENS
                for _, _, _, img, _, _ in self.forest_stats
                if img > 0
            )
        structural_floor = self.structural_tokens
        return (
            text_floor + image_floor + structural_floor,
            text_floor,
            image_floor,
            structural_floor,
        )

    def _compute_allocation(self) -> list[int]:
        """Per-turn OUTPUT (assistant) allocation from the scheduler,
        in chain order. Budget the scheduler sees is
        ``total_budget − input_floor``.
        """
        turn_ids = [tid for tid, _, _, _, _, _ in self.forest_stats]
        # full_by_id: raw leaf sum (verbatim cost). weight_by_id:
        # allocation weight, evictable tool output excluded. Mirrors
        # compactor.py — proportional/recency split by weight; last_k_raw
        # keeps its verbatim tail at full cost but splits the rest by
        # weight.
        full_by_id = {
            tid: leaves for tid, leaves, _, _, _, _ in self.forest_stats
        }
        weight_by_id = {
            tid: alloc for tid, _, _, _, _, alloc in self.forest_stats
        }
        total_budget = self._current_budget()
        input_floor, _, _, _ = self._compute_input_floor()
        budget = max(0, total_budget - input_floor)

        if self._scheduler == "proportional":
            total = sum(max(1, s) for s in weight_by_id.values()) or 1
            alloc = {
                tid: int(budget * max(1, weight_by_id[tid]) / total)
                for tid in turn_ids
            }
        elif self._scheduler == "recency_boosted":
            ratio = self._current_ratio()
            hl = max(1e-6, self._current_half_life())
            weights = {}
            for k, tid in enumerate(reversed(turn_ids)):
                multiplier = 1.0 + (ratio - 1.0) / (1.0 + k / hl)
                weights[tid] = max(1, weight_by_id[tid]) * multiplier
            total_w = sum(weights.values()) or 1.0
            alloc = {
                tid: int(budget * weights[tid] / total_w) for tid in turn_ids
            }
        elif self._scheduler == "last_k_raw":
            # Clamp k at the turn count. When k == n, the whole forest
            # is "verbatim tail": each turn gets its full leaves count as
            # its allocation, bounded and budget-invariant. Without
            # this clamp, k > n fell through to plain proportional-of-
            # budget which grew unboundedly at high budgets and made
            # one turn appear to eat everything.
            k = min(max(1, self._current_k()), len(turn_ids))
            last_ids = turn_ids[-k:]
            rest_ids = turn_ids[:-k]  # empty when k == n
            raw_cost = sum(full_by_id[tid] for tid in last_ids)
            if raw_cost >= budget and rest_ids:
                total = sum(max(1, s) for s in weight_by_id.values()) or 1
                alloc = {
                    tid: int(budget * max(1, weight_by_id[tid]) / total)
                    for tid in turn_ids
                }
            else:
                remaining = max(0, budget - raw_cost)
                alloc = {tid: full_by_id[tid] for tid in last_ids}
                if rest_ids:
                    rest_total = sum(max(1, weight_by_id[tid]) for tid in rest_ids) or 1
                    for tid in rest_ids:
                        alloc[tid] = int(remaining * max(1, weight_by_id[tid]) / rest_total)
        else:
            alloc = {tid: 0 for tid in turn_ids}

        return [alloc.get(tid, 0) for tid in turn_ids]

    def _session_min_tokens(self) -> int:
        """Session-wide floor at ``budget = 0`` (no output allowed).

        Composed of:
        - The current input floor (text + images-or-markers per the
          checkbox).
        - Every turn's assistant floor: root.tokens under proportional
          / recency_boosted; leaves under last_k_raw's verbatim tail.

        Note the input floor already reflects the checkbox choice —
        toggling images off drops the min accordingly.
        """
        n = len(self.forest_stats)
        if n == 0:
            return 0
        input_floor, _, _, _ = self._compute_input_floor()
        outputs = [min_tok for _, _, _, _, min_tok, _ in self.forest_stats]
        if self._scheduler == "last_k_raw":
            k_eff = min(self._current_k(), n)
            for i in range(n - k_eff, n):
                _, leaves, _, _, _, _ = self.forest_stats[i]
                outputs[i] = leaves
        return input_floor + sum(outputs)

    def _refresh_preview(self) -> None:
        try:
            preview = self.query_one("#choose-preview", Static)
        except Exception:  # noqa: BLE001
            return

        budget = self._current_budget()
        alloc = self._compute_allocation()  # per-turn OUTPUT allocation
        input_floor, text_floor, image_floor, structural_floor = self._compute_input_floor()

        # Per-turn OUTPUT projection is bounded on both ends:
        # - lower bound: root.tokens (expand_to_budget always keeps
        #   the root)
        # - upper bound: sum of leaves (fully-expanded tree = raw)
        # Without the leaves cap, huge budgets would show alloc
        # values well above any turn's actual capacity, especially
        # for last_k_raw where the rest bucket can over-allocate.
        output_per_turn: list[int] = []
        breakdowns: list[tuple[int, int, int]] = []  # (text, img_out, out)
        for i, a in enumerate(alloc):
            tid, leaves, text, img_tok, min_tok, _ = self.forest_stats[i]
            if img_tok > 0 and not self._include_images:
                img_out = self._IMAGE_OMISSION_MARKER_TOKENS
            elif img_tok > 0:
                img_out = img_tok
            else:
                img_out = 0
            out_val = min(max(a, min_tok), leaves)
            output_per_turn.append(out_val)
            breakdowns.append((text, img_out, out_val))

        total_output = sum(output_per_turn)
        total_projected = input_floor + total_output
        session_min = self._session_min_tokens()

        if budget < session_min:
            budget_note = (
                f"  [yellow](< min viable — output pinned at "
                f"~{session_min:,}t)[/]"
            )
        elif total_projected > budget:
            budget_note = (
                f"  [yellow](root-floor rescue pushed projected above "
                f"budget)[/]"
            )
        else:
            budget_note = ""

        # For the "saved" number we want the DELTA between what
        # verbatim images would cost and what we're spending on
        # markers — not the marker cost itself.
        full_image_tokens = sum(
            img for _, _, _, img, _, _ in self.forest_stats
        )
        image_note = ""
        if full_image_tokens > 0 and self._include_images:
            image_note = (
                f"[dim]Images: keeping all image-bearing turns "
                f"verbatim ({image_floor:,}t total).[/]\n"
            )
        elif full_image_tokens > 0 and not self._include_images:
            saved = full_image_tokens - image_floor
            image_note = (
                f"[dim]Images: dropped — replaced with omission "
                f"markers ({image_floor:,}t markers vs "
                f"{full_image_tokens:,}t verbatim; ~{saved:,}t saved).[/]\n"
            )

        # Identify the dominant OUTPUT turn — output is what the
        # scheduler controls, so pointing here tells the user which
        # turn to investigate in the tree visualizer.
        if output_per_turn:
            i_big = max(
                range(len(output_per_turn)),
                key=lambda i: output_per_turn[i],
            )
            t, im, out_val = breakdowns[i_big]
            total_at_big = t + im + out_val
            big_line = (
                f"[bold]Largest OUTPUT turn:[/] #{i_big} — "
                f"{out_val:,}t assistant  "
                f"[dim](whole turn {total_at_big:,}t = text {t:,} + "
                f"image {im:,} + output {out_val:,})[/]\n"
            )
        else:
            big_line = ""

        spark = _sparkline(output_per_turn, width=80)
        preview.update(
            f"[dim]Per-turn OUTPUT allocation (one column per turn "
            f"/ bucket; assistant only, no prompts):[/]\n"
            f"[cyan]{spark}[/]\n"
            f"{image_note}"
            f"[bold]Budget entered:[/] {budget:,}t{budget_note}\n"
            f"[bold]Input floor:[/] {input_floor:,}t  "
            f"[dim](text {text_floor:,} + "
            f"{'images ' if self._include_images else 'markers '}"
            f"{image_floor:,})[/]\n"
            f"[bold]Output budget:[/] {max(0, budget - input_floor):,}t  "
            f"[dim](what the scheduler distributes)[/]\n"
            f"[bold]Min viable output:[/] {session_min:,}t  "
            f"[dim](session floor at current settings)[/]\n"
            f"[bold]Total projected output:[/] {total_projected:,}t\n"
            f"{big_line}"
        )

    # Explicit RadioSet handler.
    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        pressed_id = event.pressed.id if event.pressed else ""
        if pressed_id.startswith("sched-"):
            self._scheduler = pressed_id[len("sched-"):]
        # Hide/show the param inputs for the new scheduler, then
        # recompute the preview under the new params.
        self._apply_param_visibility()
        self._refresh_preview()

    @on(Checkbox.Changed, "#choose-include-images")
    def _include_images_changed(self, event: Checkbox.Changed) -> None:
        self._include_images = bool(event.value)
        self._refresh_preview()

    # Explicit per-Input handlers via @on decorator. The generic
    # `on_input_changed` on the Screen didn't reliably fire on every
    # keystroke — @on wires the message directly to the method with an
    # ID selector, which is what Textual documents for multi-input
    # forms.
    @on(Input.Changed, "#choose-budget")
    def _budget_changed(self, event: Input.Changed) -> None:
        self._refresh_preview()

    @on(Input.Changed, "#choose-k")
    def _k_changed(self, event: Input.Changed) -> None:
        self._refresh_preview()

    @on(Input.Changed, "#choose-ratio")
    def _ratio_changed(self, event: Input.Changed) -> None:
        self._refresh_preview()

    @on(Input.Changed, "#choose-hl")
    def _hl_changed(self, event: Input.Changed) -> None:
        self._refresh_preview()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the budget field commits.
        if event.input.id != "choose-budget":
            return
        try:
            budget = int(event.value.replace(",", "").replace("_", "").strip())
        except ValueError:
            self.notify("Enter an integer budget", severity="warning")
            return
        if budget <= 0:
            self.notify("Budget must be positive", severity="warning")
            return
        config: dict = {
            "scheduler": self._scheduler,
            "budget": budget,
            "include_images": self._include_images,
        }
        if self._scheduler == "last_k_raw":
            config["k"] = self._current_k()
        elif self._scheduler == "recency_boosted":
            config["recency_ratio"] = self._current_ratio()
            config["recency_half_life"] = self._current_half_life()
        self.dismiss(config)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _compact_screen_name(claude_uuid: str) -> str:
    """Textual installed-screen name for a session's CompactScreen.

    Keyed by claude_uuid so a detached compaction can be found again from
    the dashboard row. See ``CompactScreen.action_detach``.
    """
    return f"compact-{claude_uuid}"


class CompactScreen(ModalScreen[None]):
    """Drive a manual compaction for one session.

    Two phases:
      1. ``input`` — fetch total tokens / turn count from full.jsonl in
         the background, show them, prompt the user for a token budget.
      2. ``running`` — kick off the CompactRunner in a thread, display
         its log + an overall trees-built / total-turns progress bar.

    Esc:
      - In ``input``: cancel the modal.
      - In ``running``: signal the runner to abort at the next safe
        checkpoint (won't kill an in-flight build subprocess).
      - In ``done``: dismiss.

    ``d`` detaches instead of aborting: the modal closes back to the
    dashboard while the compaction keeps running and the lock stays held,
    so the row still reads ``compacting`` and still refuses attach /
    resume / kill. Enter or ``c`` on that row re-opens this same screen
    with its log and progress intact — see ``action_detach``.

    The actual *expansion* step (rendering the compacted live JSONL from
    the per-turn budget) is intentionally not yet wired in — first version
    stops once every turn has a tree. The next iteration will plug in
    the proportional scheduler + ``compact_forest_to_session_jsonl`` and
    rewrite the live JSONL.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("d", "detach", "Detach (keep running)"),
    ]

    # Match the worker.log lines that report a single turn's build time:
    #   "[HH:MM:SS]   turn N done (X.Ys)"
    # Used to drive the running ETA estimate.
    _BUILD_TIME_RE = re.compile(r"\bturn\s+\d+\s+done\s+\(([0-9.]+)s\)")

    def __init__(
        self,
        claude_uuid: str,
        cwd: str,
        display_name: str,
        mv_session: Optional[Session],
    ) -> None:
        super().__init__()
        self.claude_uuid = claude_uuid
        self.cwd = cwd
        self.display_name = display_name
        self.mv_session = mv_session
        self.total_tokens = 0
        self.total_turns = 0
        # Per-turn (turn_id, full_tokens) pairs — populated during
        # stats load, used to seed the scheduler picker's histogram.
        self._forest_sizes: list[tuple[str, int]] = []
        self.budget = 0
        self._scheduler_config: Optional[dict] = None
        # Phases: input → summarizing → (Choose picker) → expanding → done.
        # "input" no longer takes a budget — it just displays stats and
        # prompts to start summarization.
        self.phase = "input"  # input | summarizing | choosing | expanding | done | failed
        self._runner: Optional[CompactRunner] = None
        # Detach state (``d``). ``_installed`` tracks whether this screen is
        # registered with Textual under _compact_screen_name so it survives
        # being popped; ``_pending_picker`` defers the scheduler picker when
        # summarization finishes while the user is back on the dashboard.
        self._detached = False
        self._installed = False
        # Whether this screen holds the compacting lock. Taken once when
        # summarization starts and held for the WHOLE flow — including the
        # scheduler/budget picker, which is a human-speed pause the session
        # must stay `compacting` across — then dropped exactly once at
        # teardown. See _acquire_compact_lock.
        self._holds_lock = False
        self._pending_picker = False
        # Per-turn build elapsed times collected from worker.log; powers
        # the ETA estimate. Bounded — only the most-recent N are used so
        # ETA tracks current rate, not a stale long-tail.
        self._build_times: list[float] = []
        self._last_built: int = 0
        self._last_total: int = 0

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                f"Compact session: [bold]{escape(self.display_name)}[/]",
                id="compact-title",
            ),
            Static(
                f"[dim]uuid {self.claude_uuid[:8]} · cwd {self.cwd}[/]",
                id="compact-meta",
            ),
            Static(
                "[dim]Loading uncompacted session stats from full.jsonl…[/]",
                id="compact-stats",
            ),
            # No longer collects a budget — kept as a hidden zero-height
            # placeholder so downstream widget queries (that used to
            # reference it) don't crash. Budget is entered on the
            # ChooseSchedulerScreen after summarization.
            Input(
                placeholder="(unused)",
                id="compact-budget",
                disabled=True,
            ),
            Static("", id="compact-state"),
            ProgressBar(total=100, id="compact-progress", show_eta=False),
            Static("", id="compact-cached"),
            RichLog(
                id="compact-log",
                wrap=False,
                highlight=False,
                markup=False,
                max_lines=2000,
            ),
            Static(
                "[dim]Enter to start summarizing · Esc to cancel[/]",
                id="compact-hint",
            ),
            id="compact-dialog",
        )

    def on_mount(self) -> None:
        # Hide the running-phase widgets until the user submits a budget,
        # but keep `compact-cached` visible — the audit (cached/total +
        # ETA) is useful BEFORE the user picks a budget so they can see
        # how much work is already done and how long the rest will take.
        for wid in ("compact-state", "compact-progress", "compact-log"):
            self.query_one(f"#{wid}").display = False
        # Initial placeholder while the chain-audit thread runs.
        self.query_one("#compact-cached", Static).update(
            "[dim]Auditing cached trees…[/]"
        )
        self._load_stats()

    # ----- Phase 1: stats loading + budget input ---------------------------

    @work(thread=True, exclusive=True, group="compact_stats")
    def _load_stats(self) -> None:
        """Compute full + live token / turn counts, cached-tree count
        (trees whose turn_id is on the chain forest), the "minimum
        compacted tokens" estimate, per-turn (turn_id, full_tokens)
        for the scheduler picker's histogram, and historical per-turn
        build times from worker.log for ETA seeding."""
        try:
            full_toks = total_token_count(self.claude_uuid, self.cwd)
            full_turns = total_turn_count(self.claude_uuid, self.cwd)
            live_toks, live_turns = live_stats(self.claude_uuid, self.cwd)
            min_toks, min_cached, min_total = minimum_compacted_tokens(
                self.claude_uuid, self.cwd
            )
            cached, history = self._audit_cache_against_chain()
            forest_sizes = self._per_turn_full_tokens()
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._stats_failed, repr(e))
            return
        self.app.call_from_thread(
            self._stats_ready,
            full_toks, full_turns, live_toks, live_turns,
            min_toks, min_cached, min_total,
            cached, history, forest_sizes,
        )

    def _per_turn_full_tokens(self) -> list[tuple[str, int]]:
        """Return ``[(turn_id, leaves_tokens), ...]`` in chain order.
        Matches the scheduler's ``_full_tokens(turn)`` — leaves only,
        no user_prompt (user_prompt is subtracted from budget as a
        floor before the scheduler runs, so it's not part of the
        proportional split)."""
        full = full_jsonl_path(self.claude_uuid)
        if not full.is_file():
            return []
        try:
            from compaction.parser import compaction_forest  # noqa: E402
            # canonical compaction view so the sparkline reflects exactly
            # the turns/leaves the pipeline compacts (command turns
            # stripped, non-compactable leaves filtered, empty turns merged).
            forest = compaction_forest(str(full))
        except Exception:  # noqa: BLE001
            return []
        return [(t.turn_id, sum(l.tokens for l in t.leaves)) for t in forest]

    def _per_turn_forest_stats(
        self,
    ) -> list[tuple[str, int, int, int, int]]:
        """Richer per-turn info for the ChooseSchedulerScreen. Returns
        ``[(turn_id, leaves_tokens, prompt_text_tokens,
              prompt_image_tokens, min_tokens), ...]``:

        - leaves_tokens: sum of assistant-side leaves (what the
          scheduler splits over).
        - prompt_text_tokens: verbatim user-prompt TEXT ONLY. Always
          kept as a floor and never compacted.
        - prompt_image_tokens: verbatim user-prompt IMAGE tokens.
          Droppable (replaced by a short omission marker) when the
          budget can't fit them; kept newest-first otherwise. This
          separation is critical — before the split we were double-
          counting image cost in the floor, inflating min viable for
          image-heavy sessions.
        - min_tokens: root.tokens from the cached tree if real, else
          leaves_tokens as fallback.
        """
        import json as _json
        full = full_jsonl_path(self.claude_uuid)
        if not full.is_file():
            return []
        try:
            from compaction.parser import compaction_forest  # noqa: E402
            from compaction.compactor import (  # noqa: E402
                _alloc_weight,
                _user_prompt_image_tokens,
                _user_prompt_text_tokens,
            )
        except Exception:  # noqa: BLE001
            return []
        try:
            # canonical compaction view — ChooseSchedulerScreen sees
            # exactly the turns/leaves the pipeline in compact_runner.py
            # builds against (command turns stripped, non-compactable
            # leaves filtered, empty turns merged).
            forest = compaction_forest(str(full))
        except Exception:  # noqa: BLE001
            return []
        cache_root = multiverse_dir() / "trees" / self.claude_uuid
        out: list[tuple[str, int, int, int, int, int]] = []
        for t in forest:
            leaves = sum(l.tokens for l in t.leaves)
            # Allocation weight excludes evictable tool-result output so
            # a turn full of long file reads / command output doesn't
            # command an outsized budget slice (mirrors the scheduler in
            # compactor.py). Kept as a 6th field; ``leaves`` (full) still
            # drives display bounds and the verbatim last_k_raw tail.
            alloc = _alloc_weight(t)
            prompt_text = _user_prompt_text_tokens(t)
            prompt_image = _user_prompt_image_tokens(t)
            min_tok = leaves  # fallback: whole raw leaf sum
            tree_path = cache_root / f"{t.turn_id}.json"
            if tree_path.is_file():
                try:
                    d = _json.loads(tree_path.read_text())
                    tree = d.get("tree")
                    if (isinstance(tree, dict)
                            and not _stub_summary(tree)):
                        tok = tree.get("tokens")
                        if isinstance(tok, int):
                            min_tok = tok
                except (OSError, _json.JSONDecodeError):
                    pass
            out.append(
                (t.turn_id, leaves, prompt_text, prompt_image, min_tok, alloc)
            )
        return out

    def _audit_cache_against_chain(self) -> tuple[int, list[float]]:
        """Return (n_trees_matching_chain, recent_build_times)."""
        import json as _json
        cached = 0
        history: list[float] = []
        full = full_jsonl_path(self.claude_uuid)
        cache_dir = multiverse_dir() / "trees" / self.claude_uuid

        if full.is_file():
            # Lazy import — the vendored package is added to sys.path by
            # full_jsonl on import, so this is cheap once warm.
            from compaction.parser import compaction_forest  # noqa: E402
            try:
                # canonical compaction view — command-turn / empty-turn
                # tree files (harmless orphans) shouldn't count toward the
                # "trees matching chain" figure the compact widget shows.
                forest = compaction_forest(str(full))
                chain_turns = {t.turn_id for t in forest}
            except Exception:  # noqa: BLE001
                chain_turns = set()

            if chain_turns and cache_dir.is_dir():
                for p in cache_dir.glob("*.json"):
                    try:
                        d = _json.loads(p.read_text())
                    except (OSError, _json.JSONDecodeError):
                        continue
                    if d.get("turn_id") in chain_turns:
                        cached += 1

        log_path = cache_dir / "worker.log"
        if log_path.is_file():
            try:
                with log_path.open() as f:
                    for line in f:
                        m = self._BUILD_TIME_RE.search(line)
                        if m:
                            try:
                                history.append(float(m.group(1)))
                            except ValueError:
                                pass
            except OSError:
                pass
        # Cap at the most-recent 20 — same window the running-phase logic
        # uses, so ETA is consistent before/during the run.
        if len(history) > 20:
            history = history[-20:]
        return cached, history

    def _stats_failed(self, err: str) -> None:
        self.query_one("#compact-stats", Static).update(
            f"[red]Failed to load stats: {err}[/]"
        )

    def _stats_ready(
        self,
        total_tokens: int,
        total_turns: int,
        live_tokens: int = 0,
        live_turns: int = 0,
        min_tokens: int = 0,
        min_cached: int = 0,
        min_total: int = 0,
        cached: int = 0,
        history: list[float] | None = None,
        forest_sizes: list[tuple[str, int]] | None = None,
    ) -> None:
        self.total_tokens = total_tokens
        self.total_turns = total_turns
        self._last_built = cached
        self._last_total = total_turns
        if history:
            self._build_times = list(history)
        if forest_sizes is not None:
            self._forest_sizes = forest_sizes
        suggested = max(1, total_tokens // 2)

        # Two-line stats: full (uncompacted) above, current live below.
        # The live line includes a percentage-of-full so the user can
        # see how much compaction has already happened.
        if live_tokens == 0:
            live_suffix = "[dim](no live JSONL on disk)[/]"
        elif total_tokens == 0:
            live_suffix = ""
        elif live_tokens == total_tokens and live_turns == total_turns:
            live_suffix = "[dim](matches full — no compaction yet)[/]"
        else:
            pct = 100.0 * live_tokens / total_tokens
            live_suffix = f"[dim]({pct:.1f}% of full)[/]"

        # Third line: minimum-compacted tokens — what live would cost if
        # every turn collapsed to its Node root plus verbatim user
        # prompt. Only trustworthy when every turn has a real cached
        # tree; note completeness when incomplete.
        if min_total == 0:
            min_line = ""
        elif min_cached >= min_total:
            min_line = (
                f"\nMin:  [bold]{min_tokens:,}[/] tokens  "
                f"[dim](if fully collapsed — all {min_total} trees cached)[/]"
            )
        else:
            missing = min_total - min_cached
            min_line = (
                f"\nMin:  [bold]{min_tokens:,}[/] tokens  "
                f"[dim](over-estimate — {missing} of {min_total} turn(s) "
                f"lack a real cached tree)[/]"
            )

        self.query_one("#compact-stats", Static).update(
            f"Full: [bold]{total_tokens:,}[/] tokens · "
            f"[bold]{total_turns}[/] turn(s)  [dim](uncompacted)[/]\n"
            f"Live: [bold]{live_tokens:,}[/] tokens · "
            f"[bold]{live_turns}[/] turn(s)  {live_suffix}"
            f"{min_line}"
        )
        self._refresh_cached_line()
        # Auto-start summarize as soon as stats are loaded — the two-
        # phase flow has no budget input up-front (the user picks
        # scheduler + budget on ChooseSchedulerScreen after trees
        # finish). This makes "open compact widget" == "start
        # summarizing" which is what the user wants.
        self._enter_summarizing_phase()

    # The budget Input widget is a leftover placeholder — no submit
    # handler needed. Enter to start summarizing is handled by the
    # auto-start in _stats_ready.

    # ----- Phase 2: summarizing --------------------------------------------

    def _enter_summarizing_phase(self) -> None:
        """Kick off tree-build for every completed turn. No expansion —
        after this finishes we push the scheduler picker."""
        self.phase = "summarizing"

        budget = self.query_one("#compact-budget", Input)
        budget.disabled = True
        budget.display = False

        stats = self.query_one("#compact-stats", Static)
        stats.update(
            f"Total: [bold]{self.total_tokens:,}[/]t  ·  "
            f"[bold]{self.total_turns}[/] turn(s)  "
            f"[dim](summarizing — scheduler picker comes next)[/]"
        )

        for wid in ("compact-state", "compact-progress",
                    "compact-cached", "compact-log"):
            self.query_one(f"#{wid}").display = True

        self.query_one("#compact-state", Static).update(
            "[cyan]Starting summarization…[/]"
        )
        progress = self.query_one("#compact-progress", ProgressBar)
        progress.update(total=max(1, self.total_turns), progress=0)
        log_widget = self.query_one("#compact-log", RichLog)
        log_widget.clear()
        self.query_one("#compact-hint", Static).update(
            "[dim]Esc to abort (won't kill in-flight build subprocess)  ·  "
            "d to detach (keeps running)[/]"
        )

        # Ensure forest_sizes is populated even if the initial stats
        # load raced or missed something (e.g. some turns just built).
        if not self._forest_sizes:
            self._forest_sizes = self._per_turn_full_tokens()

        self._start_summarize_runner()

    def _acquire_compact_lock(self) -> bool:
        """Take the compacting lock for the whole compaction.

        Previously each CompactRunner took and released it around its own
        run, so the session stopped being `compacting` between summarize
        and expand — exactly the window where the picker waits on a human.
        Invisible while a modal covered the dashboard; wrong as soon as the
        user detached with `d`, since the row dropped to dormant and Enter
        on it offered to resume the session rather than reopen the picker.
        """
        if self._holds_lock:
            return True
        if not acquire_lock(self.claude_uuid):
            return False
        self._holds_lock = True
        return True

    def _release_compact_lock(self) -> None:
        """Drop the lock, once. Guarded by ``_holds_lock`` so a screen that
        never acquired it (cancelled at the input phase) can't unlink a lock
        belonging to another monitor."""
        if not self._holds_lock:
            return
        self._holds_lock = False
        release_lock(self.claude_uuid)

    def _start_summarize_runner(self) -> None:
        if not self._acquire_compact_lock():
            self.phase = "failed"
            self._append_log(
                "[FAIL] another monitor is already compacting this session."
            )
            self.query_one("#compact-hint", Static).update(
                "[bold red]Already being compacted elsewhere. Esc to close.[/]"
            )
            return
        live = live_jsonl_path(self.claude_uuid, self.cwd)
        if self.mv_session is not None:
            kill_fn = self._make_kill_fn(self.mv_session)
        else:
            kill_fn = None
        self._runner = CompactRunner(
            claude_uuid=self.claude_uuid,
            cwd=self.cwd,
            live_jsonl=live,
            total_budget=0,
            kill_session_fn=kill_fn,
            summarize_only=True,
            own_lock=False,  # this screen holds it for the whole flow
            sync_full_before_build=(
                self.mv_session is None or self.mv_session.status != "retired"
            ),
        )
        self._run_in_thread()

    # ----- Phase 3: expanding (after ChooseSchedulerScreen) ---------------

    def _enter_expanding_phase(self, config: dict) -> None:
        """Called on the UI thread after the picker screen returns a
        config. Starts the expansion runner (skips kill/sync/build)."""
        self.phase = "expanding"
        self._scheduler_config = config
        self.budget = int(config.get("budget", 0))

        stats = self.query_one("#compact-stats", Static)
        stats.update(
            f"Total: [bold]{self.total_tokens:,}[/]t  →  "
            f"Budget: [bold]{self.budget:,}[/]t  ·  "
            f"[dim]scheduler: {config['scheduler']}[/]"
        )
        self.query_one("#compact-state", Static).update(
            "[cyan]Expanding + rewriting live…[/]"
        )
        # Reset progress: expansion writes rows, not builds turns; we
        # don't have a meaningful per-turn bar here, but keep it visible.
        progress = self.query_one("#compact-progress", ProgressBar)
        progress.update(total=1, progress=0)

        self._start_expand_runner(config)

    def _start_expand_runner(self, config: dict) -> None:
        live = live_jsonl_path(self.claude_uuid, self.cwd)
        params: dict = {}
        for pk in ("k", "recency_ratio", "recency_half_life"):
            if pk in config:
                params[pk] = config[pk]
        include_images = bool(config.get("include_images", True))
        image_mode = "keep_all" if include_images else "drop_all"
        self._runner = CompactRunner(
            claude_uuid=self.claude_uuid,
            cwd=self.cwd,
            live_jsonl=live,
            total_budget=self.budget,
            kill_session_fn=None,  # session already dormant post-summarize
            expand_only=True,
            scheduler_name=config["scheduler"],
            scheduler_params=params,
            image_mode=image_mode,
            own_lock=False,  # this screen holds it for the whole flow
        )
        self._run_in_thread()

    @staticmethod
    def _make_kill_fn(session: Session):
        """Build a callable that kills the multiverse session's tmux pane
        without reaping its state directory.

        We intentionally do NOT call ``sessions.kill_session`` here — it
        also shutil.rmtree's the per-session state dir, which would erase
        the multiverse name and friendly-row identity for the duration
        of the compaction. Killing the tmux session alone is enough: the
        existing dashboard logic detects the missing tmux and downgrades
        status to ``ended`` (which our compacting-lock then overrides to
        ``compacting``).
        """
        tmux_name = session.tmux_name

        def _kill():
            if tmux_session_exists(tmux_name):
                subprocess.run(
                    ["tmux", "kill-session", "-t", tmux_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        return _kill

    @work(thread=True, exclusive=True, group="compact_run")
    def _run_in_thread(self) -> None:
        runner = self._runner
        if runner is None:
            return

        def on_log(line: str) -> None:
            self.app.call_from_thread(self._append_log, line)

        def on_progress(built: int, total: int) -> None:
            self.app.call_from_thread(self._update_progress, built, total)

        def on_state(state: str) -> None:
            self.app.call_from_thread(self._update_state, state)

        def on_done(ok: bool, msg: str) -> None:
            self.app.call_from_thread(self._on_done, ok, msg)

        runner.run(on_log, on_progress, on_state, on_done)

    def _append_log(self, line: str) -> None:
        try:
            log = self.query_one("#compact-log", RichLog)
            # Follow the tail only while the view is already parked at the
            # bottom. RichLog defaults to auto_scroll=True and its write()
            # calls scroll_end() UNCONDITIONALLY — not "only if pinned" — so
            # every streamed line yanked the user back down mid-scroll,
            # making the build log effectively unscrollable while a build
            # was running. Passing scroll_end explicitly gives the usual
            # tail-follow behaviour: keep following at the bottom, hold
            # position once the user scrolls up.
            log.write(line, scroll_end=log.is_vertical_scroll_end)
        except Exception:  # noqa: BLE001
            pass
        # Pull per-turn build times out of the worker.log lines as they
        # stream in, to drive the ETA estimate. Cap at the most-recent N
        # so ETA tracks current rate, not stale early-build times.
        m = self._BUILD_TIME_RE.search(line)
        if m:
            try:
                self._build_times.append(float(m.group(1)))
                if len(self._build_times) > 20:
                    self._build_times = self._build_times[-20:]
                self._refresh_cached_line()
            except ValueError:
                pass

    def _update_progress(self, built: int, total: int) -> None:
        self._last_built = built
        self._last_total = total
        progress = self.query_one("#compact-progress", ProgressBar)
        progress.update(total=max(1, total), progress=built)
        self.query_one("#compact-state", Static).update(self._state_text())
        self._refresh_cached_line()

    def _refresh_cached_line(self) -> None:
        built = self._last_built
        total = self._last_total
        try:
            label = self.query_one("#compact-cached", Static)
        except Exception:  # noqa: BLE001
            return
        if total <= 0:
            label.update("")
            return
        pct = (built / total * 100.0) if total else 0.0
        remaining = max(0, total - built)
        if remaining == 0:
            eta_str = "all done"
        elif self._build_times:
            avg = sum(self._build_times) / len(self._build_times)
            eta_s = avg * remaining
            eta_str = f"~{_fmt_eta(eta_s)} (avg {avg:.1f}s/turn over last {len(self._build_times)})"
        else:
            eta_str = "—"
        label.update(
            f"[bold]Cached:[/] {built:,} / {total:,} ({pct:.1f}%)  ·  "
            f"[bold]ETA:[/] {eta_str}"
        )

    def _update_state(self, state: str) -> None:
        self._state = state
        self.query_one("#compact-state", Static).update(self._state_text())

    def _state_text(self) -> str:
        s = getattr(self, "_state", "starting")
        styled = {
            CompactState.LOCKING: "[cyan]Acquiring compaction lock…[/]",
            CompactState.KILLING: "[cyan]Killing live session…[/]",
            CompactState.SYNCING: "[cyan]Syncing full.jsonl…[/]",
            CompactState.WAITING_AUTO: (
                "[cyan]Waiting for in-flight auto-worker…[/]"
            ),
            CompactState.BUILDING: "[cyan]Building remaining trees…[/]",
            CompactState.EXPANDING: (
                "[cyan]Expanding trees + writing compacted live JSONL…[/]"
            ),
            CompactState.DONE: "[bold green]✓ Done[/]",
            CompactState.FAILED: "[bold red]✗ Failed[/]",
            CompactState.ABORTED: "[bold yellow]✗ Aborted[/]",
        }.get(s, f"[dim]{s}…[/]")
        return styled

    def _on_done(self, ok: bool, msg: str) -> None:
        self._append_log("─" * 56)
        self._append_log(("[OK] " if ok else "[FAIL] ") + msg)

        # Post-summarize hand-off: push ChooseSchedulerScreen.
        if ok and self.phase == "summarizing":
            self.phase = "choosing"
            self.query_one("#compact-hint", Static).update(
                "[dim]Trees built. Choose scheduler + budget…[/]"
            )
            if self._detached:
                # Don't ambush the dashboard with a modal the user isn't
                # looking at. Hold the picker until they re-attach;
                # on_screen_resume pushes it then.
                self._pending_picker = True
                self.app.notify(
                    f"'{self.display_name}': trees built — press Enter on "
                    "its row to choose the budget.",
                    markup=False,
                )
                return
            self._push_picker()
            return

        # Terminal state — either summarize failed, or expansion done.
        self.phase = "done" if ok else "failed"
        verdict_color = "green" if ok else "red"
        if self.phase == "done":
            verdict = "Compaction done. "
        else:
            verdict = "Compaction failed. "
        self.query_one("#compact-hint", Static).update(
            f"[bold {verdict_color}]{verdict}Esc to close.[/]"
        )
        if self._detached:
            # Finished while the user was on the dashboard. Report the
            # verdict there and drop the registration, so a later `c` on
            # that row starts a fresh compaction instead of re-opening this
            # finished one.
            self.app.notify(
                f"'{self.display_name}': {verdict.strip()}"
                + ("" if ok else f" {msg}"),
                severity="information" if ok else "error",
                markup=False,
            )
            self._teardown_detached()

    def _push_picker(self) -> None:
        """Hand off to ChooseSchedulerScreen.

        Rebuilds the richer per-turn stats now that trees are freshly
        built — root.tokens for each turn is up-to-date. Live stats come
        along so the picker's header keeps the full / live context visible
        instead of dropping it on the way out of the input phase.
        """
        live_toks, live_turns = live_stats(self.claude_uuid, self.cwd)
        struct_toks = structural_tokens(self.claude_uuid, self.cwd)
        picker = ChooseSchedulerScreen(
            claude_uuid=self.claude_uuid,
            cwd=self.cwd,
            display_name=self.display_name,
            forest_stats=self._per_turn_forest_stats(),
            default_budget=max(1, self.total_tokens // 2),
            full_tokens=self.total_tokens,
            full_turns=self.total_turns,
            live_tokens=live_toks,
            live_turns=live_turns,
            structural_tokens=struct_toks,
        )
        self.app.push_screen(picker, self._on_config_chosen)

    def _on_config_chosen(self, config: Optional[dict]) -> None:
        """Callback from ChooseSchedulerScreen. None → user exited to
        summarize-only; dict → run expansion with the picked config."""
        if config is None:
            self.phase = "done"
            self._append_log("[user] exited at scheduler picker — "
                             "summarize-only.")
            self.query_one("#compact-hint", Static).update(
                "[bold green]Trees built. Live untouched. "
                "Esc to close.[/]"
            )
            return
        self._enter_expanding_phase(config)

    # ----- Detach / re-attach ---------------------------------------------

    def action_detach(self) -> None:
        """Close back to the dashboard WITHOUT stopping the compaction.

        Esc aborts the run; this deliberately does not. The runner thread
        keeps going, the compaction lock stays held (so every monitor keeps
        rendering the row as ``compacting`` and keeps refusing attach /
        resume / kill), and the user gets the dashboard back.

        The screen instance is kept alive with ``install_screen``. That is
        load-bearing, not incidental: Textual removes a popped screen's
        widgets, so a plain pop-and-re-push would come back with an empty
        log and a reset progress bar. An installed screen stays mounted,
        keeps accepting the runner's writes while off-stack, and re-pushes
        as the same instance.
        """
        if self.phase not in ("summarizing", "choosing", "expanding"):
            self.notify(
                "Nothing is running — Esc closes this screen.",
                severity="warning",
            )
            return
        app = self.app
        if not self._installed:
            try:
                app.install_screen(self, name=_compact_screen_name(self.claude_uuid))
                self._installed = True
            except Exception:  # noqa: BLE001
                self.notify(
                    "Could not detach — leaving this screen open.",
                    severity="error",
                )
                return
        app._detached_compactions[self.claude_uuid] = self
        self._detached = True
        app.pop_screen()
        app.notify(
            f"'{self.display_name}' is still compacting in the background — "
            "press Enter on its row to come back.",
            markup=False,
        )

    def on_screen_resume(self) -> None:
        """Re-attached (or shown for the first time)."""
        self._detached = False
        if self._pending_picker:
            self._pending_picker = False
            self._push_picker()

    def _teardown_detached(self) -> None:
        """Drop the detach registration once the run has finished while the
        user was away. Safe to uninstall here — a detached screen is by
        definition off the stack, which is the one case Textual refuses."""
        self._release_compact_lock()
        app = self.app
        app._detached_compactions.pop(self.claude_uuid, None)
        if self._installed:
            try:
                app.uninstall_screen(_compact_screen_name(self.claude_uuid))
            except Exception:  # noqa: BLE001
                pass
            self._installed = False
        app.refresh_sessions()

    # ----- Cancel / dismiss -----------------------------------------------

    def action_cancel(self) -> None:
        if (
            self.phase in ("summarizing", "expanding")
            and self._runner is not None
        ):
            # First Esc: signal abort (clean stop at next checkpoint).
            if not self._runner.cancelled:
                self._runner.cancel()
                self._append_log("[user] cancel requested — will abort soon")
                return
            # Second Esc while still running: force-dismiss.
        self._release_compact_lock()
        self.dismiss(None)


class RestoreScreen(ModalScreen[Optional[tuple[str, str]]]):
    """Pick a session to restore from the orphan full JSONLs.

    "Orphans" are full JSONLs whose live JSONL no longer exists on
    disk — usually because claude code's ``cleanupPeriodDays`` reaped
    the live but our mirror persists independently.

    Filtered to orphans whose recorded cwd matches the current
    workspace, sorted newest-first. Titles that overlap an existing
    live session are hidden (the user already has that name active).

    Returns ``(claude_uuid, cwd)`` on Enter; ``None`` on Esc / no
    selection. The caller hands off to CompactScreen, which
    materializes the new live JSONL by expanding the full to a budget.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, cwd: str, live_titles: set[str]) -> None:
        super().__init__()
        self.cwd = cwd
        self.live_titles = live_titles
        # uuid → orphan dict lookup for the DataTable's RowSelected handler.
        self._by_uuid: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Restore lost session", id="restore-title"),
            Static(f"[dim]cwd {self.cwd}[/]", id="restore-meta"),
            Static(
                "[dim]Loading orphan full JSONLs…[/]",
                id="restore-status",
            ),
            DataTable(id="restore-table", cursor_type="row"),
            Static(
                "[dim]Enter to restore, Esc to cancel[/]",
                id="restore-hint",
            ),
            id="restore-dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one("#restore-table", DataTable)
        table.add_columns("Age", "Turns", "Trees", "Title", "Last user message")
        self._load_orphans()

    @work(thread=True)
    def _load_orphans(self) -> None:
        try:
            orphans = list_orphan_fulls(cwd_filter=self.cwd)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._orphans_failed, repr(e))
            return
        # Drop orphans whose title collides with a currently-live
        # session in this workspace — the user already has that name.
        orphans = [o for o in orphans if o.get("title") not in self.live_titles]
        self.app.call_from_thread(self._orphans_ready, orphans)

    def _orphans_failed(self, err: str) -> None:
        self.query_one("#restore-status", Static).update(
            f"[red]Failed to enumerate orphans: {err}[/]"
        )

    def _orphans_ready(self, orphans: list[dict]) -> None:
        status = self.query_one("#restore-status", Static)
        self._by_uuid = {o["uuid"]: o for o in orphans}
        if not orphans:
            status.update(
                "[dim]No orphan full JSONLs for this workspace. "
                "Nothing to restore.[/]"
            )
            return
        status.update(
            f"[dim]{len(orphans)} orphan session(s) — newest first. "
            f"Same-name orphans differentiated by turns + last user "
            f"message.[/]"
        )
        table = self.query_one("#restore-table", DataTable)
        table.clear()
        now = time.time()
        for o in orphans:
            age = _fmt_age(now - o.get("mtime", now))
            turns = str(o.get("turns", 0))
            trees = str(o.get("trees_cached", 0))
            title = o.get("title", "(no title)")
            msg = _summarize_msg(o.get("last_user_msg", ""), max_chars=80)
            table.add_row(age, turns, trees, title, msg, key=o["uuid"])
        table.focus()
        try:
            table.move_cursor(row=0)
        except Exception:  # noqa: BLE001
            pass

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        """Fired by the DataTable when Enter is pressed on a focused row.
        Must call ``event.stop()`` — the event bubbles up otherwise,
        and MonitorApp has its own ``on_data_table_row_selected`` that
        calls ``action_primary()`` on the dashboard-focused row, so
        selecting an orphan to restore would also attach whatever the
        user was hovered on."""
        event.stop()
        key = event.row_key.value if event.row_key is not None else None
        if not key:
            return
        o = self._by_uuid.get(str(key))
        if o is None:
            return
        self.dismiss((o["uuid"], o.get("cwd") or self.cwd))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _fmt_age(seconds: float) -> str:
    """Compact age string: '3d', '4h', '17m', '25s'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _summarize_msg(text: str, max_chars: int = 80) -> str:
    """Flatten a possibly-multi-line message to a single truncated line
    suitable for a table cell."""
    if not text:
        return ""
    single = " ".join(text.split())
    if len(single) <= max_chars:
        return single
    return single[: max_chars - 1] + "…"


def _read_title_from_full(claude_uuid: str) -> Optional[str]:
    """Grab the last ``custom-title`` from a full JSONL (for the
    restore path, where live doesn't exist and we still want a
    meaningful header on the CompactScreen)."""
    import json
    full = full_jsonl_path(claude_uuid)
    if not full.is_file():
        return None
    title: Optional[str] = None
    try:
        with full.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "custom-title":
                    ct = d.get("customTitle")
                    if isinstance(ct, str) and ct:
                        title = ct
    except OSError:
        return None
    return title


@dataclass
class _InlineEdit:
    """A folder name being edited in the row itself.

    The folder is created first and named afterwards, so ``group_id``
    always points at something real — a redraw (or another monitor) can
    happen mid-edit without the row vanishing. ``select_all`` reproduces
    Finder's "the name comes up highlighted": the first printable key
    replaces it rather than appending.
    """

    kind: str  # "create" | "rename"
    group_id: str
    buffer: str
    cursor: int
    select_all: bool
    # For a "create": everything needed to put the workspace back exactly
    # as it was if the user presses Esc. The folder is already on disk by
    # the time the name is being typed, so backing out has to undo the
    # wrap, not just close the editor.
    undo: Optional[groups.Wrap] = None
    prev_selection: list[str] = field(default_factory=list)
    prev_cursor_key: Optional[str] = None


@dataclass
class _DraftAgent:
    """An agent the user is describing but hasn't created yet.

    Unlike a folder, a session can't be made first and named afterwards:
    the name is baked in at spawn (custom title, tmux env, inbox routing,
    name claims). So the row is a placeholder until Enter, and Esc costs
    nothing because nothing was started.
    """

    buffer: str
    cursor: int
    select_all: bool
    runtime: str
    group_id: Optional[str]


@dataclass
class _MoveMode:
    """A move in progress, picking its destination in the tree in place.

    ``dest`` is the folder the cursor has walked *into* (``None`` = top
    level) and is where Enter drops. ``highlight`` is the subfolder of
    ``dest`` that ``→`` would walk into next.
    """

    group_ids: list[str]
    agent_names: list[str]
    label: str
    dest: Optional[str]
    highlight: Optional[str]


class DashboardTable(DataTable):
    """The dashboard's table, with outliner keys.

    ``DataTable`` claims ``left``/``right`` for column movement and
    ``enter`` for row selection, and knows nothing about shift-ranges or
    editing a cell in place. Rebinding here keeps all of that scoped to
    this one widget — the alternative, priority bindings on the App, would
    fire even while an Input in a modal has focus and break typing there.

    ``on_key`` runs before binding resolution, which is what lets the
    inline editor and the move mode swallow the very keys the table would
    otherwise act on.
    """

    BINDINGS = [
        Binding("right", "app.indent", "Into folder", show=False),
        Binding("left", "app.outdent", "Out of folder", show=False),
        Binding("shift+up", "app.extend_selection_up", "Select up", show=False),
        Binding(
            "shift+down", "app.extend_selection_down", "Select down", show=False
        ),
    ]

    def on_key(self, event: events.Key) -> None:
        intercept = getattr(self.app, "_intercept_key", None)
        if intercept is not None and intercept(event):
            event.stop()
            event.prevent_default()


class ConfirmGroupDeleteScreen(ModalScreen[bool]):
    """Confirmation before removing a folder.

    Worth confirming even though nothing is destroyed: on a collapsed
    folder the user can't see how much is about to be tipped back out.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    def __init__(
        self,
        name: str,
        agent_count: int,
        subgroup_count: int,
        parent_name: Optional[str],
    ) -> None:
        super().__init__()
        self.group_name = name
        self.agent_count = agent_count
        self.subgroup_count = subgroup_count
        self.parent_name = parent_name

    def compose(self) -> ComposeResult:
        held = []
        if self.agent_count:
            held.append(
                f"{self.agent_count} agent"
                + ("s" if self.agent_count != 1 else "")
            )
        if self.subgroup_count:
            held.append(
                f"{self.subgroup_count} subfolder"
                + ("s" if self.subgroup_count != 1 else "")
            )
        where = (
            f"into '{escape(self.parent_name)}'"
            if self.parent_name
            else "back to the top level"
        )
        body = (
            f"{' and '.join(held)} move {where}."
            if held
            else "The folder is empty."
        )
        yield Vertical(
            Static(
                f"Remove folder [bold]'{escape(self.group_name)}'[/]?",
                id="confirm-group-header",
            ),
            Static(
                body + " No sessions are killed and no transcripts are "
                "deleted — this only undoes the grouping.",
                id="confirm-group-body",
            ),
            Static("Enter to confirm · Esc to cancel", id="confirm-group-hint"),
            id="confirm-group-dialog",
        )

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


class MonitorApp(App):
    CSS = """
    Screen {
        layers: base modal;
    }
    DataTable {
        height: 1fr;
    }
    #new-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #new-error {
        color: $error;
        height: auto;
        padding-top: 1;
    }
    #new-runtime {
        height: auto;
        padding-bottom: 1;
    }
    #new-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #name-dialog {
        width: 100;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #name-reason {
        padding-bottom: 1;
    }
    #name-error {
        color: $error;
        height: auto;
        padding-top: 1;
    }
    #name-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #confirm-delete-dialog {
        width: 100;
        height: auto;
        padding: 1 2;
        border: round $error;
        background: $surface;
    }
    #confirm-delete-header {
        height: auto;
        padding-bottom: 1;
    }
    #confirm-delete-body {
        color: $text-muted;
        height: auto;
        padding-bottom: 1;
    }
    #confirm-delete-hint {
        color: $text-muted;
        height: auto;
    }
    #confirm-force-dialog {
        width: 100;
        height: auto;
        padding: 1 2;
        border: round $error;
        background: $surface;
    }
    #confirm-force-header {
        height: auto;
        padding-bottom: 1;
    }
    #confirm-force-body {
        color: $text-muted;
        height: auto;
        padding-bottom: 1;
    }
    #confirm-force-hint {
        color: $text-muted;
        height: auto;
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
        height: auto;
    }
    #ask-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #ask-blurb {
        color: $text-muted;
        padding-bottom: 1;
    }
    #ask-warning {
        padding-bottom: 1;
        height: auto;
    }
    #ask-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #ask-results-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #ask-results-question {
        padding-bottom: 1;
    }
    #ask-results-hint {
        color: $text-muted;
        padding-top: 1;
    }
    #inbox-view-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #inbox-view-header {
        padding-bottom: 1;
    }
    #inbox-view-hint {
        color: $text-muted;
        padding-top: 1;
    }
    #web-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #web-title {
        height: auto;
        padding-bottom: 1;
    }
    #web-body {
        height: 1fr;
    }
    #web-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #sb-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #sb-title {
        height: auto;
        padding-bottom: 1;
    }
    #sb-status {
        height: auto;
        padding-bottom: 1;
    }
    #sb-form {
        height: 1fr;
    }
    #sb-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #spawn-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #spawn-header {
        padding-bottom: 1;
        height: auto;
    }
    #spawn-preview {
        height: auto;
        padding-bottom: 1;
    }
    #spawn-prompt {
        padding-top: 1;
        height: auto;
    }
    #spawn-runtime {
        height: auto;
        padding-bottom: 1;
    }
    #spawn-reject-prompt {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #spawn-error {
        color: $error;
        height: auto;
        padding-top: 1;
    }
    #spawn-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #handoff-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #handoff-header {
        padding-bottom: 1;
        height: auto;
    }
    #handoff-context-scroll {
        /* The report is the point of this dialog, so it gets the room —
           and its own scrollbar, because a hand-off can run long. */
        height: 1fr;
        min-height: 6;
        border: round $panel;
        padding: 0 1;
    }
    #handoff-context {
        height: auto;
    }
    #handoff-prompt {
        padding-top: 1;
        height: auto;
    }
    #handoff-runtime-prompt {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #handoff-runtime {
        height: auto;
    }
    #handoff-error {
        color: $error;
        height: auto;
    }
    #handoff-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #compact-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #compact-title {
        height: auto;
    }
    #compact-meta {
        height: auto;
        padding-bottom: 1;
    }
    #compact-stats {
        height: auto;
        padding-bottom: 1;
    }
    #compact-state {
        height: auto;
        padding-top: 1;
    }
    #compact-progress {
        height: auto;
        padding-top: 1;
        padding-bottom: 0;
    }
    #compact-cached {
        height: auto;
        padding-top: 1;
        padding-bottom: 1;
    }
    #compact-log {
        height: 1fr;
        border: solid $primary-darken-2;
        background: $surface-darken-1;
    }
    #compact-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #choose-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #choose-title { height: auto; padding-bottom: 1; }
    #choose-sched-label { height: auto; }
    #choose-scheduler { height: auto; }
    #choose-include-images { height: auto; padding-top: 1; }
    #choose-images-desc { height: auto; padding-bottom: 1; }
    #choose-k-label { height: auto; padding-top: 1; }
    #choose-k { height: 3; }
    #choose-k-desc { height: auto; padding-bottom: 1; }
    #choose-ratio-label { height: auto; padding-top: 1; }
    #choose-ratio { height: 3; }
    #choose-ratio-desc { height: auto; padding-bottom: 1; }
    #choose-hl-label { height: auto; padding-top: 1; }
    #choose-hl { height: 3; }
    #choose-hl-desc { height: auto; padding-bottom: 1; }
    #choose-budget-label { height: auto; padding-top: 1; }
    #choose-budget { height: 3; }
    #choose-preview { height: auto; padding-top: 1; }
    #choose-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #restore-dialog {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #restore-title { height: auto; }
    #restore-meta { height: auto; padding-bottom: 1; }
    #restore-status { height: auto; padding-bottom: 1; }
    #restore-table { height: 1fr; }
    #restore-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    #confirm-group-dialog {
        width: 100;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #confirm-group-header { height: auto; padding-bottom: 1; }
    #confirm-group-body {
        color: $text-muted;
        height: auto;
        padding-bottom: 1;
    }
    #confirm-group-hint { color: $text-muted; height: auto; }
    """

    BINDINGS = [
        Binding("n", "new_session", "New"),
        Binding("h", "handoff", "Handoff"),
        Binding("R", "resume_session", "Resume"),
        Binding("c", "compact", "Compact"),
        Binding("x", "restore", "Restore"),
        Binding("v", "visualize", "Visualize"),
        Binding("b", "toggle_auto_build", "Build on/off"),
        Binding("a", "ask_all", "Ask all"),
        Binding("i", "view_inbox", "View inbox"),
        Binding("w", "share_web", "Web share"),
        Binding("S", "secondbrain", "SecondBrain"),
        Binding("enter", "primary", "Attach/Resume"),
        Binding("k", "kill", "Kill"),
        Binding("g", "new_group", "New folder"),
        Binding("m", "move_to_group", "Move"),
        Binding("e", "rename_group", "Rename"),
        Binding("u", "ungroup", "Out of folder", show=False),
        Binding("space", "toggle_collapse", "Fold/unfold"),
        Binding("z", "toggle_collapse_all", "Fold all", show=False),
        Binding("escape", "clear_selection", "Cancel", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, claude_args: list[str]) -> None:
        super().__init__()
        self.claude_args = claude_args
        raw_cwd = os.environ.get("MULTIVERSE_ORIG_CWD") or os.getcwd()
        self.monitor_cwd = str(Path(raw_cwd).expanduser().resolve())
        self.sessions: list[Session] = []
        self.entries: list[DashboardRow] = []
        # Folder tree for this workspace, reloaded each tick so a second
        # monitor's edits show up here, and the flattened display order
        # (folder headers interleaved with agents) the table renders from.
        self.group_state: groups.GroupState = groups.GroupState()
        self.display_rows: list[groups.DisplayRow] = []
        # Folder-name edit in progress, rendered into the row itself.
        self._inline: Optional[_InlineEdit] = None
        # Destination pick in progress, walked in the tree itself.
        self._move: Optional[_MoveMode] = None
        # New agent being described in a placeholder row.
        self._draft: Optional[_DraftAgent] = None
        # shift+↑/↓ range: row keys, in display order, plus the anchor the
        # range grows from.
        self._selection: list[str] = []
        self._select_anchor: Optional[str] = None
        self._web_tag = ""
        # Fork JSONLs created by in-flight ask-all subprocesses. Hidden from
        # the dashboard + resume widget for their brief lifetime.
        self._active_fork_uuids: set[str] = set()
        self._fork_lock = threading.Lock()
        # Spawn-request queue state. Each tick scans ``spawn_requests/`` and
        # shows at most one modal at a time — the rest queue behind it.
        self._seen_spawn_ids: set[str] = set()
        self._spawn_queue: list[dict] = []
        self._active_spawn_id: Optional[str] = None
        self._active_spawn_modal: Optional[SpawnRequestScreen] = None
        # CompactScreens detached with ``d``: still running, off-stack, kept
        # alive via install_screen. Keyed by claude_uuid so Enter / ``c`` on
        # the (still ``compacting``) row can re-open the same instance.
        self._detached_compactions: dict[str, CompactScreen] = {}
        # When the attach sidecar forcibly detaches the user to deal with a
        # spawn request, we remember the tmux session here so we can
        # reattach once the queue fully drains (set by ``_attach_to``;
        # consumed by ``_tick_spawn_requests``).
        self._pending_reattach: Optional[str] = None
        # Cleared and refilled by each _redraw_table pass.
        self._inbox_cache: dict[tuple[str, str], int] = {}
        self._tokens_cache: dict[tuple[str, str], Optional[int]] = {}
        # chosen name → (spawner name, request id, approved at), for
        # spawns approved but not yet created. A ``None`` spawner marks a
        # name two approvals are racing for, which the monitor cannot
        # attribute. Deliberately in memory only: a spawn that never
        # happens should leave nothing behind, here or on disk.
        self._pending_group_spawns: dict[
            str, tuple[Optional[str], str, float]
        ] = {}
        self._handoff_in_progress = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        table = DashboardTable(cursor_type="row", zebra_stripes=True)
        # Name leads so the folder tree's connectors sit flush against the
        # left edge of the dashboard, where a tree reads.
        table.add_columns(
            "Name", "ID", "Runtime", "Inbox", "Build", "Status", "Tokens",
            "Updated", "Preview",
        )
        yield table
        yield Footer()

    def on_mount(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.title = "Multiverse Monitor"
        self.sub_title = f"cwd: {self.monitor_cwd}"
        self.refresh_sessions()
        self._recover_ending_sessions()
        self.set_interval(1.0, self.refresh_sessions)

    def _recover_ending_sessions(self) -> None:
        """Resume teardown requests left behind by an exited dashboard."""
        for session in self.sessions:
            if (
                session.status == "ending"
                and session.ending_marker_path.is_file()
                and (not session.host or session.host == current_host())
            ):
                try:
                    self._launch_session_teardown(session, delay=0.0)
                except OSError:
                    # The row remains ``ending`` and k can retry with a visible
                    # error. Startup recovery itself must not prevent mounting.
                    continue

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_primary()

    def refresh_sessions(self) -> None:
        all_sessions = list_sessions()
        # Scope to the monitor's cwd — other multiverse instances own sessions
        # in other cwds.
        self.sessions = [s for s in all_sessions if s.cwd == self.monitor_cwd]
        with self._fork_lock:
            hide = set(self._active_fork_uuids)
        self.entries = build_dashboard_rows(
            self.sessions,
            cwd=self.monitor_cwd,
            hide_uuids=hide,
        )
        self._file_arrived_spawns()
        self.group_state = groups.load(self.monitor_cwd)
        self._redraw_table()
        self._tick_spawn_requests()
        self._update_web_indicator()

    def _reload_groups_and_redraw(self) -> None:
        """Redraw for a change that is only about the tree.

        Folding, renaming, moving and deleting a folder touch the group
        store and nothing else. Going through ``refresh_sessions`` for
        those re-scans every transcript in the workspace — and re-runs the
        spawn tick and the web-share probe — from inside a keypress
        handler, which is both slow and capable of pushing a modal at the
        user because they folded a folder.
        """
        self.group_state = groups.load(self.monitor_cwd)
        self._redraw_table()

    def _file_arrived_spawns(self) -> None:
        """File approved spawns once the agent they named actually exists.

        Filing at approval time would leave a folder — and a membership
        row for a name with no session — behind whenever create_session
        fails or the requesting agent dies before running it.

        The window is deliberately short. An approval that never becomes a
        session is dropped once its request is gone (plus a grace period
        for the row to appear), and in any case after
        ``_SPAWN_FILING_TTL``: the name returns to the pool when the
        request does, and without the expiry an unrelated agent that later
        draws that name would be filed under a spawner it has nothing to
        do with.
        """
        if not self._pending_group_spawns:
            return
        now = time.time()
        live = {groups.member_key(e.name) for e in self.entries}
        for name, (spawner, rid, approved_at) in list(
            self._pending_group_spawns.items()
        ):
            waited = now - approved_at
            # The requesting agent removes its request file in a
            # ``finally``, so a missing file means it is done either way —
            # but the row it just created can take a tick or two to show
            # up, hence the grace period before believing it.
            gone = bool(rid) and not request_path(rid).is_file()
            if waited > _SPAWN_FILING_TTL or (
                gone and waited > _SPAWN_FILING_GRACE
            ):
                # Checked before the row, not after: an agent appearing
                # under this name long after the approval expired is a
                # different agent that drew a freed name.
                self._pending_group_spawns.pop(name, None)
                continue
            if groups.member_key(name) not in live:
                continue
            if not spawner:
                # Ambiguous (or an unnamed requester): the row exists, but
                # not whose child it is. Drop it rather than pick a parent.
                self._pending_group_spawns.pop(name, None)
                continue
            try:
                groups.auto_group_spawn(self.monitor_cwd, spawner, name)
            except (OSError, ValueError):
                continue  # transient; keep the pair and retry next pass
            self._pending_group_spawns.pop(name, None)

    def _update_web_indicator(self) -> None:
        """Reflect web-share state in the header subtitle. ``status`` also
        reaps a share whose processes have died, so this doubles as cleanup.
        """
        try:
            share = web_share.status(self.monitor_cwd)
        except Exception:
            share = None
        self._web_tag = "  ·  🌐 web: ON" if share is not None else ""
        self._set_subtitle()

    def _set_subtitle(self) -> None:
        """The header line doubles as the move mode's status bar — a mode
        needs somewhere to say what it is doing, and this is the one place
        that isn't another window."""
        banner = self._move_banner() or self._draft_banner()
        self.sub_title = banner or f"cwd: {self.monitor_cwd}{self._web_tag}"

    def _in_place_edit_active(self) -> bool:
        """True while a row in the table is being edited in place.

        The folder keys are App-level bindings, so they still fire in the
        moments the table itself does not have focus. Every in-place mode
        has to be listed here or those keys mutate the tree under a live
        draft, rename or move.
        """
        return (
            self._inline is not None
            or self._move is not None
            or self._draft is not None
        )

    def _user_is_busy(self) -> bool:
        """True while the user is mid-something the tick must not interrupt.

        A modal is the obvious case, but the draft row, the inline rename
        and the move mode are edits in the table itself rather than
        screens — pushing a spawn modal (or worse, the tmux reattach)
        under one of those steals the keystrokes mid-word and leaves the
        half-finished edit live behind it.
        """
        return self._in_place_edit_active() or isinstance(self.screen, ModalScreen)

    def _tick_spawn_requests(self) -> None:
        """One pass of the spawn-request watcher. Called every 1s.

        Responsibilities:
          1. Enqueue any newly-seen, cwd-matching, unresolved requests.
          2. If the active modal's request was resolved externally (another
             monitor got there first), dismiss it.
          3. If no modal is open and the queue is non-empty, push the next
             one — skipping queued entries that have been resolved since.
        """
        for req in list_pending_requests(self.monitor_cwd):
            rid = req.get("id")
            if not rid or rid in self._seen_spawn_ids:
                continue
            self._seen_spawn_ids.add(rid)
            self._spawn_queue.append(req)

        if self._active_spawn_id is not None:
            # Dismiss the open modal if the request was resolved externally
            # (another monitor got there first) OR the request file vanished
            # (the caller cleaned up — it finished, was rejected elsewhere,
            # or the caller interrupted/cancelled the wait).
            resolved = read_resolution(self._active_spawn_id) is not None
            gone = not request_path(self._active_spawn_id).is_file()
            if resolved or gone:
                modal = self._active_spawn_modal
                if modal is not None:
                    try:
                        modal.dismiss(None)
                    except Exception:
                        # Modal may already be tearing down; the callback
                        # handles the cleanup either way.
                        pass

        if self._active_spawn_id is None and self._spawn_queue:
            # Don't stomp on something the user opened explicitly (New,
            # Resume, Ask, a rename, a move). Wait for the next tick.
            if self._user_is_busy():
                return
            while self._spawn_queue:
                req = self._spawn_queue.pop(0)
                rid = req.get("id") or ""
                # The request may have been resolved by another monitor, or
                # cleaned up by the tool (rejection / caller-cancel path)
                # during the interval between enqueue and push. Drop
                # silently. (No timeout check — spawn requests never expire;
                # orphaned ones are filtered by list_pending_requests'
                # caller-liveness check instead.)
                if read_resolution(rid) is not None:
                    continue
                if not request_path(rid).is_file():
                    continue
                self._show_spawn_modal(req)
                break

        # Reattach path: if a spawn request previously yanked the user out
        # of a tmux session AND the queue has fully drained AND no modal
        # is open, put them back. Skip (with a heads-up) if the target
        # session was killed while they were away.
        if (
            self._pending_reattach
            and self._active_spawn_id is None
            and not self._spawn_queue
            and not self._user_is_busy()
        ):
            target = self._pending_reattach
            self._pending_reattach = None
            if tmux_session_exists(target):
                self.notify(f"Reattaching to {target}…")
                self._attach_to(target)
            else:
                self.notify(
                    f"Skipping reattach — {target} is no longer running",
                    severity="warning",
                )

    def _show_spawn_modal(self, req: dict) -> None:
        rid = req.get("id") or ""
        self._active_spawn_id = rid

        def on_close(decision: Optional[SpawnDecision]) -> None:
            self._active_spawn_id = None
            self._active_spawn_modal = None
            if decision is None:
                # External dismiss: another monitor resolved this first, so
                # there's no person here to explain anything. O_EXCL makes the
                # write a no-op against the winner.
                write_resolution(rid, status="rejected", reason="cancelled")
                return
            if not decision.accepted:
                # "cancelled" stays the default so an unexplained Esc reads the
                # same as it always did to the requesting agent.
                write_resolution(
                    rid,
                    status="rejected",
                    reason=decision.reason or "cancelled",
                )
                who = req.get("from", "?")
                if decision.reason:
                    self.notify(
                        f"Rejected spawn from '{who}': {decision.reason}",
                        markup=False,
                    )
                else:
                    self.notify(f"Rejected spawn from '{who}'", markup=False)
                return
            chosen_name, chosen_runtime = decision.name, decision.runtime
            won = write_resolution(
                rid,
                status="accepted",
                name=chosen_name,
                runtime=chosen_runtime,
            )
            if won:
                # File the pair here as well as in the spawning agent's MCP
                # server. Belt and braces on purpose: the server that calls
                # auto_group_spawn is the plugin build the AGENT started
                # with, so a long-running agent from an older build would
                # never file its children. The monitor is the process the
                # user just upgraded, and it is the authoritative resolver
                # (we won the O_EXCL race), so doing it here covers the
                # child regardless of what the agent is running. The two
                # calls agree: whichever runs first puts the spawner in a
                # folder, and the other simply joins the same one.
                #
                # Approval is not creation, though: the agent still has to
                # run create_session, and that can fail or never happen.
                # Remember the pair and file it when the child actually
                # turns up, so a failed spawn leaves no folder behind.
                #
                # Two live approvals on one name is the case this can't
                # resolve: whichever agent wins the name claim decides
                # whose child the row is, and that is settled elsewhere.
                # Rather than guess a parent, mark the name ambiguous and
                # skip the fallback — the agent's own server still files
                # the real winner on every current build, so only an old
                # build loses the (optional) grouping.
                held = self._pending_group_spawns.get(chosen_name)
                spawner: Optional[str] = req.get("from", "")
                if held is not None and held[1] != rid:
                    spawner = None
                self._pending_group_spawns[chosen_name] = (
                    spawner,
                    rid,
                    time.time(),
                )
                self.notify(
                    f"Approved spawn from '{req.get('from', '?')}' "
                    f"as '{chosen_name}' ({chosen_runtime})",
                    markup=False,
                )
            else:
                # Another monitor beat us to the resolution. Show what
                # actually won, if anything meaningful.
                actual = read_resolution(rid)
                if actual and actual.get("status") == "accepted":
                    won_name = actual.get("name") or "?"
                    won_runtime = actual.get("runtime") or RUNTIME_CLAUDE
                    self.notify(
                        f"Another monitor already accepted this "
                        f"spawn as '{won_name}' ({won_runtime})",
                        severity="warning",
                    )

        modal = SpawnRequestScreen(self.monitor_cwd, req)
        self._active_spawn_modal = modal
        self.push_screen(modal, on_close)

    def _pending_inbox(self, e: DashboardRow) -> int:
        """``pending_inbox_count`` for one agent, once per redraw."""
        key = (e.cwd, e.name)
        if key not in self._inbox_cache:
            self._inbox_cache[key] = pending_inbox_count(e.cwd, e.name)
        return self._inbox_cache[key]

    def _tokens_of(self, e: DashboardRow) -> Optional[int]:
        """``_agent_tokens`` for one agent, once per redraw."""
        key = (e.cwd, e.name)
        if key not in self._tokens_cache:
            self._tokens_cache[key] = self._agent_tokens(e)
        return self._tokens_cache[key]

    def _agent_tokens(self, e: DashboardRow) -> Optional[int]:
        """Token total shown for one agent, or ``None`` when unmeasurable.

        Displayed = transcript tokens + structural (derived from the last
        recorded API call's usage). Matches claude's own /clear / /context
        total to a few tokens. Falls back to just the transcript number
        when structural isn't measurable yet (freshly-compacted session
        before its first API call).

        Prefer the direct measurement — the last real (non-msg_priv) API
        request's usage total is literally what /context shows. It stays
        correct even when claude's microcompaction has evicted content the
        parser still sees on disk (parser + structural would over-count in
        that case). Fall back to parser + structural only for sessions with
        no recorded API call yet (freshly-compacted before first turn,
        brand-new session).
        """
        if not e.claude_uuid:
            return None
        last_usage = last_real_usage_total_cached(e.claude_uuid, e.cwd)
        if last_usage:
            return last_usage
        live_toks, _ = live_stats_cached(e.claude_uuid, e.cwd)
        struct_toks = structural_tokens_cached(e.claude_uuid, e.cwd)
        return live_toks + struct_toks

    def _agent_cells(self, row: groups.DisplayRow, now: float) -> tuple:
        e = row.entry
        style = STATUS_STYLES.get(e.status, "dim")
        status_cell = f"[{style}]{e.status}[/]"
        updated = _ago(now - e.status_ts) if e.status_ts else "-"
        if e.session is not None:
            id_cell = escape(e.session.id)
        else:
            session_id = e.session_id or ""
            id_cell = f"[dim]{escape(session_id[:8])}[/]"
        pending = self._pending_inbox(e)
        inbox_cell = f"[bold yellow]📩 {pending}[/]" if pending > 0 else "[dim]·[/]"
        if e.runtime != RUNTIME_CLAUDE:
            build_cell = "[dim]-[/]"
        elif e.claude_uuid and auto_build_enabled(e.claude_uuid):
            build_cell = "[bold green]on[/]"
        else:
            build_cell = "[dim]off[/]"
        tokens = self._tokens_of(e)
        tokens_cell = f"{tokens:,}" if tokens is not None else "[dim]-[/]"
        # Both are text the user or the transcript produced — the draft
        # row accepts any name, and a preview is whatever the agent last
        # said — so neither goes into a cell as markup.
        if e.preview:
            # Truncate first, escape second: the other way round can slice
            # an escape sequence in half.
            preview = e.preview[:59] + "…" if len(e.preview) > 60 else e.preview
            preview = escape(preview)
        else:
            preview = "[dim](empty)[/]"
        label = self._decorate(row, escape(e.name))
        name_cell = f"[dim]{row.prefix}[/]{label}" if row.prefix else label
        return (
            name_cell, id_cell, e.runtime, inbox_cell, build_cell,
            status_cell, tokens_cell, updated, preview,
        )

    def _group_cells(self, row: groups.DisplayRow, now: float) -> tuple:
        """Render a folder header.

        A collapsed folder has to answer, on its own line, the question its
        hidden rows would have answered: is anything in there waiting on
        me? So the header rolls up the inbox count, the most
        attention-worthy status, the token total and the freshest
        timestamp of everything beneath it — at any depth.
        """
        group = row.group
        chevron = (
            groups.CHEVRON_CLOSED if group.collapsed else groups.CHEVRON_OPEN
        )
        count = row.agent_count
        if self._inline is not None and self._inline.group_id == group.id:
            name_cell = self._inline_name_cell(row)
        else:
            label = self._decorate(
                row,
                f"[bold]{chevron} {escape(group.name)}[/][dim] ({count})[/]",
            )
            name_cell = f"[dim]{row.prefix}[/]{label}"

        agents = row.agents
        pending = sum(self._pending_inbox(a) for a in agents)
        inbox_cell = f"[bold yellow]📩 {pending}[/]" if pending > 0 else "[dim]·[/]"

        if agents:
            counts: dict[str, int] = {}
            for a in agents:
                counts[a.status] = counts.get(a.status, 0) + 1
            top = min(
                counts,
                key=lambda st: (_GROUP_STATUS_RANK.get(st, 99), st),
            )
            style = STATUS_STYLES.get(top, "dim")
            status_cell = f"[{style}]{counts[top]} {top}[/]"
        else:
            status_cell = "[dim]empty[/]"

        token_total = sum(
            t for t in (self._tokens_of(a) for a in agents) if t is not None
        )
        tokens_cell = f"[dim]{token_total:,}[/]" if token_total else "[dim]-[/]"

        latest = max((a.status_ts or 0 for a in agents), default=0)
        updated = _ago(now - latest) if latest else "-"

        # Expanded, the agents speak for themselves right below; collapsed,
        # the header is all the user has, so name who is in there.
        if group.collapsed and agents:
            shown = [a.name for a in agents[:3]]
            rest = len(agents) - len(shown)
            preview = ", ".join(shown) + (f", +{rest}" if rest > 0 else "")
            if len(preview) > 60:
                preview = preview[:59] + "…"
            preview_cell = f"[dim]{escape(preview)}[/]"
        else:
            preview_cell = ""

        return (
            name_cell, "", "[dim]-[/]", inbox_cell, "[dim]-[/]",
            status_cell, tokens_cell, updated, preview_cell,
        )

    def _redraw_table(self) -> None:
        table = self.query_one(DataTable)
        prev_key = self._cursor_row_key()
        prev_cursor = table.cursor_row
        table.clear()
        now = time.time()
        # One pass, one answer per agent: a folder header rolls up every
        # agent beneath it at any depth, so without this an agent nested
        # d deep is globbed and token-counted d+1 times per redraw — and
        # typing a folder name redraws on every keystroke.
        self._inbox_cache.clear()
        self._tokens_cache.clear()
        self.display_rows = groups.build_display_rows(
            self.entries,
            self.group_state,
            draft_in=(
                self._draft.group_id if self._draft is not None else groups.NO_DRAFT
            ),
        )
        # Drop selected keys whose rows are gone (killed, or folded away).
        if self._selection:
            live = {r.key for r in self.display_rows}
            self._selection = [k for k in self._selection if k in live]
        for row in self.display_rows:
            if row.is_group:
                cells = self._group_cells(row, now)
            elif row.is_draft:
                cells = self._draft_cells(row)
            else:
                cells = self._agent_cells(row, now)
            table.add_row(*cells, key=row.key)
        self._restore_cursor(table, prev_key, prev_cursor)
        self._set_subtitle()

    def _cursor_row_key(self) -> Optional[str]:
        """Row key currently under the cursor, from the previous draw."""
        if not self.display_rows:
            return None
        try:
            idx = self.query_one(DataTable).cursor_row
        except Exception:
            return None
        if isinstance(idx, int) and 0 <= idx < len(self.display_rows):
            return self.display_rows[idx].key
        return None

    def _restore_cursor(
        self, table: DataTable, prev_key: Optional[str], prev_cursor: Any
    ) -> None:
        """Keep the cursor on the same thing across a redraw.

        Index alone isn't enough once folders can collapse: folding a
        folder above the cursor shifts every row below it, and the cursor
        would silently land on a different agent. Follow the row key when
        it survives, and fall back to the old index only when it doesn't
        (the row was killed, or filtered away).
        """
        if not self.display_rows:
            return
        target: Optional[int] = None
        if prev_key is not None:
            for i, row in enumerate(self.display_rows):
                if row.key == prev_key:
                    target = i
                    break
        if target is None:
            fallback = prev_cursor if isinstance(prev_cursor, int) else 0
            target = max(0, min(fallback, len(self.display_rows) - 1))
        try:
            table.move_cursor(row=target)
        except Exception:
            pass

    def _current_display(self) -> Optional[groups.DisplayRow]:
        """The display row under the cursor — folder header or agent."""
        if not self.display_rows:
            return None
        table = self.query_one(DataTable)
        idx = table.cursor_row or 0
        if 0 <= idx < len(self.display_rows):
            return self.display_rows[idx]
        return None

    def _current_entry(self) -> Optional[DashboardRow]:
        """The agent under the cursor.

        ``None`` on a folder header, which is what every session action
        wants: they already return early on ``None``, so a folder row is
        inert for attach/kill/compact rather than acting on some arbitrary
        member.
        """
        row = self._current_display()
        if row is None or row.is_group:
            return None
        return row.entry

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------
    #
    # Three interactions live here, and none of them opens a window:
    #
    #   * naming     — the folder is created first and its name is edited
    #                  in the row itself (`_inline`), Finder-style.
    #   * selecting  — shift+↑/↓ grows a range (`_selection`), and the
    #                  folder keys then act on the whole range.
    #   * moving     — `_move` walks the folder tree in place with →/←
    #                  while the rows being moved stay visible.
    #
    # All three are plain app state that `_redraw_table` reads, so the
    # 1s refresh tick can run underneath any of them without disturbing
    # what the user is doing.

    def _intercept_key(self, event: events.Key) -> bool:
        """First refusal on a key, for the modes that own the arrows.

        Called from ``DashboardTable.on_key`` — before the table's own
        bindings resolve — so the inline editor and the move mode can hold
        ↑↓←→ and Enter for as long as they are active, and hand everything
        else back untouched.
        """
        if self._inline is not None:
            return self._inline_key(event)
        if self._draft is not None:
            return self._draft_key(event)
        if self._move is not None:
            return self._move_key(event)
        if event.key in _CURSOR_MOVE_KEYS and self._selection:
            # Moving the cursor ends the selection, the way every list
            # works. Decided here, on the keystroke, rather than from
            # DataTable's RowHighlighted message: that message is posted,
            # not called, and the table also emits one for its own
            # ``clear()`` mid-rebuild — so a selection was being wiped by
            # the very redraw that drew it.
            self._clear_selection()
            self._redraw_table()
        return False

    def _decorate(self, row: groups.DisplayRow, label: str) -> str:
        """Selection and move-mode styling for a name cell's label.

        Applied to the label only, never the tree connectors — reversing
        those too would smear the shape of the tree.
        """
        if row.key in self._selection:
            label = f"[reverse]{label}[/]"
        move = self._move
        if move is None:
            return label
        gid = row.group.id if (row.is_group and row.group is not None) else None
        name = row.entry.name if (not row.is_group and row.entry) else None
        if (gid and gid in move.group_ids) or (name and name in move.agent_names):
            return f"[b yellow]\u21e2[/] {label}"
        if gid and gid == move.dest:
            return f"[reverse]{label}[/] [b green]\u2190 drop here[/]"
        if gid and gid == move.highlight:
            return f"{label} [dim]\u2192 enter[/]"
        return label

    def _current_group(self) -> Optional[groups.Group]:
        row = self._current_display()
        if row is not None and row.is_group:
            return row.group
        return None

    def _cursor_folder_id(self) -> Optional[str]:
        """The folder the cursor is *in*: the highlighted folder itself, or
        the one holding the highlighted agent."""
        row = self._current_display()
        if row is None:
            return None
        if row.is_group and row.group is not None:
            return row.group.id
        if row.entry is not None:
            return self.group_state.group_of(row.entry.name)
        return None

    def _row_index(self, key: str) -> Optional[int]:
        for i, row in enumerate(self.display_rows):
            if row.key == key:
                return i
        return None

    def _move_cursor_to_key(self, key: str) -> None:
        idx = self._row_index(key)
        if idx is None:
            return
        try:
            self.query_one(DataTable).move_cursor(row=idx)
        except Exception:
            pass

    # -- selection ------------------------------------------------------

    def _clear_selection(self) -> None:
        self._selection = []
        self._select_anchor = None

    def _targets(self) -> tuple[list[str], list[str], str]:
        """What the folder keys act on: the selection, else the cursor row.

        Returns ``(group_ids, agent_names, label)``.
        """
        rows = [r for r in self.display_rows if r.key in set(self._selection)]
        if not rows:
            row = self._current_display()
            rows = [row] if row is not None else []
        group_ids = [r.group.id for r in rows if r.is_group and r.group]
        agent_names = [r.entry.name for r in rows if not r.is_group and r.entry]
        total = len(group_ids) + len(agent_names)
        if total == 1 and agent_names:
            label = f"'{agent_names[0]}'"
        elif total == 1:
            group = self.group_state.get(group_ids[0])
            label = f"folder '{group.name}'" if group else "folder"
        else:
            label = f"{total} items"
        return group_ids, agent_names, label

    def action_extend_selection_up(self) -> None:
        self._extend_selection(-1)

    def action_extend_selection_down(self) -> None:
        self._extend_selection(1)

    def _extend_selection(self, delta: int) -> None:
        """shift+↑/↓ — grow (or shrink) a contiguous range from an anchor."""
        if self._in_place_edit_active():
            return
        if not self.display_rows:
            return
        table = self.query_one(DataTable)
        idx = table.cursor_row if isinstance(table.cursor_row, int) else 0
        idx = max(0, min(idx, len(self.display_rows) - 1))
        if self._select_anchor is None:
            self._select_anchor = self.display_rows[idx].key
        target = max(0, min(idx + delta, len(self.display_rows) - 1))
        anchor_idx = self._row_index(self._select_anchor)
        if anchor_idx is None:
            anchor_idx = target
            self._select_anchor = self.display_rows[target].key
        lo, hi = sorted((anchor_idx, target))
        self._selection = [r.key for r in self.display_rows[lo : hi + 1]]
        try:
            table.move_cursor(row=target)
        except Exception:
            pass
        self._redraw_table()

    def action_clear_selection(self) -> None:
        """Escape: back out of whatever is in progress."""
        if self._inline is not None:
            self._cancel_inline_edit()
            return
        if self._draft is not None:
            self._cancel_draft()
            return
        if self._move is not None:
            self._cancel_move()
            return
        if self._selection:
            self._clear_selection()
            self._redraw_table()

    # -- inline naming --------------------------------------------------

    def _begin_inline_edit(
        self,
        kind: str,
        group: groups.Group,
        undo: Optional[groups.Wrap] = None,
        prev_selection: Optional[list[str]] = None,
        prev_cursor_key: Optional[str] = None,
    ) -> None:
        self._inline = _InlineEdit(
            kind=kind,
            group_id=group.id,
            buffer=group.name,
            cursor=len(group.name),
            select_all=True,
            undo=undo,
            prev_selection=list(prev_selection or ()),
            prev_cursor_key=prev_cursor_key,
        )
        self._move_cursor_to_key(groups.group_row_key(group.id))
        self._redraw_table()

    def _cancel_inline_edit(self) -> None:
        """Esc aborts, it doesn't just close the field.

        A rename simply reverts. A create undoes the whole thing — the
        folder goes away and everything it swept up goes back where it
        was, including a selection spanning several folders — because
        pressing Esc on a name prompt means "never mind", and leaving a
        stray "New folder" behind is not that.
        """
        edit = self._inline
        self._inline = None
        if edit is None:
            self._redraw_table()
            return
        if edit.kind != "create" or edit.undo is None:
            self._redraw_table()
            return
        try:
            groups.undo_wrap(self.monitor_cwd, edit.undo)
        except OSError as exc:
            self.notify(f"Could not undo: {exc}", severity="error")
            self._reload_groups_and_redraw()
            return
        self._reload_groups_and_redraw()
        # Put the user back where they were, selection included.
        self._selection = [
            k for k in edit.prev_selection
            if k in {r.key for r in self.display_rows}
        ]
        self._select_anchor = self._selection[0] if self._selection else None
        if self._selection:
            self._redraw_table()
        if edit.prev_cursor_key:
            self._move_cursor_to_key(edit.prev_cursor_key)
        self.notify("New folder discarded")

    def _commit_inline_edit(self) -> None:
        edit = self._inline
        if edit is None:
            return
        name = edit.buffer.strip()
        group = self.group_state.get(edit.group_id)
        if group is None:
            self._inline = None
            self._reload_groups_and_redraw()
            return
        if not name or name == group.name:
            self._inline = None
            self._redraw_table()
            return
        try:
            groups.rename_group(self.monitor_cwd, edit.group_id, name)
        except (ValueError, OSError) as exc:
            # Stay in the field so the name can be fixed without retyping.
            self.notify(str(exc), severity="error", markup=False)
            return
        self._inline = None
        self._reload_groups_and_redraw()
        self._move_cursor_to_key(groups.group_row_key(edit.group_id))

    def _edit_text(self, edit: Any, event: events.Key) -> bool:
        """One text field's worth of behaviour, shared by the folder-name
        editor and the new-agent draft. Returns True if the key was a text
        edit; the callers own Enter, Esc and Tab.

        ``select_all`` is Finder's "the name comes up highlighted": the
        first printable key replaces it rather than appending to it.
        """
        key = event.key
        if key in ("left", "right", "home", "end"):
            edit.select_all = False
            if key == "left":
                edit.cursor = max(0, edit.cursor - 1)
            elif key == "right":
                edit.cursor = min(len(edit.buffer), edit.cursor + 1)
            elif key == "home":
                edit.cursor = 0
            else:
                edit.cursor = len(edit.buffer)
            self._redraw_table()
            return True
        if key == "backspace":
            if edit.select_all:
                edit.buffer, edit.cursor = "", 0
            elif edit.cursor > 0:
                edit.buffer = (
                    edit.buffer[: edit.cursor - 1] + edit.buffer[edit.cursor :]
                )
                edit.cursor -= 1
            edit.select_all = False
            self._redraw_table()
            return True
        if key == "delete":
            if edit.select_all:
                edit.buffer, edit.cursor = "", 0
            else:
                edit.buffer = (
                    edit.buffer[: edit.cursor] + edit.buffer[edit.cursor + 1 :]
                )
            edit.select_all = False
            self._redraw_table()
            return True
        if key == "ctrl+u":
            edit.buffer, edit.cursor, edit.select_all = "", 0, False
            self._redraw_table()
            return True
        char = event.character
        if char and char.isprintable():
            if edit.select_all:
                edit.buffer, edit.cursor = "", 0
                edit.select_all = False
            edit.buffer = (
                edit.buffer[: edit.cursor] + char + edit.buffer[edit.cursor :]
            )
            edit.cursor += len(char)
            self._redraw_table()
            return True
        return False

    def _field_cell(self, edit: Any) -> str:
        """A text field rendered into a table cell, cursor and all.

        The buffer is whatever the user has typed, so it is escaped before
        it goes anywhere near markup — a lone ``[/]`` mid-rename would
        otherwise raise out of the table's render and take the app down.
        """
        if edit.select_all:
            return f"[reverse]{escape(edit.buffer) or ' '}[/]"
        before = escape(edit.buffer[: edit.cursor])
        at = escape(edit.buffer[edit.cursor : edit.cursor + 1]) or " "
        after = escape(edit.buffer[edit.cursor + 1 :])
        return f"{before}[reverse]{at}[/]{after}"

    def _inline_key(self, event: events.Key) -> bool:
        """Keys while a folder name is being edited. Consumes everything so
        a stray keystroke can't fire a dashboard action mid-rename."""
        edit = self._inline
        if edit is None:
            return False
        if event.key == "escape":
            self._cancel_inline_edit()
            return True
        if event.key == "enter":
            self._commit_inline_edit()
            return True
        self._edit_text(edit, event)
        return True

    def _inline_name_cell(self, row: groups.DisplayRow) -> str:
        """The name cell for the folder currently being renamed."""
        edit = self._inline
        assert edit is not None
        body = self._field_cell(edit)
        return f"[dim]{row.prefix}[/][bold]{groups.CHEVRON_OPEN}[/] {body}"

    # -- in-place move --------------------------------------------------

    def _move_key(self, event: events.Key) -> bool:
        """Keys while picking a destination. →/← walk into and out of
        folders; ↑/↓ pick which subfolder → would walk into."""
        move = self._move
        if move is None:
            return False
        key = event.key
        if key == "escape":
            self._cancel_move()
            return True
        if key == "enter":
            self._commit_move()
            return True
        candidates = self._move_candidates()
        if key in ("up", "down"):
            if candidates:
                keys = [g.id for g in candidates]
                if move.highlight in keys:
                    i = keys.index(move.highlight)
                    i = (i + (1 if key == "down" else -1)) % len(keys)
                else:
                    i = 0
                move.highlight = keys[i]
                self._sync_move_cursor()
                self._redraw_table()
            return True
        if key == "right":
            if move.highlight:
                move.dest = move.highlight
                groups.set_collapsed(self.monitor_cwd, move.dest, False)
                self.group_state = groups.load(self.monitor_cwd)
                move.highlight = self._first_candidate_in(move.dest)
                self._redraw_table()
                self._sync_move_cursor()
            return True
        if key == "left":
            if move.dest is not None:
                left = move.dest
                parent = self.group_state.get(move.dest)
                move.dest = parent.parent if parent is not None else None
                move.highlight = left
                self._redraw_table()
                self._sync_move_cursor()
            return True
        return True

    def _move_candidates(self) -> list[groups.Group]:
        """Subfolders of the current destination that are legal to enter —
        a folder can't be moved inside its own subtree."""
        move = self._move
        if move is None:
            return []
        blocked: set[str] = set()
        for gid in move.group_ids:
            blocked.add(gid)
            blocked |= set(self.group_state.descendants_of(gid))
        return [
            g
            for g in self.group_state.children_of(move.dest)
            if g.id not in blocked
        ]

    def _first_candidate_in(self, group_id: Optional[str]) -> Optional[str]:
        move = self._move
        if move is None:
            return None
        saved, move.dest = move.dest, group_id
        try:
            candidates = self._move_candidates()
        finally:
            move.dest = saved
        return candidates[0].id if candidates else None

    def _sync_move_cursor(self) -> None:
        """Keep the table cursor on whatever the move is pointing at, so the
        destination is visible even in a long list."""
        move = self._move
        if move is None:
            return
        focus = move.highlight or move.dest
        if focus:
            self._move_cursor_to_key(groups.group_row_key(focus))

    def action_move_to_group(self) -> None:
        """``m`` — start picking a destination, in the tree itself."""
        if self._inline is not None:
            return
        if self._move is not None:
            self._commit_move()
            return
        group_ids, agent_names, label = self._targets()
        if not group_ids and not agent_names:
            return
        self._move = _MoveMode(
            group_ids=group_ids,
            agent_names=agent_names,
            label=label,
            dest=None,
            highlight=None,
        )
        self._move.highlight = self._first_candidate_in(None)
        self._clear_selection()
        self._redraw_table()
        self._sync_move_cursor()

    def _cancel_move(self) -> None:
        self._move = None
        self._redraw_table()
        self.notify("Move cancelled")

    def _commit_move(self) -> None:
        move = self._move
        if move is None:
            return
        self._move = None
        try:
            moved = groups.move_into(
                self.monitor_cwd,
                move.dest,
                group_ids=move.group_ids,
                agent_names=move.agent_names,
            )
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")
            self._reload_groups_and_redraw()
            return
        self._reload_groups_and_redraw()
        dest = self.group_state.get(move.dest)
        where = f"into '{dest.name}'" if dest is not None else "to the top level"
        if moved:
            self.notify(f"Moved {move.label} {where}", markup=False)
        else:
            self.notify(f"{move.label} — already {where}", markup=False)
        focus = move.group_ids[0] if move.group_ids else None
        if focus:
            self._move_cursor_to_key(groups.group_row_key(focus))

    def _move_banner(self) -> str:
        move = self._move
        if move is None:
            return ""
        dest = self.group_state.get(move.dest)
        path = "/".join(self.group_state.path_names(move.dest)) if dest else "top level"
        return (
            f"⇢ moving {move.label} → {path}"
            "    ↑↓ pick · → into · ← out · ⏎ drop · esc cancel"
        )

    # -- structure keys -------------------------------------------------

    def action_indent(self) -> None:
        """``→`` — on a folder, open it; on anything else, wrap it in a new
        folder and start naming that folder in place."""
        if self._in_place_edit_active():
            return
        row = self._current_display()
        if row is None:
            return
        if not self._selection and row.is_group and row.group is not None:
            group = row.group
            if group.collapsed:
                groups.set_collapsed(self.monitor_cwd, group.id, False)
                self._reload_groups_and_redraw()
                self._move_cursor_to_key(row.key)
            else:
                idx = self._row_index(row.key)
                if idx is not None and idx + 1 < len(self.display_rows):
                    nxt = self.display_rows[idx + 1]
                    if nxt.depth > row.depth:
                        self._move_cursor_to_key(nxt.key)
            return
        group_ids, agent_names, _ = self._targets()
        if not group_ids and not agent_names:
            return
        prev_selection = list(self._selection)
        prev_cursor_key = row.key
        try:
            wrap = groups.wrap_in_new_folder(
                self.monitor_cwd,
                group_ids=group_ids,
                agent_names=agent_names,
            )
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        self._clear_selection()
        self._reload_groups_and_redraw()
        self._begin_inline_edit(
            "create",
            wrap.group,
            undo=wrap,
            prev_selection=prev_selection,
            prev_cursor_key=prev_cursor_key,
        )

    def action_outdent(self) -> None:
        """``←`` — on an open folder, close it; otherwise lift the row (or
        the selection) one level out of its folder."""
        if self._in_place_edit_active():
            return
        row = self._current_display()
        if row is None:
            return
        if not self._selection and row.is_group and row.group is not None:
            group = row.group
            if not group.collapsed:
                groups.set_collapsed(self.monitor_cwd, group.id, True)
                self._reload_groups_and_redraw()
                self._move_cursor_to_key(row.key)
            elif group.parent:
                self._move_cursor_to_key(groups.group_row_key(group.parent))
            # A collapsed folder already at the top level has nowhere left
            # to go: ← stops there rather than warning about it. Lifting a
            # nested folder out is ``u``'s job, not this key's.
            return
        group_ids, agent_names, _ = self._targets()
        self._lift(group_ids, agent_names)

    def _lift(self, group_ids: list[str], agent_names: list[str]) -> None:
        if not group_ids and not agent_names:
            return
        focus_key = None
        row = self._current_display()
        if row is not None:
            focus_key = row.key
        try:
            moved = groups.lift_out(
                self.monitor_cwd,
                group_ids=group_ids,
                agent_names=agent_names,
            )
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        if not moved:
            self.notify("Already at the top level", severity="warning")
            return
        self._clear_selection()
        self._reload_groups_and_redraw()
        if focus_key:
            self._move_cursor_to_key(focus_key)

    def action_new_group(self) -> None:
        """``g`` — a new folder, always.

        With a selection, or on an agent row, that is ``→``'s wrap. On a
        folder header ``→`` means "open this", so there it makes an empty
        subfolder inside the folder the cursor is in — otherwise the key
        the footer calls "New folder" is the one key that cannot make one.
        """
        if self._in_place_edit_active():
            return
        row = self._current_display()
        if self._selection or (row is not None and not row.is_group):
            self.action_indent()
            return
        parent_id = self._cursor_folder_id()
        parent = self.group_state.get(parent_id)
        # A new row inside a folded folder would be invisible, and the
        # name field with it.
        if parent is not None and parent.collapsed:
            groups.set_collapsed(self.monitor_cwd, parent.id, False)
        try:
            wrap = groups.wrap_in_new_folder(self.monitor_cwd, parent=parent_id)
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error", markup=False)
            # The expand above is already on disk; don't leave the cached
            # tree disagreeing with it until the next tick.
            self._reload_groups_and_redraw()
            return
        self._reload_groups_and_redraw()
        self._begin_inline_edit(
            "create",
            wrap.group,
            undo=wrap,
            prev_cursor_key=row.key if row is not None else None,
        )

    def action_rename_group(self) -> None:
        """``e`` — edit the folder's name where it sits."""
        if self._in_place_edit_active():
            return
        group = self.group_state.get(self._cursor_folder_id())
        if group is None:
            self.notify(
                "Highlight a folder — or an agent inside one — to rename it",
                severity="warning",
            )
            return
        self._begin_inline_edit("rename", group)

    def action_ungroup(self) -> None:
        """``u`` — lift out one level, without ``←``'s fold-first behaviour."""
        if self._in_place_edit_active():
            return
        group_ids, agent_names, _ = self._targets()
        self._lift(group_ids, agent_names)

    def action_toggle_collapse(self) -> None:
        """``space`` — fold or unfold a folder."""
        if self._in_place_edit_active():
            return
        row = self._current_display()
        if row is None:
            return
        if row.is_group and row.group is not None:
            groups.toggle_collapsed(self.monitor_cwd, row.group.id)
            self._reload_groups_and_redraw()
            self._move_cursor_to_key(row.key)
            return
        # On an agent, fold the folder it lives in — and ride the cursor up
        # to that header, since the row it was on is about to disappear.
        gid = (
            self.group_state.group_of(row.entry.name)
            if row.entry is not None
            else None
        )
        if gid is None:
            self.notify("Not in a folder — nothing to fold", severity="warning")
            return
        groups.set_collapsed(self.monitor_cwd, gid, True)
        self._reload_groups_and_redraw()
        self._move_cursor_to_key(groups.group_row_key(gid))

    def action_toggle_collapse_all(self) -> None:
        """``z`` — fold everything, or unfold everything if already folded."""
        if self._in_place_edit_active():
            return
        if not self.group_state.groups:
            self.notify("No folders yet — press → to make one", severity="warning")
            return
        collapse = any(
            not g.collapsed for g in self.group_state.groups.values()
        )
        groups.set_all_collapsed(self.monitor_cwd, collapse)
        self._reload_groups_and_redraw()
        self.notify("Folded every folder" if collapse else "Unfolded every folder")

    def _delete_group_flow(self, group: groups.Group) -> None:
        """``k`` on a folder header: remove the folder, keep the agents.

        An empty folder goes without a prompt — there is nothing to lose
        and nothing hidden. A folder with contents still confirms, because
        when it's folded the user can't see how much is about to be tipped
        back out.
        """
        state = self.group_state
        # Count agents the dashboard can actually show. Membership is kept
        # by name and nothing prunes it when a transcript is deleted, so
        # the raw member list can name agents that no longer exist — and
        # then the prompt promises to move rows the user cannot see.
        live = {groups.member_key(e.name) for e in self.entries}
        agent_count = len(state.members_of(group.id, recursive=True) & live)
        subgroup_count = len(state.descendants_of(group.id))

        def remove() -> None:
            try:
                groups.delete_group(self.monitor_cwd, group.id)
            except OSError as exc:
                self.notify(f"Failed to remove folder: {exc}", severity="error")
                return
            self._reload_groups_and_redraw()
            self.notify(f"Removed folder '{group.name}'", markup=False)

        if not agent_count and not subgroup_count:
            remove()
            return

        parent = state.get(group.parent)

        def on_close(confirmed: Optional[bool]) -> None:
            if confirmed:
                remove()

        self.push_screen(
            ConfirmGroupDeleteScreen(
                group.name,
                agent_count,
                subgroup_count,
                parent.name if parent is not None else None,
            ),
            on_close,
        )

    def action_new_session(self) -> None:
        """``n`` — start a new agent where the cursor is, like making a file
        in the folder you are looking at.

        Nothing is spawned yet: a placeholder row appears in the tree with
        its suggested name selected for typing over, and Tab cycles the
        runtime. Enter creates it; Esc costs nothing because nothing has
        started.
        """
        if self._inline is not None or self._move is not None:
            return
        if self._draft is not None:
            self._commit_draft()
            return
        # The folder the cursor is in — on a folder header, that folder;
        # on an agent, whichever folder holds it.
        self._begin_draft(group_id=self._cursor_folder_id())

    def _begin_draft(self, *, group_id: Optional[str]) -> bool:
        """Open a placeholder row for a session that doesn't exist yet."""
        suggestion = pick_random_name(all_session_names_in_cwd(self.monitor_cwd))
        if not suggestion:
            self.notify(
                "Could not pick a name — the workspace pool is exhausted",
                severity="error",
            )
            return False
        self._draft = _DraftAgent(
            buffer=suggestion,
            cursor=len(suggestion),
            select_all=True,
            runtime=RUNTIME_CLAUDE,
            group_id=group_id,
        )
        self._clear_selection()
        self._redraw_table()
        self._move_cursor_to_key(groups.DRAFT_ROW_KEY)
        return True

    def _draft_key(self, event: events.Key) -> bool:
        """Keys while a new agent is being described. Consumes everything,
        so a stray keystroke can't act on the dashboard behind the draft."""
        draft = self._draft
        if draft is None:
            return False
        key = event.key
        if key == "escape":
            self._cancel_draft()
            return True
        if key == "enter":
            self._commit_draft()
            return True
        if key in ("tab", "shift+tab"):
            step = 1 if key == "tab" else -1
            order = list(RUNTIMES)
            i = order.index(draft.runtime) if draft.runtime in order else 0
            draft.runtime = order[(i + step) % len(order)]
            self._redraw_table()
            return True
        self._edit_text(draft, event)
        return True

    def _cancel_draft(self) -> None:
        """Esc on a draft. Nothing was created, so there is nothing to undo
        — but this is reached from two places (the key interceptor and the
        app-level Esc binding, which covers the case where the table isn't
        focused), so it lives in one place to keep them from drifting.
        """
        if self._draft is None:
            return
        self._draft = None
        self._redraw_table()
        self.notify("New agent cancelled")

    def _commit_draft(self) -> None:
        draft = self._draft
        if draft is None:
            return
        name = draft.buffer.strip()
        # Both checks leave the draft open so the name can be fixed
        # without starting over.
        if not name:
            self.notify("Name cannot be empty", severity="error")
            return
        if name in all_session_names_in_cwd(self.monitor_cwd):
            self.notify(
                f"'{name}' is already used in this workspace",
                severity="error",
                markup=False,
            )
            return
        runtime = draft.runtime
        folder = self.group_state.get(draft.group_id)
        runtime_args = self.claude_args if runtime == RUNTIME_CLAUDE else []
        try:
            session = create_session(
                self.monitor_cwd,
                runtime_args,
                name=name,
                runtime=runtime,
            )
        except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
            self.notify(f"Spawn failed: {e}", severity="error")
            return
        self._draft = None
        where = f" in '{folder.name}'" if folder is not None else ""
        self.notify(f"Spawned {name} with {runtime}{where}", markup=False)
        if session is None:
            self.refresh_sessions()
            return
        if folder is not None:
            try:
                groups.assign_agent(self.monitor_cwd, name, folder.id)
            except (ValueError, OSError):
                pass  # the session exists; filing it is a nicety
        self.refresh_sessions()
        self._move_cursor_to_key(f"mv:{session.id}")

    def _draft_cells(self, row: groups.DisplayRow) -> tuple:
        """The placeholder row: a name field and a Tab-cycled runtime."""
        draft = self._draft
        assert draft is not None
        name_cell = f"[dim]{row.prefix}[/]{self._field_cell(draft)}"
        return (
            name_cell,
            "[dim]—[/]",
            f"[reverse]{draft.runtime}[/]",
            "[dim]·[/]",
            "[dim]-[/]",
            "[b green]new[/]",
            "[dim]-[/]",
            "-",
            "[dim]⇥ runtime · ⏎ create · esc cancel[/]",
        )

    def _draft_banner(self) -> str:
        draft = self._draft
        if draft is None:
            return ""
        folder = self.group_state.get(draft.group_id)
        where = f"in {'/'.join(self.group_state.path_names(folder.id))}" if folder else "at the top level"
        return (
            f"+ new {draft.runtime} agent {where}"
            "    type a name · ⇥ runtime · ⏎ create · esc cancel"
        )

    def action_handoff(self) -> None:
        """Generate context from a dormant agent, then create its successor."""
        target = self._current_entry()
        if target is None:
            return
        if target.is_ghost:
            self.notify("Folded ghost rows cannot be handed off", severity="warning")
            return
        if target.status != "dormant" or target.session is not None:
            self.notify(
                "Handoff is only available for dormant agents",
                severity="warning",
            )
            return
        native_id = target.session_id or target.claude_uuid
        if not native_id:
            self.notify(
                f"'{target.name}' has no native session ID to hand off",
                severity="error",
                markup=False,
            )
            return
        if self._handoff_in_progress:
            self.notify("A handoff context is already being generated", severity="warning")
            return

        self._handoff_in_progress = True
        self.notify(f"Generating handoff context from '{target.name}'…", markup=False)
        self._handoff_context_worker(target, native_id)

    @work(thread=True, exclusive=True, group="handoff")
    def _handoff_context_worker(
        self, target: DashboardRow, native_id: str
    ) -> None:
        try:
            result = ask_one(
                question=_HANDOFF_PROMPT,
                target=AskTarget(
                    name=target.name,
                    cwd=target.cwd,
                    claude_uuid=target.claude_uuid,
                    runtime=target.runtime,
                    session_id=native_id,
                ),
                timeout=_HANDOFF_TIMEOUT_SECONDS,
                register_fork=self._register_fork,
            )
        except Exception as exc:  # noqa: BLE001 — return failure to the TUI
            result = AskResult(
                name=target.name,
                response=None,
                error=f"handoff fork failed: {exc}",
                fork_uuid="",
                elapsed=0.0,
            )
        self.call_from_thread(self._handoff_context_ready, target, native_id, result)

    def _handoff_context_ready(
        self,
        target: DashboardRow,
        native_id: str,
        result: AskResult,
    ) -> None:
        self._handoff_in_progress = False
        if result.error or not result.response:
            detail = result.error or "the predecessor returned an empty handoff"
            self.notify(
                f"Could not generate handoff context for '{target.name}': {detail}",
                severity="error",
                markup=False,
            )
            return

        handoff_context = result.response.strip()

        def create_successor(
            successor_name: str, successor_runtime: str
        ) -> Optional[Session]:
            retired: Optional[Session] = None
            try:
                # A dormant Claude transcript has no writer, so this is the
                # final synchronization. Once the retired record exists all
                # future compactions deliberately treat full.jsonl as frozen.
                if target.runtime == RUNTIME_CLAUDE:
                    sync_full(native_id, target.cwd)
                retired = create_retired_session_record(
                    cwd=target.cwd,
                    name=target.name,
                    runtime=target.runtime,
                    native_session_id=native_id,
                    successor=successor_name,
                )
                runtime_args = (
                    self.claude_args if successor_runtime == RUNTIME_CLAUDE else []
                )
                session = create_session(
                    target.cwd,
                    runtime_args,
                    name=successor_name,
                    runtime=successor_runtime,
                    initial_prompt=_successor_prompt(
                        target.name, handoff_context
                    ),
                )
            except (OSError, subprocess.CalledProcessError, ValueError, RuntimeError) as exc:
                # Retirement is committed first so the predecessor can never
                # race back to life while its successor starts. If startup
                # fails, remove only that wrapper record; the native dormant
                # transcript was never deleted and becomes available again.
                if retired is not None:
                    remove_retired_session_record(retired)
                self.refresh_sessions()
                self.notify(f"Handoff failed: {exc}", severity="error", markup=False)
                return None

            self.notify(
                f"Retired '{target.name}' and spawned successor "
                f"'{successor_name}' ({successor_runtime})",
                markup=False,
            )
            return session

        folder_id = self.group_state.group_of(target.name)

        def on_close(chosen: Optional[tuple[str, str]]) -> None:
            if chosen is None:
                self.notify(
                    f"Handoff from '{target.name}' cancelled", markup=False
                )
                return
            successor_name, successor_runtime = chosen
            session = create_successor(successor_name, successor_runtime)
            if session is None:
                self.refresh_sessions()
                return
            # The successor lands in the predecessor's folder, so a
            # hand-off keeps the lineage together the way a spawn does.
            if folder_id is not None:
                try:
                    groups.assign_agent(
                        self.monitor_cwd, successor_name, folder_id
                    )
                except (ValueError, OSError):
                    pass  # the session exists; filing it is a nicety
            self.refresh_sessions()
            self._move_cursor_to_key(f"mv:{session.id}")

        # Unlike `n`, this spawn has something to read before it happens:
        # the report the predecessor wrote for whoever takes over. Show it
        # in full, with the next name in the line filled in.
        self.push_screen(
            HandoffSuccessorScreen(
                target.cwd,
                target.name,
                target.runtime,
                handoff_context,
                pick_successor_name(
                    target.name, all_session_names_in_cwd(target.cwd)
                ),
            ),
            on_close,
        )

    def action_resume_session(self) -> None:
        hide = wrapped_runtime_session_ids(self.sessions)
        with self._fork_lock:
            hide |= {(RUNTIME_CLAUDE, uuid) for uuid in self._active_fork_uuids}

        def on_picked(result: Optional[tuple[str, str, Optional[str]]]) -> None:
            if not result:
                return
            runtime, session_id, session_name = result

            existing = existing_names_in_cwd(self.monitor_cwd)
            if session_name and session_name not in existing:
                self._spawn_resumed(
                    self.monitor_cwd, runtime, session_id, session_name
                )
                return

            if session_name:
                reason = (
                    f"'{session_name}' is already used in this workspace. "
                    "Pick a different name for this session:"
                )
                default_name = session_name
            else:
                reason = (
                    f"This {runtime} session has no name yet. "
                    "Assign a name for the multiverse session:"
                )
                default_name = pick_random_name(existing) or ""

            def on_name(name: Optional[str]) -> None:
                if not name:
                    return
                self._spawn_resumed(
                    self.monitor_cwd, runtime, session_id, name
                )

            self.push_screen(
                NameInputScreen(self.monitor_cwd, default_name, reason), on_name
            )

        self.push_screen(ResumeSessionScreen(self.monitor_cwd, hide), on_picked)

    def _attach_to(self, tmux_session: str) -> None:
        """Attach the user to a tmux session, with a sidecar guard that
        force-detaches them back to this monitor if a spawn_agent request
        arrives while they're inside. On forced detach, queues a reattach
        that will fire once the spawn queue fully drains.
        """
        session = next(
            (item for item in self.sessions if item.tmux_name == tmux_session),
            None,
        )
        if session is not None and (
            session.status in ("ending", "retired")
            or session.ending_marker_path.is_file()
        ):
            state = session.status
            self.notify(
                f"Session is {state} — attach is disabled",
                severity="warning",
            )
            return

        guard = _SpawnAttachGuard(cwd=self.monitor_cwd, tmux_session=tmux_session)
        guard.start()
        try:
            with self.suspend():
                attach_tmux_session(
                    tmux_session,
                    on_client_started=guard.set_client_pid,
                )
        finally:
            guard.stop()

        if guard.forced_detach:
            self._pending_reattach = tmux_session
            self.notify(
                f"Detached from {tmux_session} to review a spawn request — "
                "will reattach when the queue clears."
            )

    def _spawn_resumed(
        self, cwd: str, runtime: str, session_id: str, name: str
    ) -> None:
        # Strip ghost-row display suffixes — passing the literal "step2b
        # (folded 02:33)" as `--name` would write that into the new
        # session's custom-title and the dashboard would re-suffix it on
        # the next refresh, accumulating ad infinitum.
        if runtime == RUNTIME_CLAUDE:
            name = strip_fold_suffix(name) or name
            runtime_args = ["--resume", session_id]
        elif runtime == RUNTIME_CODEX:
            runtime_args = ["resume", session_id]
        else:
            runtime_args = ["--session", session_id]
        try:
            create_session(
                cwd,
                runtime_args,
                resumed_from=session_id,
                name=name,
                runtime=runtime,
            )
        except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
            self.notify(f"Resume failed: {e}", severity="error")
            return
        self.refresh_sessions()
        self.notify(f"Resumed {runtime} {session_id[:8]} as {name}", markup=False)

    def action_primary(self) -> None:
        """Enter: attach for live sessions, resume for dormant/ended ones.

        On a folder header there is nothing to attach to, so Enter does the
        folder-ish thing and folds it.
        """
        row = self._current_display()
        if row is not None and row.is_group and row.group is not None:
            groups.toggle_collapsed(self.monitor_cwd, row.group.id)
            self._reload_groups_and_redraw()
            self._move_cursor_to_key(row.key)
            return
        e = self._current_entry()
        if e is None:
            return

        if e.status == "compacting":
            # Ours, detached with `d`? Re-open it rather than refuse.
            if e.claude_uuid and self._reattach_compaction(e.claude_uuid):
                return
            self.notify(
                "Session is compacting in another monitor — wait until it "
                "finishes",
                severity="warning",
            )
            return

        if e.status == "ending":
            self.notify(
                "Session is ending — attach is disabled until teardown finishes",
                severity="warning",
            )
            return

        if e.status == "retired":
            successor = e.session.successor if e.session is not None else None
            hint = f"; its successor is '{successor}'" if successor else ""
            self.notify(
                f"Session is retired and cannot be attached or resumed{hint}",
                severity="warning",
            )
            return

        # Live multiverse → attach.
        if e.is_live and e.session is not None:
            if not tmux_session_exists(e.session.tmux_name):
                self.notify(f"Session {e.session.id} is not running", severity="warning")
                return
            self._attach_to(e.session.tmux_name)
            self.refresh_sessions()
            return

        # Dormant or ended — resume the underlying runtime session.
        session_id = e.session_id
        if not session_id:
            self.notify(
                f"No {e.runtime} session ID tracked for this row — cannot resume",
                severity="warning",
            )
            return

        # Claude's own reaper (`cleanupPeriodDays`, default 30) deletes
        # dormant live JSONLs, while our state dir and the full.jsonl
        # mirror both survive it. The row therefore keeps rendering as
        # normal, but `claude --resume <uuid>` exits 1 with "No
        # conversation found" — and because claude IS the tmux session's
        # command, that exit takes the tmux session down with it, so the
        # resume looks like it silently did nothing at all.
        #
        # The session is fully recoverable the whole time: its full.jsonl
        # is intact and RestoreScreen would list it. Detect the reaped
        # shape here and point at the path that works instead of spawning
        # a doomed session.
        #
        # Ordering matters: this must run BEFORE the kill_session() below,
        # which rmtree's the multiverse state dir. Failing after that
        # point would destroy the row's identity on behalf of a resume
        # that was never going to succeed.
        if e.runtime == RUNTIME_CLAUDE:
            if not live_jsonl_path(session_id, e.cwd).is_file():
                if full_jsonl_path(session_id).is_file():
                    self.notify(
                        f"'{e.name}' has no live transcript — claude's "
                        f"cleanupPeriodDays reaper likely deleted it. The "
                        f"full mirror is intact: press x to restore it.",
                        severity="warning",
                    )
                else:
                    self.notify(
                        f"'{e.name}' has no live transcript and no full "
                        f"mirror — nothing to resume.",
                        severity="error",
                    )
                return

        # Ended multiverse row: free the name by reaping its meta dir before
        # spawning the replacement (otherwise existing_names_in_cwd blocks it).
        if e.session is not None and e.status == "ended":
            if foreign_host_still_reported_live(e.session):
                self.notify(
                    f"'{e.name}' was created on host '{e.session.host}' and "
                    "its status there still says it's active — refusing to "
                    "resume as a new session here to avoid creating a "
                    "duplicate. Kill it from its own host first if it's "
                    "actually dead.",
                    severity="error",
                )
                return
            try:
                kill_session(e.session)
            except RuntimeError as exc:
                self.notify(f"Could not stop runtime: {exc}", severity="error")
                return

        self._spawn_resumed(e.cwd, e.runtime, session_id, e.name)

    def _register_fork(self, fork_uuid: str, active: bool) -> None:
        with self._fork_lock:
            if active:
                self._active_fork_uuids.add(fork_uuid)
            else:
                self._active_fork_uuids.discard(fork_uuid)

    def action_ask_all(self) -> None:
        targets: list[AskTarget] = []
        for e in self.entries:
            if e.session_id or e.claude_uuid:
                targets.append(
                    AskTarget(
                        name=e.name,
                        cwd=e.cwd,
                        claude_uuid=e.claude_uuid,
                        runtime=e.runtime,
                        session_id=e.session_id,
                    )
                )
        if not targets:
            self.notify("No named sessions to ask", severity="warning")
            return

        def on_question(q: Optional[str]) -> None:
            if not q:
                return
            self.notify(f"Asking {len(targets)} session(s)…")
            self._ask_worker(q, targets)

        self.push_screen(AskQuestionScreen(len(targets)), on_question)

    @work(exclusive=True, thread=True, group="ask_all")
    def _ask_worker(self, question: str, targets: list[AskTarget]) -> None:
        results = ask_all(
            question, targets, register_fork=self._register_fork
        )
        self.call_from_thread(self._show_ask_results, question, results)

    def _show_ask_results(
        self, question: str, results: list[AskResult]
    ) -> None:
        ok = sum(1 for r in results if r.response and not r.error)
        self.notify(f"Got {ok}/{len(results)} response(s)")
        self.push_screen(AskResultsScreen(question, results))

    def action_view_inbox(self) -> None:
        """Open a read-only viewer for the highlighted agent's pending inbox.

        Snapshot only — pressing ``i`` does not consume the messages.
        They remain in the inbox to be delivered to the agent on its next
        ``SessionStart`` / ``UserPromptSubmit`` / ``Stop`` hook (or by
        ``check_inbox``), so this is purely a "peek" for the user.
        """
        e = self._current_entry()
        if e is None:
            return
        messages = list_pending_messages(e.cwd, e.name)
        if not messages:
            self.notify(f"No pending messages for '{e.name}'", markup=False)
            return
        self.push_screen(InboxViewerScreen(e.name, messages))

    def action_share_web(self) -> None:
        """Toggle remote browser control of this workspace (the `w` key).

        Opens a panel that starts/stops an ephemeral, per-workspace web
        share: a ttyd bound to loopback serving a dedicated monitor for
        this cwd, exposed via the configured tunnel + auth. Workspace-level
        (not row-level), so selection doesn't matter.
        """

        def _on_close(_: None) -> None:
            self.refresh_sessions()

        self.push_screen(WebShareScreen(self.monitor_cwd), _on_close)

    def action_secondbrain(self) -> None:
        """Enroll / connect / disconnect this device to SecondBrain (the `S` key).

        Opens a panel to authenticate this device to the SecondBrain apprentice and
        bring up (or tear down) the connector daemon that streams perception events to
        it. Device-level (not row-level), so selection doesn't matter.
        """

        def _on_close(_: None) -> None:
            self.refresh_sessions()

        self.push_screen(SecondBrainScreen(self.monitor_cwd), _on_close)

    def action_kill(self) -> None:
        row = self._current_display()
        if row is not None and row.is_group and row.group is not None:
            self._delete_group_flow(row.group)
            return
        e = self._current_entry()
        if e is None:
            return
        if e.status == "compacting":
            self.notify(
                "Session is compacting — kill blocked until it finishes",
                severity="warning",
            )
            return
        if e.status == "ending":
            if e.session is None:
                self.notify("Session is already ending", severity="warning")
                return
            if e.session.host and e.session.host != current_host():
                self.notify(
                    f"Session is ending on host {e.session.host}; "
                    "teardown cannot be retried from this host",
                    severity="warning",
                )
                return
            self._start_session_teardown(
                e.session,
                f"Finished teardown for {e.session.id}",
                delay=0.0,
            )
            return
        if e.status == "retired":
            self.notify(
                "Retired sessions are preserved and cannot be killed",
                severity="warning",
            )
            return
        if e.session is None:
            native_session_id = e.session_id
            cwd = e.cwd
            display_name = e.name
            runtime = e.runtime

            def _on_close(confirmed: Optional[bool]) -> None:
                if not confirmed:
                    return
                if not native_session_id:
                    self.notify(
                        f"No {runtime} session ID tracked for this row — cannot delete",
                        severity="error",
                    )
                    return
                try:
                    if runtime == RUNTIME_CODEX:
                        delete_codex_thread(native_session_id)
                    elif runtime == RUNTIME_PI:
                        delete_pi_session(native_session_id, cwd)
                    else:
                        jsonl = live_jsonl_path(native_session_id, cwd)
                        if not jsonl.is_file():
                            self.notify("JSONL already gone", severity="warning")
                            self.refresh_sessions()
                            return
                        jsonl.unlink()
                except (OSError, RuntimeError, ValueError) as exc:
                    self.notify(f"Failed to delete: {exc}", severity="error")
                    return
                # The name goes back into the pool the moment the
                # transcript is gone, so drop its folder membership too —
                # otherwise the next agent to draw this name is filed
                # into a folder it was never put in.
                try:
                    groups.assign_agent(cwd, display_name, None)
                except OSError:
                    # The transcript is already gone; a stale membership
                    # row is not worth failing the delete over.
                    pass
                self.notify(
                    f"Deleted dormant session '{display_name}'", markup=False
                )
                self.refresh_sessions()

            self.push_screen(
                ConfirmDeleteScreen(display_name, runtime), _on_close
            )
            return
        if foreign_host_still_reported_live(e.session):
            # Session was created on a different host and never wrote
            # `ended` there. Normally we refuse (avoids duplicate-session
            # bookkeeping issues while the original process is still
            # running elsewhere). But when a whole container/machine dies
            # without a clean shutdown — recycled Beaker container, dead
            # remote — every session it owned is stuck this way, with no
            # way for the user to clear them from any other host. Offer
            # a confirm-and-force-kill path here, keyed on the user's
            # first-hand knowledge that the foreign host is gone.
            session = e.session
            display_name = e.name

            def _on_force_close(confirmed: Optional[bool]) -> None:
                if not confirmed:
                    return
                self._begin_session_kill(
                    session,
                    f"Force-killed '{display_name}' "
                    f"(was on host {session.host})",
                )

            self.push_screen(
                ConfirmForceKillScreen(
                    display_name=display_name,
                    other_host=e.session.host or "(unknown)",
                    current=current_host(),
                ),
                _on_force_close,
            )
            return

        self._begin_session_kill(e.session, f"Killed {e.session.id}")

    def _begin_session_kill(self, session: Session, success_message: str) -> None:
        """Persist ``ending`` and hand teardown to a dashboard-independent process."""
        try:
            mark_session_ending(session)
        except OSError as exc:
            self.notify(f"Could not mark session as ending: {exc}", severity="error")
            return

        self._start_session_teardown(
            session,
            success_message,
            delay=_ENDING_MIN_VISIBLE_SECONDS,
        )

    def _launch_session_teardown(
        self, session: Session, *, delay: float
    ) -> subprocess.Popen[bytes]:
        """Launch a serialized teardown process that survives this dashboard."""
        plugin_root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env["MULTIVERSE_ROOT"] = str(STATE_DIR.parent)
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "src.session_teardown",
                session.id,
                "--delay",
                str(delay),
            ],
            cwd=plugin_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )

    def _start_session_teardown(
        self, session: Session, success_message: str, *, delay: float
    ) -> None:
        try:
            proc = self._launch_session_teardown(session, delay=delay)
        except OSError as exc:
            self.notify(f"Could not start session teardown: {exc}", severity="error")
            self.refresh_sessions()
            return

        # Rebuild from the durable status immediately. The child owns the
        # blocking wait/signals and remains alive if this Textual app exits.
        self.refresh_sessions()
        self.call_after_refresh(
            self._watch_session_teardown, proc, session, success_message
        )

    @work(thread=True, group="session_kill")
    def _watch_session_teardown(
        self,
        proc: subprocess.Popen[bytes],
        session: Session,
        success_message: str,
    ) -> None:
        error: Optional[str] = None
        if proc.wait() != 0:
            try:
                import json

                payload = json.loads(
                    (session.dir / "ending_error.json").read_text()
                )
                error = str(payload.get("error") or "session teardown failed")
            except (OSError, ValueError, AttributeError):
                error = "session teardown failed"
        self.call_from_thread(
            self._finish_session_kill, success_message, error
        )

    def _finish_session_kill(
        self, success_message: str, error: Optional[str]
    ) -> None:
        if error is not None:
            self.notify(f"Could not stop runtime: {error}", severity="error")
        else:
            self.notify(success_message)
        self.refresh_sessions()

    def action_compact(self) -> None:
        e = self._current_entry()
        if e is None:
            return
        if e.is_ghost:
            self.notify("Cannot compact a folded ghost row", severity="warning")
            return
        if e.runtime != RUNTIME_CLAUDE:
            self.notify(
                f"Multiverse compaction is Claude-only; {e.runtime} uses its native compaction",
                severity="warning",
            )
            return
        if not e.claude_uuid:
            self.notify(
                "No claude UUID tracked for this row — cannot compact",
                severity="warning",
            )
            return
        # A compaction of this row detached with `d` is still running —
        # re-open it. Checked before the status guard, since such a row is
        # by definition `compacting` and would be refused below.
        if self._reattach_compaction(e.claude_uuid):
            return

        if e.status not in _COMPACTABLE_STATUSES:
            self.notify(
                f"Compaction only allowed for idle/ended/retired/dormant sessions "
                f"(this is {e.status})",
                severity="warning",
            )
            return

        uuid = e.claude_uuid
        self.push_screen(
            CompactScreen(
                claude_uuid=uuid,
                cwd=e.cwd,
                display_name=e.name,
                mv_session=e.session,
            ),
            lambda _=None: self._compact_closed(uuid),
        )

    def _reattach_compaction(self, claude_uuid: str) -> bool:
        """Re-open the CompactScreen detached for ``claude_uuid``, if any.

        Returns True if one existed and was pushed. The instance was kept
        alive by ``install_screen`` while off-stack, so its log, progress
        bar and runner come back exactly as they were.
        """
        if claude_uuid not in self._detached_compactions:
            return False
        try:
            self.push_screen(
                _compact_screen_name(claude_uuid),
                lambda _=None: self._compact_closed(claude_uuid),
            )
        except Exception:  # noqa: BLE001
            # Registration outlived the installed screen somehow — drop it
            # rather than wedge the row into permanently refusing.
            self._detached_compactions.pop(claude_uuid, None)
            return False
        return True

    def _compact_closed(self, claude_uuid: str) -> None:
        """A CompactScreen dismissed for good. Runs after the pop, which is
        the only point ``uninstall_screen`` accepts (it refuses a screen
        still on the stack)."""
        self._detached_compactions.pop(claude_uuid, None)
        try:
            self.uninstall_screen(_compact_screen_name(claude_uuid))
        except Exception:  # noqa: BLE001
            pass
        self.refresh_sessions()

    def action_restore(self) -> None:
        """Restore a lost session from an orphan full JSONL.

        Opens a picker of full JSONLs in this workspace whose live
        counterpart no longer exists (typically because claude code's
        ``cleanupPeriodDays`` reaped them; our mirror kept them). The
        user picks one and it goes into the standard compaction flow
        which will (build any missing trees and then) render a fresh
        live JSONL from the picked full according to the chosen
        budget.
        """
        # Collect currently-live titles in this workspace so we can
        # skip orphans with a duplicate name — the user is already
        # working under that name.
        live_titles: set[str] = set()
        for row in self.entries:
            if not row.is_ghost and row.name:
                live_titles.add(row.name)

        def _on_pick(result: Optional[tuple[str, str]]) -> None:
            if result is None:
                return
            uuid, cwd = result

            # Guard: if we get back a uuid whose live now magically
            # exists (raced with an active session), don't clobber.
            live_path = live_jsonl_path(uuid, cwd)
            if live_path.is_file():
                self.notify(
                    f"Live JSONL now exists at {live_path.name}; "
                    f"refusing to overwrite. Refresh and try again.",
                    severity="warning",
                )
                return

            # Read the title from the full so the CompactScreen
            # header shows a meaningful name.
            title = _read_title_from_full(uuid) or uuid[:8]

            def _on_close(_: None) -> None:
                self.refresh_sessions()

            self.push_screen(
                CompactScreen(
                    claude_uuid=uuid,
                    cwd=cwd,
                    display_name=f"{title} (restore)",
                    # No mv_session — this uuid doesn't have an active
                    # multiverse wrapper. The runner's kill step is a
                    # no-op when kill_session_fn is None.
                    mv_session=None,
                ),
                _on_close,
            )

        self.push_screen(RestoreScreen(cwd=self.monitor_cwd, live_titles=live_titles), _on_pick)

    def action_toggle_auto_build(self) -> None:
        """Flip the per-session auto tree-build gate for the highlighted
        row. With auto-build on, every UserPromptSubmit on that session
        spawns a detached worker that builds compaction trees for the
        previous turn. Off by default — opt-in per session because each
        build is a privileged claude --resume call.
        """
        e = self._current_entry()
        if e is None:
            return
        if e.is_ghost:
            self.notify(
                "Cannot toggle auto-build on a folded ghost row",
                severity="warning",
            )
            return
        if e.status == "retired":
            self.notify(
                "Retired sessions are frozen; run compaction explicitly with c",
                severity="warning",
            )
            return
        if e.runtime != RUNTIME_CLAUDE:
            self.notify("Auto tree-build is Claude-only", severity="warning")
            return
        if not e.claude_uuid:
            self.notify(
                "No claude UUID tracked for this row — cannot toggle",
                severity="warning",
            )
            return
        new_state = auto_build_toggle(e.claude_uuid)
        label = "ON" if new_state else "OFF"
        self.notify(
            f"Auto tree-build for [bold]{escape(e.name)}[/]: {label}",
        )
        self.refresh_sessions()

    def action_visualize(self) -> None:
        """Render the current live JSONL as a navigable HTML page.

        For each turn (chain-walked): show the cached tree with frontier
        highlighting matching the current live state, or a flat list of
        leaves if there's no cached tree. Same gate as Compact minus the
        idle/ended/dormant restriction — visualization is read-only and
        safe to run while the session is working / waiting.
        """
        e = self._current_entry()
        if e is None:
            return
        if e.is_ghost:
            self.notify("Cannot visualize a folded ghost row", severity="warning")
            return
        if e.runtime != RUNTIME_CLAUDE:
            self.notify("Compaction visualization is Claude-only", severity="warning")
            return
        if not e.claude_uuid:
            self.notify(
                "No claude UUID tracked for this row — cannot visualize",
                severity="warning",
            )
            return

        plugin_root = Path(
            os.environ.get("MULTIVERSE_PLUGIN_ROOT")
            or Path(__file__).resolve().parent.parent
        )
        script = plugin_root / "scripts" / "visualize_live.py"
        if not script.is_file():
            self.notify(
                f"visualize_live.py not found at {script}",
                severity="error",
            )
            return

        self._visualize_worker(e.claude_uuid, e.cwd, str(script))

    @work(thread=True, exclusive=True, group="visualize")
    def _visualize_worker(
        self, claude_uuid: str, cwd: str, script: str,
    ) -> None:
        try:
            proc = subprocess.run(
                [sys.executable, script, claude_uuid, cwd],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(
                self.notify, "Visualizer timed out (>120s)", severity="error",
            )
            return
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            self.app.call_from_thread(
                self.notify,
                "Visualizer failed: " + " | ".join(tail),
                severity="error",
            )
            return
        # Last line of stdout is the path the script printed.
        out_lines = [l for l in proc.stdout.splitlines() if l.strip()]
        if not out_lines:
            self.app.call_from_thread(
                self.notify, "Visualizer produced no output path",
                severity="warning",
            )
            return
        path = out_lines[-1].strip()
        # Try to open it via the platform handler. Falls back to a notify
        # with the path if no handler is available (e.g. inside a Docker
        # exec on a remote host).
        opener = None
        if sys.platform == "darwin":
            opener = ["open", path]
        elif sys.platform.startswith("linux"):
            opener = ["xdg-open", path]
        opened = False
        if opener is not None:
            try:
                subprocess.Popen(
                    opener, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                opened = True
            except (FileNotFoundError, OSError):
                opened = False
        if opened:
            self.app.call_from_thread(
                self.notify, f"Opened {path}",
            )
        else:
            self.app.call_from_thread(
                self.notify,
                f"Wrote {path} (couldn't auto-open; copy it out to view)",
            )

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


def _fmt_eta(seconds: float) -> str:
    """Format an ETA in seconds as HH:MM:SS / Mm Ss / Ss, whichever fits."""
    if seconds < 0:
        return "?"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


def main() -> None:
    MonitorApp(claude_args=sys.argv[1:]).run()


if __name__ == "__main__":
    main()
