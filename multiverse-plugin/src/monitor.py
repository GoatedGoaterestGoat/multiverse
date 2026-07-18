"""Multiverse monitor — Textual TUI for orchestrating runtime sessions.

Each runtime process runs inside a detached tmux session. The monitor tracks
them via per-session files under ``~/.multiverse/sessions/<id>/``:

  meta.json    — cwd, runtime, created_at, runtime args (written on spawn)
  status.json  — {"status": ..., "ts": ...} (written by hook scripts)
  claude_uuid / runtime_session_id — native runtime session identifier

The dashboard also surfaces dormant, named Claude and Codex sessions that are
not currently wrapped by a Multiverse session. These can be resumed directly
with Enter.

Attach = ``tmux attach-session`` run while the Textual app is suspended.
Detach with normal ``Ctrl-b d`` and you land back in the monitor.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

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

from .ask_agents import AskResult, AskTarget, ask_all
from .auto_build import is_enabled as auto_build_enabled, toggle as auto_build_toggle
from .compact_runner import CompactRunner, State as CompactState
from .full_jsonl import (
    full_jsonl_path,
    list_orphan_fulls,
    live_jsonl_path,
    live_stats,
    live_stats_cached,
    minimum_compacted_tokens,
    structural_tokens,
    structural_tokens_cached,
    total_token_count,
    total_turn_count,
)
from .paths import multiverse_dir
from .inbox import list_pending_messages, pending_inbox_count
from .names import pick_random_name
from .sessions import (
    RUNTIME_CLAUDE,
    RUNTIME_CODEX,
    STATE_DIR,
    TMUX_PREFIX,
    DashboardRow,
    Session,
    all_session_names_in_cwd,
    build_dashboard_rows,
    create_session,
    current_host,
    delete_codex_thread,
    existing_names_in_cwd,
    foreign_host_still_reported_live,
    kill_session,
    list_claude_sessions_in,
    list_codex_sessions_in,
    list_sessions,
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
from . import web_share
from . import sb_control


STATUS_STYLES = {
    "idle": "dim",
    "working": "bold yellow",
    "waiting": "bold red",
    "listening": "bold cyan",
    "starting": "cyan",
    "ended": "dim strike",
    "dormant": "blue",
    "compacting": "bold magenta",
    "unknown": "dim",
}

# Statuses where the user is allowed to launch a compaction. Working/
# waiting/starting/compacting are excluded — the session is busy or
# already locked.
_COMPACTABLE_STATUSES = {"idle", "ended", "dormant"}


class _SpawnAttachGuard:
    """Background watcher while the user is attached to a tmux session.

    Polls ``~/.multiverse/spawn_requests/`` every second for cwd-matching
    pending requests. When one appears, runs ``tmux detach-client -s
    <session>`` to force the user back into the monitor so they can see
    the naming modal (Textual's event loop is suspended during attach, so
    the normal tick can't fire).

    ``forced_detach`` is set to True when we issue the detach so the
    caller can schedule a reattach once the queue has fully drained.
    """

    _POLL_INTERVAL = 1.0

    def __init__(self, cwd: str, tmux_session: str) -> None:
        self.cwd = cwd
        self.tmux_session = tmux_session
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.forced_detach = False

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
                pending = list_pending_requests(self.cwd)
            except Exception:
                pending = []
            if pending:
                try:
                    subprocess.run(
                        ["tmux", "detach-client", "-s", self.tmux_session],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                    self.forced_detach = True
                except (OSError, subprocess.SubprocessError):
                    pass
                return  # one detach is all we need
            if self._stop.wait(self._POLL_INTERVAL):
                return


class NewSessionScreen(ModalScreen[Optional[tuple[str, str]]]):
    """Prompt for a session name for a new session in the monitor's cwd."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Create", show=False, priority=True),
    ]

    def __init__(self, cwd: str) -> None:
        super().__init__()
        self.cwd = cwd
        suggestion = pick_random_name(all_session_names_in_cwd(cwd))
        self.default_name = suggestion or ""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("New session"),
            Static(f"[dim]cwd: {self.cwd}[/]"),
            Static("Session name:"),
            Input(value=self.default_name, id="new-name"),
            Static("Runtime:"),
            RadioSet(
                RadioButton("Claude", value=True, id="new-runtime-claude"),
                RadioButton("Codex", id="new-runtime-codex"),
                id="new-runtime",
            ),
            Static("", id="new-error"),
            Static(
                "Tab to choose runtime · Enter to create · Esc to go back",
                id="new-hint",
            ),
            id="new-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#new-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_submit()

    def action_submit(self) -> None:
        self._try_submit()

    def _try_submit(self) -> None:
        name = self.query_one("#new-name", Input).value.strip()
        err = self.query_one("#new-error", Static)
        if not name:
            err.update("[red]Name cannot be empty.[/]")
            return
        if name in all_session_names_in_cwd(self.cwd):
            err.update(
                f"[red]'{name}' is already used in this workspace. Choose another.[/]"
            )
            return
        runtime = (
            RUNTIME_CODEX
            if self.query_one("#new-runtime-codex", RadioButton).value
            else RUNTIME_CLAUDE
        )
        self.dismiss((name, runtime))

    def action_cancel(self) -> None:
        self.dismiss(None)


class SpawnRequestScreen(ModalScreen[Optional[tuple[str, str]]]):
    """Approval widget for an agent-initiated spawn request.

    Returns the chosen name and runtime on accept, or ``None`` on Esc. The
    monitor's watcher may also dismiss this externally (if another monitor
    resolved the request first) — in that case the callback still sees
    ``None`` and ``write_resolution`` no-ops via O_EXCL.
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
        self.caller_runtime = (
            RUNTIME_CODEX
            if request.get("caller_runtime") == RUNTIME_CODEX
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
                f"[bold]'{from_name}'[/] ({self.caller_runtime}) wants to "
                "spawn a subagent:",
                id="spawn-header",
            ),
            Static(f"[dim italic]{preview or '(empty instruction)'}[/]", id="spawn-preview"),
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
                id="spawn-runtime",
            ),
            Static("", id="spawn-error"),
            Static(
                "Tab to choose runtime · Enter to accept · Esc to reject",
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
        self._try_submit()

    def action_submit(self) -> None:
        self._try_submit()

    def _try_submit(self) -> None:
        name = self.query_one("#spawn-name", Input).value.strip()
        err = self.query_one("#spawn-error", Static)
        if not name:
            err.update("[red]Name cannot be empty.[/]")
            return
        if name in all_session_names_in_cwd(self.cwd):
            err.update(
                f"[red]'{name}' is already used in this workspace.[/]"
            )
            return
        runtime = (
            RUNTIME_CODEX
            if self.query_one("#spawn-runtime-codex", RadioButton).value
            else RUNTIME_CLAUDE
        )
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
                f"[red]'{name}' is already used in this workspace. Choose another.[/]"
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
                f"Delete dormant session [bold]'{self.display_name}'[/]?",
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
                f"Force-kill session [bold]'{self.display_name}'[/]?",
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
            Static(f"[bold]Q:[/] {self.question}", id="ask-results-question"),
        ]
        if not self.results:
            blocks.append(Static("[dim](no targets)[/]"))
        for r in self.results:
            header = f"[bold yellow]━━━ {r.name}[/] [dim]({r.elapsed:.1f}s)[/]"
            blocks.append(Static(header))
            if r.error:
                blocks.append(Static(f"[red]error:[/] {r.error}"))
            else:
                blocks.append(Static(r.response or "[dim](empty)[/]"))
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
        from rich.markup import escape

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

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "primary", "Enroll/Connect"),
        Binding("e", "reenroll", "Re-enroll"),
        Binding("d", "disconnect", "Disconnect"),
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
                Input(value=_socket.gethostname(), id="sb-name"),
                Static("[dim]Workspaces to expose (comma-separated abs paths):[/]"),
                Input(value=self.cwd, id="sb-workspaces"),
                Static("[dim]— Cloudflare Access service token (wss:// only; blank = keep/none) —[/]"),
                Input(value=sb_control.get_cf_client_id(), placeholder="CF-Access-Client-Id",
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
        self.query_one("#sb-url", Input).focus()

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
            here = "[green]✓ this workspace exposed[/]" if self.cwd in granted else (
                "[yellow]this workspace not yet granted[/]")
            lines.append(
                f"Device: [bold]{st.get('device_id', '?')}[/] · "
                f"{len(granted)} workspace(s) granted · {here}"
            )
            lines.append(
                f"CF token: {'[green]stored[/]' if st.get('has_cf_creds') else '[dim]none[/]'}"
            )
        else:
            lines.append("[dim]Not enrolled yet — fill the form and press Enter to enroll.[/]")
        self.query_one("#sb-status", Static).update("\n".join(lines))

        running = st.get("running")
        if running:
            hint = "[b]d[/] disconnect · [b]e[/] re-enroll · [b]Esc[/] close (daemon keeps running)"
        elif st.get("enrolled"):
            hint = "[b]Enter[/] connect · [b]e[/] re-enroll · [b]Esc[/] close"
        else:
            hint = "[b]Enter[/] enroll & connect · [b]Esc[/] cancel"
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
        if st.get("running"):
            self.notify("Already connected — press d to disconnect.")
            return
        if st.get("enrolled"):
            self._start_worker("connect")
        else:
            self._start_worker("enroll")

    def action_reenroll(self) -> None:
        if not self._busy:
            self._start_worker("enroll")

    def action_disconnect(self) -> None:
        if not self._busy:
            self._start_worker("disconnect")

    def _start_worker(self, op: str) -> None:
        url = self._val("url")
        if op in ("connect", "enroll") and not url:
            self._set_error("Endpoint URL is required.")
            return
        cid, csecret, cf_err = self._cf_from_fields()
        if cf_err:
            self._set_error(cf_err)
            return
        code = fingerprint = name = ""
        workspaces: list[str] = []
        if op == "enroll":
            code, fingerprint = self._val("code"), self._val("fingerprint")
            if not code or not fingerprint:
                self._set_error("Pairing code and fingerprint are required to enroll.")
                return
            name = self._val("name") or ""
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
                sb_control.enroll(
                    base_url=url, code=code, fingerprint=fingerprint,
                    name=name, want=workspaces,
                )
                sb_control.start(url)
            elif op == "connect":
                sb_control.start(url)
            elif op == "disconnect":
                sb_control.stop()
        except Exception as exc:  # surface enroll/connect failures to the panel
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

        yield Vertical(
            Static(
                f"Compact: [bold]{self.display_name}[/]\n"
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

    The actual *expansion* step (rendering the compacted live JSONL from
    the per-turn budget) is intentionally not yet wired in — first version
    stops once every turn has a tree. The next iteration will plug in
    the proportional scheduler + ``compact_forest_to_session_jsonl`` and
    rewrite the live JSONL.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

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
        # Per-turn build elapsed times collected from worker.log; powers
        # the ETA estimate. Bounded — only the most-recent N are used so
        # ETA tracks current rate, not a stale long-tail.
        self._build_times: list[float] = []
        self._last_built: int = 0
        self._last_total: int = 0

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                f"Compact session: [bold]{self.display_name}[/]",
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
            "[dim]Esc to abort (won't kill in-flight build subprocess)[/]"
        )

        # Ensure forest_sizes is populated even if the initial stats
        # load raced or missed something (e.g. some turns just built).
        if not self._forest_sizes:
            self._forest_sizes = self._per_turn_full_tokens()

        self._start_summarize_runner()

    def _start_summarize_runner(self) -> None:
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
            self.query_one("#compact-log", RichLog).write(line)
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
            # Rebuild the richer per-turn stats now that trees are
            # freshly built — root.tokens for each turn is up-to-date.
            # Live stats come along so the picker's header keeps the
            # full / live context visible instead of dropping it when
            # we transition from the input phase.
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
    """

    BINDINGS = [
        Binding("n", "new_session", "New"),
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
        # When the attach sidecar forcibly detaches the user to deal with a
        # spawn request, we remember the tmux session here so we can
        # reattach once the queue fully drains (set by ``_attach_to``;
        # consumed by ``_tick_spawn_requests``).
        self._pending_reattach: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        table = DataTable(cursor_type="row", zebra_stripes=True)
        table.add_columns(
            "ID", "Name", "Runtime", "Inbox", "Build", "Status", "Tokens",
            "Updated", "Preview",
        )
        yield table
        yield Footer()

    def on_mount(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.title = "Multiverse Monitor"
        self.sub_title = f"cwd: {self.monitor_cwd}"
        self.refresh_sessions()
        self.set_interval(1.0, self.refresh_sessions)

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
        self._redraw_table()
        self._tick_spawn_requests()
        self._update_web_indicator()

    def _update_web_indicator(self) -> None:
        """Reflect web-share state in the header subtitle. ``status`` also
        reaps a share whose processes have died, so this doubles as cleanup.
        """
        try:
            share = web_share.status(self.monitor_cwd)
        except Exception:
            share = None
        tag = "  ·  🌐 web: ON" if share is not None else ""
        self.sub_title = f"cwd: {self.monitor_cwd}{tag}"

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
            # Don't stomp on a modal the user opened explicitly (New,
            # Resume, Ask, etc). Wait for the next tick.
            if isinstance(self.screen, ModalScreen):
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
            and not isinstance(self.screen, ModalScreen)
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

        def on_close(selection: Optional[tuple[str, str]]) -> None:
            self._active_spawn_id = None
            self._active_spawn_modal = None
            if selection:
                chosen_name, chosen_runtime = selection
                won = write_resolution(
                    rid,
                    status="accepted",
                    name=chosen_name,
                    runtime=chosen_runtime,
                )
                if won:
                    self.notify(
                        f"Approved spawn from '{req.get('from', '?')}' "
                        f"as '{chosen_name}' ({chosen_runtime})"
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
            else:
                # Could be user Esc OR external dismiss. O_EXCL makes the
                # second case a no-op.
                write_resolution(rid, status="rejected", reason="cancelled")

        modal = SpawnRequestScreen(self.monitor_cwd, req)
        self._active_spawn_modal = modal
        self.push_screen(modal, on_close)

    def _redraw_table(self) -> None:
        table = self.query_one(DataTable)
        prev_cursor = table.cursor_row
        table.clear()
        now = time.time()
        for e in self.entries:
            style = STATUS_STYLES.get(e.status, "dim")
            status_cell = f"[{style}]{e.status}[/]"
            updated = _ago(now - e.status_ts) if e.status_ts else "-"
            if e.session is not None:
                id_cell = e.session.id
            else:
                session_id = e.session_id or ""
                id_cell = f"[dim]{session_id[:8]}[/]"
            pending = pending_inbox_count(e.cwd, e.name)
            inbox_cell = f"[bold yellow]📩 {pending}[/]" if pending > 0 else "[dim]·[/]"
            if e.claude_uuid and auto_build_enabled(e.claude_uuid):
                build_cell = "[bold green]on[/]"
            else:
                build_cell = "[dim]off[/]"
            if e.claude_uuid:
                # Displayed = transcript tokens + structural (derived
                # from the last recorded API call's usage). Matches
                # claude's own /clear / /context total to a few tokens.
                # Falls back to just the transcript number when
                # structural isn't measurable yet (freshly-compacted
                # session before its first API call).
                live_toks, _ = live_stats_cached(e.claude_uuid, e.cwd)
                struct_toks = structural_tokens_cached(e.claude_uuid, e.cwd)
                tokens_cell = f"{live_toks + struct_toks:,}"
            else:
                tokens_cell = "[dim]-[/]"
            preview = e.preview or "[dim](empty)[/]"
            if len(preview) > 60:
                preview = preview[:59] + "…"
            table.add_row(
                id_cell, e.name, e.runtime, inbox_cell, build_cell, status_cell,
                tokens_cell, updated, preview,
                key=e.row_key,
            )
        if self.entries:
            row = max(0, min(prev_cursor or 0, len(self.entries) - 1))
            try:
                table.move_cursor(row=row)
            except Exception:
                pass

    def _current_entry(self) -> Optional[DashboardRow]:
        if not self.entries:
            return None
        table = self.query_one(DataTable)
        idx = table.cursor_row or 0
        if 0 <= idx < len(self.entries):
            return self.entries[idx]
        return None

    def action_new_session(self) -> None:
        def cb(result: Optional[tuple[str, str]]) -> None:
            if not result:
                return
            name, runtime = result
            runtime_args = self.claude_args if runtime == RUNTIME_CLAUDE else []
            try:
                create_session(
                    self.monitor_cwd,
                    runtime_args,
                    name=name,
                    runtime=runtime,
                )
            except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
                self.notify(f"Spawn failed: {e}", severity="error")
                return
            self.refresh_sessions()
            self.notify(f"Spawned {name} with {runtime}")

        self.push_screen(NewSessionScreen(self.monitor_cwd), cb)

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
        guard = _SpawnAttachGuard(cwd=self.monitor_cwd, tmux_session=tmux_session)
        guard.start()
        try:
            with self.suspend():
                subprocess.run(["tmux", "attach-session", "-t", tmux_session])
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
        else:
            runtime_args = ["resume", session_id]
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
        self.notify(f"Resumed {runtime} {session_id[:8]} as {name}")

    def action_primary(self) -> None:
        """Enter: attach for live sessions, resume for dormant/ended ones."""
        e = self._current_entry()
        if e is None:
            return

        if e.status == "compacting":
            self.notify(
                "Session is compacting — wait until it finishes",
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
            kill_session(e.session)

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
            self.notify(f"No pending messages for '{e.name}'")
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
        e = self._current_entry()
        if e is None:
            return
        if e.status == "compacting":
            self.notify(
                "Session is compacting — kill blocked until it finishes",
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
                self.notify(f"Deleted dormant session '{display_name}'")
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
                kill_session(session)
                self.notify(
                    f"Force-killed '{display_name}' "
                    f"(was on host {session.host})"
                )
                self.refresh_sessions()

            self.push_screen(
                ConfirmForceKillScreen(
                    display_name=display_name,
                    other_host=e.session.host or "(unknown)",
                    current=current_host(),
                ),
                _on_force_close,
            )
            return
        kill_session(e.session)
        self.notify(f"Killed {e.session.id}")
        self.refresh_sessions()

    def action_compact(self) -> None:
        e = self._current_entry()
        if e is None:
            return
        if e.is_ghost:
            self.notify("Cannot compact a folded ghost row", severity="warning")
            return
        if not e.claude_uuid:
            self.notify(
                "No claude UUID tracked for this row — cannot compact",
                severity="warning",
            )
            return
        if e.status not in _COMPACTABLE_STATUSES:
            self.notify(
                f"Compaction only allowed for idle/ended/dormant sessions "
                f"(this is {e.status})",
                severity="warning",
            )
            return

        def _on_close(_: None) -> None:
            self.refresh_sessions()

        self.push_screen(
            CompactScreen(
                claude_uuid=e.claude_uuid,
                cwd=e.cwd,
                display_name=e.name,
                mv_session=e.session,
            ),
            _on_close,
        )

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
        if not e.claude_uuid:
            self.notify(
                "No claude UUID tracked for this row — cannot toggle",
                severity="warning",
            )
            return
        new_state = auto_build_toggle(e.claude_uuid)
        label = "ON" if new_state else "OFF"
        self.notify(
            f"Auto tree-build for [bold]{e.name}[/]: {label}",
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
