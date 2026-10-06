"""A transient, read-only inspector; terminal capture never blocks the UI."""
import asyncio
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from ..display import clean
from .tree import within


class Inspector(Vertical):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.row = None
        self.identity = None
        self.generation = 0
        self.capturing = False
        self.requested = None
        self.follow_next = False

    def compose(self):
        yield Static("", id="inspect-name", markup=False)
        yield Static("", id="inspect-meta", markup=False)
        yield Static("", id="inspect-state", markup=False)
        yield Static("CURRENT WORK", id="inspect-label")
        yield Static("", id="inspect-work", markup=False)
        yield Static("", id="inspect-group", markup=False)
        with VerticalScroll(id="pane-scroll"):
            yield Static("", id="preview", markup=False)
        yield Static("", id="inspect-hint", markup=False)

    def show(self, row):
        self.app.add_class("inspecting")
        self.update_row(row)

    def close(self):
        self.generation += 1
        self.row = self.identity = None
        self.app.remove_class("inspecting")

    def update_row(self, row):
        if not row:
            self.close()
            return
        group = row["kind"] == "group"
        identity = (row["key"], row.get("live"), row.get("runtime", {}).get("launch_id"))
        changed = identity != self.identity
        if changed:
            self.generation += 1
            self.identity = identity
            self.follow_next = True
            self.query_one("#preview", Static).update("Reading terminal…")
            self.query_one("#pane-scroll").border_title = "LIVE PANE"
        self.row = dict(row)
        accent = row["accent"]
        self.query_one("#inspect-name", Static).update(Text(clean(row["name"]), style=f"bold {accent}"))
        self.query_one("#inspect-meta", Static).update(self.app.scope_trail(row))
        self.query_one("#inspect-hint", Static).update(
            "v full details · p / Esc close")
        self.query_one("#inspect-label", Static).update("PURPOSE" if group else "CURRENT WORK")
        self.query_one("#inspect-work", Static).update(clean(
            row.get("description", "No description yet.") if group else row.get("summary") or row.get("task") or "No work reported yet."))
        self.query_one("#inspect-group").display = group
        self.query_one("#pane-scroll").display = not group
        if group:
            state = f'{row["alive"]}/{row["total"]} live · {row["waiting"]} waiting · {row["blocked"]} blocked'
            members = [a for a in self.app.snapshot["agents"] if within(a, Path(row["path"]))]
            members.sort(key=lambda a: (a.get("status") != "waiting", a.get("phase") != "blocked"))
            work = Text("ACTIVE WORK\n", style="#7f849c")
            for agent in members[:4]:
                work.append("\n" + clean(agent["name"]) + "\n", style=accent)
                work.append(clean(agent.get("summary") or agent.get("task", "—")) + "\n", style="#a6adc8")
            if not members:
                work.append("\nNo agents yet. Select this group and press n.")
            self.query_one("#inspect-group", Static).update(work)
        else:
            state = Text(clean(row.get("harness", "unknown")), style="#a6adc8")
            if row.get("model"):
                state.append(" / " + clean(row["model"]))
            live = row.get("live", "unknown")
            state.append(f" · {live}", style="#a6e3a1" if live == "alive" else "#f9e2af")
            state.append("\nStatus: " + clean(row.get("status", "unknown")), style="#a6adc8")
            state.append(" · Reported: " + clean(row.get("phase", "unknown")), style="#a6adc8")
            state.append("\n" + clean(row.get("context_line", "Context: N/A")), style="#a6adc8")
            selection = row.get('runtime', {}).get('native_mode')
            if selection:
                state.append(f'\nMode: {clean(selection["mode"])} ({clean(selection["policy"])})', style='#a6adc8')
                if selection.get('fallback_reason'):
                    state.append('\nFallback: ' + clean(selection['fallback_reason']), style='#f9e2af')
            versions = row.get('runtime', {}).get('native_versions')
            relay = row.get('runtime', {}).get('native_relay') or {}
            if row.get('launch_error'):
                state.append('\n' + clean(row['launch_error'].splitlines()[0]), style='bold #f38ba8')
            if relay.get('state') == 'lost':
                state.append('\n' + clean(relay.get('reason', 'Native relay lost')), style='bold #f38ba8')
                state.append('\n' + clean(relay.get('detail', '')), style='#f9e2af')
            if versions:
                mismatch = versions['cli'] != versions['daemon']
                state.append(f'\nVersions: CLI {clean(versions["cli"])} / daemon {clean(versions["daemon"])}'
                             + (' (different)' if mismatch else ''), style='#f9e2af' if mismatch else '#a6adc8')
            if live != "alive":
                self.query_one("#pane-scroll").border_title = "TERMINAL · " + live.upper()
                message = ('Native work is ending.' if live == 'ending' else
                           'Native process stopped; tmux wrapper remains. Resume with r.' if live == 'stopped' else
                           'No tmux wrapper. Resume with r.' if live == 'dormant' else 'Terminal state is unknown.')
                if row.get('launch_error'):
                    self.query_one('#pane-scroll').border_title = 'SAVED LAUNCH OUTPUT'
                self.query_one("#preview", Static).update(Text('\n'.join(
                    clean(line) for line in (row.get('reason') or message).splitlines())))
        self.query_one("#inspect-state", Static).update(state)
        self.capture_if_needed()

    def capture_if_needed(self):
        row = self.row
        revision = (self.generation, self.app.scan_count)
        if (row and row["kind"] == "agent" and row.get("live") == "alive"
                and not self.capturing and revision != self.requested and self.app.ready()):
            self.requested = revision
            self.capturing = True
            self.capture(dict(row), self.generation)

    @work(group="preview", exit_on_error=False)
    async def capture(self, row, generation):
        error = False
        try:
            output = await asyncio.to_thread(self.app.backend.inspect, Path(row["universe"]), row["id"])
        except Exception as exc:
            output, error = f"Preview unavailable: {exc}", True
        finally:
            self.capturing = False
        # A delayed result may belong to another hover target or an old launch.
        if self.row and generation == self.generation:
            scroll = self.query_one("#pane-scroll", VerticalScroll)
            follow = self.follow_next or scroll.scroll_y >= scroll.max_scroll_y - 1
            self.follow_next = False
            lines = str(output).rstrip().splitlines()[-100:]
            text = "\n".join(clean(line) for line in lines) or "Terminal has no output yet."
            self.query_one("#preview", Static).update(Text(text, no_wrap=True, overflow="ellipsis"))
            stamp = "UNAVAILABLE" if error else datetime.now().strftime("%H:%M:%S")
            self.query_one("#pane-scroll").border_title = "LIVE PANE · " + stamp
            if follow:
                scroll.call_after_refresh(scroll.scroll_end, animate=False)
        elif self.row:
            self.capture_if_needed()
