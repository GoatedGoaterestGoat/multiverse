"""Multiverse monitor — Textual TUI for orchestrating multiple claude sessions.

Each claude process runs inside a detached tmux session. The monitor tracks
them via per-session files under ``~/.multiverse/sessions/<id>/``:

  meta.json    — cwd, created_at, claude_args (written on spawn)
  status.json  — {"status": ..., "ts": ...} (written by hook scripts)
  control.json — existing context-fold signaling channel
  claude_uuid  — current claude session UUID (written by SessionStart hook)

The dashboard also surfaces *dormant* claude sessions: claude JSONLs with a
user-assigned ``custom-title`` that aren't currently wrapped by a multiverse
session. These can be resumed directly with Enter.

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

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    ProgressBar,
    RichLog,
    Static,
)

from .ask_agents import AskResult, AskTarget, ask_all
from .auto_build import is_enabled as auto_build_enabled, toggle as auto_build_toggle
from .compact_runner import CompactRunner, State as CompactState
from .full_jsonl import (
    full_jsonl_path,
    live_jsonl_path,
    total_token_count,
    total_turn_count,
)
from .paths import multiverse_dir
from .inbox import pending_inbox_count
from .names import pick_random_name
from .sessions import (
    STATE_DIR,
    TMUX_PREFIX,
    DashboardRow,
    Session,
    build_dashboard_rows,
    create_session,
    existing_names_in_cwd,
    kill_session,
    list_claude_sessions_in,
    list_sessions,
    strip_fold_suffix,
    tmux_session_exists,
    wrapped_claude_uuids,
)
from .spawn_requests import (
    list_pending_requests,
    read_resolution,
    request_path,
    write_resolution,
)


STATUS_STYLES = {
    "idle": "dim",
    "working": "bold yellow",
    "waiting": "bold red",
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


class NewSessionScreen(ModalScreen[Optional[str]]):
    """Prompt for a session name for a new session in the monitor's cwd."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, cwd: str) -> None:
        super().__init__()
        self.cwd = cwd
        suggestion = pick_random_name(existing_names_in_cwd(cwd))
        self.default_name = suggestion or ""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("New session"),
            Static(f"[dim]cwd: {self.cwd}[/]"),
            Static("Session name:"),
            Input(value=self.default_name, id="new-name"),
            Static("", id="new-error"),
            Static("Esc to go back", id="new-hint"),
            id="new-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#new-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_submit()

    def _try_submit(self) -> None:
        name = self.query_one("#new-name", Input).value.strip()
        err = self.query_one("#new-error", Static)
        if not name:
            err.update("[red]Name cannot be empty.[/]")
            return
        if name in existing_names_in_cwd(self.cwd):
            err.update(
                f"[red]'{name}' is already used in this workspace. Choose another.[/]"
            )
            return
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SpawnRequestScreen(ModalScreen[Optional[str]]):
    """Naming widget for an agent-initiated spawn request.

    Returns the chosen name on accept, or ``None`` on Esc. The monitor's
    watcher may also dismiss this externally (if another monitor resolved
    the request first) — in that case the callback still sees ``None`` and
    ``write_resolution`` no-ops via O_EXCL.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    _PREVIEW_CHARS = 280

    def __init__(self, cwd: str, request: dict) -> None:
        super().__init__()
        self.cwd = cwd
        self.request = request
        seeded = (request.get("default_name") or "").strip()
        if not seeded or seeded in existing_names_in_cwd(cwd):
            seeded = pick_random_name(existing_names_in_cwd(cwd)) or ""
        self.default_name = seeded

    def compose(self) -> ComposeResult:
        from_name = self.request.get("from", "?")
        instruction = self.request.get("instruction", "") or ""
        preview = instruction[: self._PREVIEW_CHARS]
        if len(instruction) > self._PREVIEW_CHARS:
            preview = preview.rstrip() + "…"

        yield Vertical(
            Static(
                f"[bold]'{from_name}'[/] wants to spawn a subagent:",
                id="spawn-header",
            ),
            Static(f"[dim italic]{preview or '(empty instruction)'}[/]", id="spawn-preview"),
            Static("Name the new agent:", id="spawn-prompt"),
            Input(value=self.default_name, id="spawn-name"),
            Static("", id="spawn-error"),
            Static("Enter to accept · Esc to reject", id="spawn-hint"),
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
        name = event.value.strip()
        err = self.query_one("#spawn-error", Static)
        if not name:
            err.update("[red]Name cannot be empty.[/]")
            return
        if name in existing_names_in_cwd(self.cwd):
            err.update(
                f"[red]'{name}' is already used in this workspace.[/]"
            )
            return
        self.dismiss(name)

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
        if name in existing_names_in_cwd(self.cwd):
            err.update(
                f"[red]'{name}' is already used in this workspace. Choose another.[/]"
            )
            return
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ResumeSessionScreen(ModalScreen[Optional[tuple[str, Optional[str]]]]):
    """Browse claude sessions in the monitor's cwd and pick one to resume.

    Hides sessions already wrapped by a live or ended multiverse session —
    those are surfaced on the dashboard itself.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, cwd: str, hide_uuids: set[str]) -> None:
        super().__init__()
        self.cwd = cwd
        self.hide_uuids = hide_uuids
        self.sessions_info: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Resume claude session — pick one:"),
            Static(f"[dim]cwd: {self.cwd}[/]"),
            DataTable(cursor_type="row", zebra_stripes=True, id="resume-table"),
            Static("Enter on a row to resume · Esc to go back", id="resume-hint"),
            id="resume-dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one("#resume-table", DataTable)
        table.add_columns("UUID", "Name", "Preview", "Updated")
        self._populate()
        table.focus()
        if self.sessions_info:
            try:
                table.move_cursor(row=0)
            except Exception:
                pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value if event.row_key is not None else None
        if not key:
            return
        claude_name: Optional[str] = None
        for info in self.sessions_info:
            if info["uuid"] == key:
                claude_name = info.get("name")
                break
        self.dismiss((str(key), claude_name))

    def _populate(self) -> None:
        all_sessions = list_claude_sessions_in(self.cwd)
        self.sessions_info = [
            info for info in all_sessions if info["uuid"] not in self.hide_uuids
        ]
        table = self.query_one("#resume-table", DataTable)
        table.clear()
        now = time.time()
        for info in self.sessions_info:
            preview = info["preview"] or "(empty)"
            claude_name = info.get("name")
            name_cell = claude_name if claude_name else "[dim](unnamed)[/]"
            table.add_row(
                info["uuid"][:8],
                name_cell,
                preview,
                _ago(now - info["mtime"]),
                key=info["uuid"],
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
        self.budget = 0
        self.phase = "input"  # input | running | done | failed
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
            Input(
                placeholder="Token budget (e.g. 32000)",
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
                "[dim]Enter to start once stats load · Esc to cancel[/]",
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
        """Compute total tokens, total turns, cached-tree count (trees
        whose turn_id is on the chain forest), and historical per-turn
        build times from worker.log for ETA seeding."""
        try:
            toks = total_token_count(self.claude_uuid, self.cwd)
            turns = total_turn_count(self.claude_uuid, self.cwd)
            cached, history = self._audit_cache_against_chain()
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._stats_failed, repr(e))
            return
        self.app.call_from_thread(
            self._stats_ready, toks, turns, cached, history
        )

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
            from compaction.parser import parse_session  # noqa: E402
            try:
                forest = parse_session(str(full))
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
        cached: int = 0,
        history: list[float] | None = None,
    ) -> None:
        self.total_tokens = total_tokens
        self.total_turns = total_turns
        self._last_built = cached
        self._last_total = total_turns
        if history:
            self._build_times = list(history)
        suggested = max(1, total_tokens // 2)
        self.query_one("#compact-stats", Static).update(
            f"Total: [bold]{total_tokens:,}[/] tokens · "
            f"[bold]{total_turns}[/] turn(s) (uncompacted)"
        )
        self._refresh_cached_line()
        budget = self.query_one("#compact-budget", Input)
        budget.disabled = False
        budget.placeholder = f"Token budget (e.g. {suggested:,} for ~50%)"
        budget.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.phase != "input":
            return
        raw = event.value.replace(",", "").replace("_", "").strip()
        try:
            b = int(raw)
        except ValueError:
            self.notify("Enter an integer token budget", severity="warning")
            return
        if b <= 0:
            self.notify("Budget must be positive", severity="warning")
            return
        if self.total_tokens == 0:
            self.notify("Stats still loading — wait a moment", severity="warning")
            return
        self.budget = b
        self._enter_running_phase()

    # ----- Phase 2: running ------------------------------------------------

    def _enter_running_phase(self) -> None:
        self.phase = "running"

        budget = self.query_one("#compact-budget", Input)
        budget.disabled = True
        budget.display = False

        stats = self.query_one("#compact-stats", Static)
        stats.update(
            f"Total: [bold]{self.total_tokens:,}[/]t  →  "
            f"Budget: [bold]{self.budget:,}[/]t  ·  "
            f"[bold]{self.total_turns}[/] turn(s)"
        )

        for wid in ("compact-state", "compact-progress", "compact-cached", "compact-log"):
            self.query_one(f"#{wid}").display = True

        self.query_one("#compact-state", Static).update(
            "[cyan]Starting compaction…[/]"
        )
        progress = self.query_one("#compact-progress", ProgressBar)
        progress.update(total=max(1, self.total_turns), progress=0)
        log_widget = self.query_one("#compact-log", RichLog)
        log_widget.clear()
        self.query_one("#compact-hint", Static).update(
            "[dim]Esc to abort (won't kill in-flight build subprocess)[/]"
        )

        self._start_runner()

    def _start_runner(self) -> None:
        live = live_jsonl_path(self.claude_uuid, self.cwd)
        if self.mv_session is not None:
            kill_fn = self._make_kill_fn(self.mv_session)
        else:
            kill_fn = None
        self._runner = CompactRunner(
            claude_uuid=self.claude_uuid,
            cwd=self.cwd,
            live_jsonl=live,
            total_budget=self.budget,
            kill_session_fn=kill_fn,
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
        self.phase = "done" if ok else "failed"
        self._append_log("─" * 56)
        self._append_log(("[OK] " if ok else "[FAIL] ") + msg)
        verdict_color = "green" if ok else "red"
        self.query_one("#compact-hint", Static).update(
            f"[bold {verdict_color}]"
            + ("Trees built. " if ok else "Compaction failed. ")
            + "Esc to close.[/]"
        )

    # ----- Cancel / dismiss -----------------------------------------------

    def action_cancel(self) -> None:
        if self.phase == "running" and self._runner is not None:
            # First Esc: signal abort (clean stop at next checkpoint).
            if not self._runner.cancelled:
                self._runner.cancel()
                self._append_log("[user] cancel requested — will abort soon")
                return
            # Second Esc while still running: force-dismiss.
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
    """

    BINDINGS = [
        Binding("n", "new_session", "New"),
        Binding("R", "resume_session", "Resume"),
        Binding("c", "compact", "Compact"),
        Binding("v", "visualize", "Visualize"),
        Binding("b", "toggle_auto_build", "Build on/off"),
        Binding("a", "ask_all", "Ask all"),
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
        table.add_columns("ID", "Name", "Inbox", "Build", "Status", "Updated", "Preview")
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
        for s in self.sessions:
            if s.status not in ("ended",) and not tmux_session_exists(s.tmux_name):
                s.status = "ended"
        with self._fork_lock:
            hide = set(self._active_fork_uuids)
        self.entries = build_dashboard_rows(
            self.sessions,
            cwd=self.monitor_cwd,
            hide_uuids=hide,
        )
        self._redraw_table()
        self._tick_spawn_requests()

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
            if read_resolution(self._active_spawn_id) is not None:
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
                # cleaned up by the tool (timeout/rejection path) during the
                # interval between enqueue and push. Drop silently.
                if read_resolution(rid) is not None:
                    continue
                if not request_path(rid).is_file():
                    continue
                timeout = int(req.get("timeout_seconds") or 300)
                ts = float(req.get("ts") or 0)
                if time.time() - ts > timeout:
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

        def on_close(chosen_name: Optional[str]) -> None:
            self._active_spawn_id = None
            self._active_spawn_modal = None
            if chosen_name:
                won = write_resolution(rid, status="accepted", name=chosen_name)
                if won:
                    self.notify(
                        f"Approved spawn from '{req.get('from', '?')}' "
                        f"as '{chosen_name}'"
                    )
                else:
                    # Another monitor beat us to the resolution. Show what
                    # actually won, if anything meaningful.
                    actual = read_resolution(rid)
                    if actual and actual.get("status") == "accepted":
                        won_name = actual.get("name") or "?"
                        self.notify(
                            f"Another monitor already accepted this "
                            f"spawn as '{won_name}'",
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
                uuid = e.claude_uuid or ""
                id_cell = f"[dim]{uuid[:8]}[/]"
            pending = pending_inbox_count(e.cwd, e.name)
            inbox_cell = f"[bold yellow]📩 {pending}[/]" if pending > 0 else "[dim]·[/]"
            if e.claude_uuid and auto_build_enabled(e.claude_uuid):
                build_cell = "[bold green]on[/]"
            else:
                build_cell = "[dim]off[/]"
            preview = e.preview or "[dim](empty)[/]"
            if len(preview) > 60:
                preview = preview[:59] + "…"
            table.add_row(
                id_cell, e.name, inbox_cell, build_cell, status_cell, updated, preview,
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
        def cb(name: Optional[str]) -> None:
            if not name:
                return
            try:
                create_session(self.monitor_cwd, self.claude_args, name=name)
            except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
                self.notify(f"Spawn failed: {e}", severity="error")
                return
            self.refresh_sessions()
            self.notify(f"Spawned {name}")

        self.push_screen(NewSessionScreen(self.monitor_cwd), cb)

    def action_resume_session(self) -> None:
        hide = wrapped_claude_uuids(self.sessions)
        with self._fork_lock:
            hide = hide | self._active_fork_uuids

        def on_picked(result: Optional[tuple[str, Optional[str]]]) -> None:
            if not result:
                return
            uuid, claude_name = result

            existing = existing_names_in_cwd(self.monitor_cwd)
            if claude_name and claude_name not in existing:
                self._spawn_resumed(self.monitor_cwd, uuid, claude_name)
                return

            if claude_name:
                reason = (
                    f"'{claude_name}' is already used in this workspace. "
                    "Pick a different name for this session:"
                )
                default_name = claude_name
            else:
                reason = (
                    "This claude session has no name yet. "
                    "Assign a name for the multiverse session:"
                )
                default_name = pick_random_name(existing) or ""

            def on_name(name: Optional[str]) -> None:
                if not name:
                    return
                self._spawn_resumed(self.monitor_cwd, uuid, name)

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

    def _spawn_resumed(self, cwd: str, uuid: str, name: str) -> None:
        # Strip ghost-row display suffixes — passing the literal "step2b
        # (folded 02:33)" as `--name` would write that into the new
        # session's custom-title and the dashboard would re-suffix it on
        # the next refresh, accumulating ad infinitum.
        name = strip_fold_suffix(name) or name
        try:
            create_session(cwd, ["--resume", uuid], resumed_from=uuid, name=name)
        except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
            self.notify(f"Resume failed: {e}", severity="error")
            return
        self.refresh_sessions()
        self.notify(f"Resumed {uuid[:8]} as {name}")

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

        # Dormant or ended — resume the underlying claude session.
        uuid = e.claude_uuid
        if not uuid:
            self.notify(
                "No claude UUID tracked for this row — cannot resume", severity="warning"
            )
            return

        # Ended multiverse row: free the name by reaping its meta dir before
        # spawning the replacement (otherwise existing_names_in_cwd blocks it).
        if e.session is not None and e.status == "ended":
            kill_session(e.session)

        self._spawn_resumed(e.cwd, uuid, e.name)

    def _register_fork(self, fork_uuid: str, active: bool) -> None:
        with self._fork_lock:
            if active:
                self._active_fork_uuids.add(fork_uuid)
            else:
                self._active_fork_uuids.discard(fork_uuid)

    def action_ask_all(self) -> None:
        targets: list[AskTarget] = []
        for e in self.entries:
            if e.claude_uuid:
                targets.append(
                    AskTarget(name=e.name, cwd=e.cwd, claude_uuid=e.claude_uuid)
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
            self.notify(
                "Dormant sessions cannot be killed from here — delete the JSONL manually",
                severity="warning",
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
