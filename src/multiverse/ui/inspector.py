"""A transient, read-only inspector; terminal capture never blocks the UI."""
import asyncio
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from ..display import clean, live_label
from . import theme
from .plugin_service import in_daemon
from .tree import within


PLUGIN_FACTS_TIMEOUT = 5.0


class Inspector(Vertical):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.row = None
        self.identity = None
        self.generation = 0
        self.capturing = False
        self.requested = None
        self.follow_next = False
        self.reading_plugins = False
        self.plugin_revision = None

    def compose(self):
        yield Static("", id="inspect-name", markup=False)
        yield Static("", id="inspect-meta", markup=False)
        yield Static("", id="inspect-state", markup=False)
        yield Static("", id="inspect-plugins", markup=False)
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
        current = (row["key"], row.get("live"), row.get("runtime", {}).get("launch_id"))
        changed = current != self.identity
        if changed:
            self.generation += 1
            self.identity = current
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
            self.query_one("#inspect-group", Static).update(active_work(members, accent))
        else:
            state = agent_state(row)
            live = row.get("live", "unknown")
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
        if changed:
            self.plugin_revision = None
            self.show_plugin_lines([])
        self.plugin_facts_if_needed()
        self.capture_if_needed()

    def show_plugin_lines(self, lines):
        widget = self.query_one("#inspect-plugins", Static)
        widget.display = bool(lines)
        widget.update(Text("\n".join(f"{clean(plugin)}: {clean(text)}" for plugin, text in lines), style=theme.SUBTEXT0))

    def plugin_facts_if_needed(self):
        """Plugin lines for an agent, read off the event loop once per scan; core facts show at once."""
        row = self.row
        revision = (self.generation, self.app.scan_count)
        if row and row["kind"] == "agent" and not self.reading_plugins and revision != self.plugin_revision:
            self.plugin_revision = revision
            self.reading_plugins = True
            self.read_plugin_facts(dict(row), self.generation)

    @work(group="plugin-facts", exit_on_error=False)
    async def read_plugin_facts(self, row, generation):
        try:
            lines = await asyncio.wait_for(
                in_daemon(self.app.plugin_service.facts, Path(row["universe"]), row), PLUGIN_FACTS_TIMEOUT)
        except Exception:  # a hung or failing plugin never reaches the inspector
            lines = [("plugins", "unavailable")]
        finally:
            self.reading_plugins = False
        if self.row and generation == self.generation:
            self.show_plugin_lines(lines)
        elif self.row:
            self.plugin_facts_if_needed()

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


def active_work(members, accent):
    """A group's agents that need attention first: waiting, then blocked; at most four."""
    members = sorted(members, key=lambda a: (a.get("status") != "waiting", a.get("phase") != "blocked"))
    work = Text("ACTIVE WORK\n", style=theme.OVERLAY1)
    for agent in members[:4]:
        work.append("\n" + clean(agent["name"]) + "\n", style=accent)
        work.append(clean(agent.get("summary") or agent.get("task", "—")) + "\n", style=theme.SUBTEXT0)
    if not members:
        work.append("\nNo agents yet. Select this group and press n.")
    return work


# The agent state block is these facts, in order. Each appends its lines to the
# block, whose base style colors any text appended without one.

def identity(state, row):
    state.append(clean(row.get("harness", "unknown")))
    if row.get("model"):
        state.append(" / " + clean(row["model"]))
    live = live_label(row) if "live" in row else "unknown"
    state.append(f" · {live}", style=theme.GREEN if live == "alive" else theme.BLUE if live == "starting" else theme.YELLOW)


def damage(state, row):
    """A row whose record cannot be read says which file and why; its other facts are unknown."""
    found = row.get("damaged")
    if not isinstance(found, dict):
        return
    if found.get("recover") == "remove":  # nothing here is an agent: there is nothing to repair, only a folder to remove
        folder = Path(found.get("file", ""))
        folder = folder.parent if folder.name == "properties.json" else folder  # older listings name the missing file
        state.append("\nLeftover folder: " + clean(str(folder)), style=f"bold {theme.YELLOW}")
        state.append("\nNo agent records, only leftover files. Press d to move it to the trash; mv2 first checks that "
                     "no terminal is running for it. The other agents are not affected.", style=theme.SUBTEXT0)
        return
    state.append("\nDamaged: " + clean(found.get("file", "")), style=f"bold {theme.RED}")
    state.append("\n" + clean(found.get("reason", "")), style=theme.YELLOW)
    if found.get("recover") == "kill":  # a terminal may still be running behind the lost record
        state.append("\nPress K to find and stop its terminal. Then fix or restore the file and refresh (F5).", style=theme.SUBTEXT0)
    else:
        state.append("\nFix or restore the file and refresh (F5). The other agents are not affected.", style=theme.SUBTEXT0)


def progress(state, row):
    state.append("\nStatus: " + clean(row.get("status", "unknown")), style=theme.SUBTEXT0)
    state.append(" · Reported: " + clean(row.get("phase", "unknown")), style=theme.SUBTEXT0)


def focused(state, row):
    if row.get("focus"):
        state.append("\n◉ " + clean(row.get("focus_line", "Focused")) + " · f unfocuses", style=theme.MAUVE)


def context(state, row):
    state.append("\n" + clean(row.get("context_line", "Context: N/A")), style=theme.SUBTEXT0)


def native_mode(state, row):
    selection = row.get('runtime', {}).get('native_mode')
    if selection:
        state.append(f'\nMode: {clean(selection["mode"])} ({clean(selection["policy"])})', style=theme.SUBTEXT0)


def fallback(state, row):
    """Why the launch is not what was asked for (a Claude Code too old for the mod, a relay that was unavailable)."""
    selection = row.get('runtime', {}).get('native_mode')
    if selection and selection.get('fallback_reason'):
        state.append('\nFallback: ' + clean(selection['fallback_reason']), style=theme.YELLOW)


def plugin_outcomes(row):
    """(text, whether any is a problem): from the launch's plugin record, any harness, each plugin it left out and
    why, each plugin's problem and its mode outcome, in name order; then the record's problems no plugin entry
    says (a known plugin that is not installed has no entry), then its own error. What the harness's fallback
    reason already says, as it did for launches recorded before plugin outcomes left it, is not repeated."""
    record = (row.get("runtime") or {}).get("plugins")
    if not isinstance(record, dict):
        return "", False
    said = str(((row.get("runtime") or {}).get("native_mode") or {}).get("fallback_reason") or "")
    parts, trouble, entries = [], False, []
    found = record.get("plugins") if isinstance(record.get("plugins"), dict) else {}
    for name, detail in sorted(found.items()):
        if not isinstance(detail, dict):
            continue
        entries += [str(detail[key]) for key in ("reason", "problem") if detail.get(key)]
        if detail.get("state") == "skipped":
            reason = str(detail.get("reason") or "no reason given")
            if reason not in said:
                parts.append(f"{name} left out: {reason}")
                trouble = True
            continue
        if detail.get("problem") and str(detail["problem"]) not in said:
            parts.append(f"{name}: {detail['problem']}")
            trouble = True
        if detail.get("mode"):
            parts.append(f"{name}: {detail['mode']}")
    for problem in record.get("problems") if isinstance(record.get("problems"), list) else ():
        if problem and not any(str(problem) in text or text in str(problem) for text in entries) and str(problem) not in said:
            parts.append(str(problem))
            trouble = True
    if record.get("error") and str(record["error"]) not in said:
        parts.append(str(record["error"]))
        trouble = True
    return " · ".join(clean(part) for part in parts), trouble


def plugins_line(state, row):
    """One line for what the launch's plugins came to: a problem in yellow, a plugin's reduced mode in plain text."""
    text, trouble = plugin_outcomes(row)
    if text:
        state.append("\nPlugins: " + text, style=theme.YELLOW if trouble else theme.SUBTEXT0)


def launch_error(state, row):
    if row.get('launch_error'):
        state.append('\n' + clean(row['launch_error'].splitlines()[0]), style=f'bold {theme.RED}')


def relay_loss(state, row):
    relay = row.get('runtime', {}).get('native_relay') or {}
    if relay.get('state') == 'lost':
        state.append('\n' + clean(relay.get('reason', 'Native relay lost')), style=f'bold {theme.RED}')
        state.append('\n' + clean(relay.get('detail', '')), style=theme.YELLOW)


def versions(state, row):
    found = row.get('runtime', {}).get('native_versions')
    if found:
        mismatch = found['cli'] != found['daemon']
        state.append(f'\nVersions: CLI {clean(found["cli"])} / daemon {clean(found["daemon"])}'
                     + (' (different)' if mismatch else ''), style=theme.YELLOW if mismatch else theme.SUBTEXT0)


# What is wrong comes right after who the agent is: the block is short, and a cut must take the routine lines.
AGENT_FACTS = (identity, damage, launch_error, relay_loss, fallback, plugins_line, progress, focused, context,
               native_mode, versions)


def agent_state(row):
    state = Text(style=theme.SUBTEXT0)
    for fact in AGENT_FACTS:
        fact(state, row)
    return state
