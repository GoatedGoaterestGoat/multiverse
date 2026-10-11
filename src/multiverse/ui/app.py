"""The dashboard app: layout, key bindings, refreshing from the backend and moving between scopes.

The table (fleet.py), tree editing (editing.py) and agent commands (commands.py)
are mixins of MultiverseApp. Their key actions stay app actions, so bindings and
check_action see them; Textual @on handlers must stay on this class.
"""
import asyncio
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Input, OptionList, Static, TextArea

from ..display import clean
from . import columns, theme
from .backend import LocalBackend
from .commands import AgentCommands
from .editing import TreeEditing
from .fleet import FleetView
from .inspector import Inspector
from .panels import LocalView
from .plugin_service import PluginService
from .plugins_screen import PluginsScreen
from .screens import KEYS, Details
from .tree import group_key, rate_limits
from .widgets import AgentTable, Breadcrumb, Fleet, HarnessMenu


class MultiverseApp(FleetView, TreeEditing, AgentCommands, App):
    CSS_PATH = "app.tcss"
    TITLE = "Multiverse"
    BINDINGS = [
        Binding("enter", "open", "Open"), Binding("slash", "filter", "Find", priority=True),
        Binding("backspace", "back", "Back"),
        Binding("n", "spawn", "New agent"), Binding("g", "subverse", "New subverse"),
        Binding("m", "message", "Message"), Binding("p", "preview", "Inspector"), Binding("x", "cut", "Move"), Binding("K", "kill", "Kill"),
        Binding("question_mark", "help", "Keys"), Binding("q", "quit", "Quit"),
        Binding("escape", "clear_filter", "Clear search", show=False),
        Binding("v", "details", "Details", show=False), 
        Binding("d", "remove", "Delete", show=False), Binding("s", "stop", "Kill", show=False),
        Binding("r", "resume", "Resume", show=False), Binding("c", "compaction", "Compaction", show=False),
        Binding("f5", "refresh", "Refresh", show=False),
    ]

    def __init__(self, root, backend=None, *, refresh_interval=1.5, plugin_service=None):
        super().__init__()
        self.plugin_service = plugin_service or PluginService()
        self.plugin_keys, self.plugin_screens, self.plugin_clashes = {}, {}, []
        self.root = Path(root)
        self.backend = backend or LocalBackend()
        self.theme = "catppuccin-mocha"
        self.refresh_interval = refresh_interval
        self.snapshot = None
        self.selected = None  # Agent only; group selection lives in selected_key.
        self.selected_key = None
        self.visible_rows = {}
        self.visible_agents = {}
        self.collapsed = set()
        self.scanning = self.operating = False
        self.inspector_timer = None
        self.inspector = None
        self.draft = None
        self.moving = None  # The row being moved while a destination is chosen.
        self._harness_options = None
        self.scan_count = 0
        self._table_signature = None
        self.history = []
        self._restore_scroll = None
        self._scope_generation = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static("◈ MULTIVERSE" + (" / DEMO" if self.backend.demo else ""), id="brand")
            yield Static("", id="totals", markup=False)
        yield Breadcrumb("", id="breadcrumb", markup=False)
        yield Input(placeholder="/ Find agents, tasks, status or groups · Esc clears", id="filter")
        with Horizontal(id="body"):
            with Fleet(id="fleet"):
                yield AgentTable(id="agents", cursor_type="row", show_row_labels=False)
                yield Static("Loading universe…", id="empty", markup=False)
            yield LocalView(id="local-view")
            self.inspector = Inspector(id="inspector")
            yield self.inspector
        yield Static("", id="selection", markup=False)
        yield Static("", id="status", markup=False)
        yield Footer()
        yield HarnessMenu(id="harness-menu")

    def get_css_variables(self):
        # app.tcss names colors as $mv-NAME; theme.py holds their values.
        return {**super().get_css_variables(), **theme.css_variables()}

    def on_mount(self):
        self.apply_size(self.size.width, self.size.height)
        self.refresh_snapshot()
        self.load_plugin_registry()
        if self.refresh_interval:
            self.set_interval(self.refresh_interval, self.refresh_snapshot)
        self.query_one("#agents", DataTable).focus()

    def apply_size(self, width, height):
        self.set_class(width < 110, "compact")
        self.set_class(height < 28, "short")
        self.set_class(width < 68, "narrow")
        self.set_class(width < 120, "drawer")

    def on_resize(self, event):
        self.apply_size(event.size.width, event.size.height)

    def set_status(self, message, error=False):
        status = self.query_one("#status", Static)
        status.update(clean(message))
        status.display = bool(message)
        status.set_class(error, "error")

    def ready(self):
        return len(self.screen_stack) == 1 and not self.operating and self.draft is None and self.moving is None

    def navigable(self):
        """Moving through the tree stays possible while a move destination is chosen."""
        return self.ready() or (self.moving is not None and len(self.screen_stack) == 1 and not self.operating)

    def check_action(self, action, parameters):
        if action in ('kill', 'stop', 'resume'):
            eligible = ('stopped', 'dormant') if action == 'resume' else ('alive', 'ending', 'stopped', 'dormant')
            return bool(self.ready() and self.selected and self.selected.get('live') in eligible)
        if action == "plugin_key":
            return self.ready() and not isinstance(self.focused, (Input, TextArea))
        if action == "filter":
            # Handle the opening slash before buffered keys are forwarded to
            # the old widget, but leave literal slashes in editors and drafts.
            return self.ready() and not isinstance(self.focused, (Input, TextArea))
        return True

    def refresh_snapshot(self):
        if not self.scanning:
            self.scanning = True
            self.scan()

    @work(group="scan", exit_on_error=False)
    async def scan(self):
        root, generation = self.root, self._scope_generation
        try:
            snapshot = await asyncio.to_thread(self.backend.snapshot, root)
            if generation != self._scope_generation:
                return
            self.snapshot = snapshot
            self.scan_count += 1
            self.title = clean(snapshot["name"])
            self.query_one("#brand", Static).update(Text("◈ " + self.title + (" / DEMO" if self.backend.demo else ""),
                                                        style=snapshot["accent"]))
            trail = [entry["name"] for entry in self.history] + [self.title]
            breadcrumb = self.query_one("#breadcrumb", Breadcrumb)
            breadcrumb.display = bool(self.history)
            breadcrumb.update("‹ Back · " + " / ".join(trail))
            totals = self.snapshot["counts"]
            limits = rate_limits(snapshot["agents"])
            failures = self.plugin_service.error_count()
            self.query_one("#totals", Static).update(
                (limits + " · " if limits else "")
                + f'{totals["alive"]} live · {totals["waiting"]} waiting · {totals["blocked"]} blocked · {totals["total"]} agents'
                + (f' · {failures} plugin error{"s" if failures != 1 else ""}' if failures else ''))
            try:
                await self.query_one("#local-view", LocalView).show_view(root, snapshot.get("widgets", []))
            except Exception as error:
                # The side panel must never keep the agent table from filling.
                self.set_status(f"Could not show this view's widgets: {error}", error=True)
            if generation != self._scope_generation:
                return
            self.populate_table()
            if self._restore_scroll is not None:
                scroll = self._restore_scroll
                self._restore_scroll = None
                self.call_after_refresh(self.restore_scroll, scroll)
        except Exception as error:
            self.set_status(f"Could not refresh: {error}", error=True)
        finally:
            self.scanning = False
            if generation != self._scope_generation:
                self.refresh_snapshot()

    def restore_scroll(self, scroll):
        self.query_one("#agents", DataTable).scroll_to(x=scroll[0], y=scroll[1], animate=False)
        self.query_one("#local-view", LocalView).scroll_to(y=scroll[2], animate=False)

    @on(OptionList.OptionSelected, "#harness-menu")
    def choose_harness(self, event):
        event.stop()
        if self.draft and not self.operating and event.option_index < len(self.draft.harnesses):
            self.draft.values["harness"] = self.draft.harnesses[event.option_index]
            self.draft.focus("model")
            self.populate_table()
            self.query_one("#agents", DataTable).focus()

    @on(DataTable.RowHighlighted, "#agents")
    def highlight(self, event):
        key = str(event.row_key.value)
        if self.draft:
            return
        if key in self.visible_rows:
            self.show_selection(key)
            if self.inspector.row:
                self.queue_inspector(key)

    def action_open(self):
        if self.moving:
            if self.navigable() and self.select_cursor():
                self.place()
            return
        if self.ready() and self.select_cursor():
            if self.selected:
                self.action_attach()
            elif (row := self.visible_rows.get(self.selected_key)) and row["kind"] == "group":
                self.enter_scope(Path(row["path"]))

    def enter_scope(self, root):
        if not self.ready() or root == self.root or not self.snapshot:
            return
        table = self.query_one("#agents", DataTable)
        search = self.query_one("#filter", Input)
        self.history.append({"root": self.root, "name": self.snapshot["name"], "selected_key": self.selected_key,
                             "collapsed": set(self.collapsed), "search": search.value, "search_visible": search.display,
                             "scroll": (table.scroll_x, table.scroll_y, self.query_one("#local-view").scroll_y)})
        self.switch_scope(root)

    def action_back(self, from_mouse=False):
        if self.ready() and self.history and (from_mouse or not isinstance(self.focused, (Input, TextArea))):
            context = self.history.pop()
            self.switch_scope(context["root"], context)

    def switch_scope(self, root, context=None):
        self.hide_inspector()
        self.root = root
        self._scope_generation += 1
        self.snapshot = self.selected = None
        self.visible_rows, self.visible_agents = {}, {}
        self.collapsed = set(context["collapsed"]) if context else set()
        self.selected_key = context["selected_key"] if context else group_key(root)
        self._table_signature = None
        self._restore_scroll = context["scroll"] if context else (0, 0, 0)
        search = self.query_one("#filter", Input)
        search.value = context["search"] if context else ""
        search.display = context["search_visible"] if context else False
        self.query_one("#agents", DataTable).clear(columns=True)
        self.query_one("#local-view").remove_class("populated")
        self.query_one("#empty", Static).update("Loading universe…")
        self.query_one("#empty").display = True
        self.set_status("")
        self.refresh_snapshot()
        self.load_plugin_registry()

    def action_fold(self):
        if not self.navigable():
            return
        row = self.visible_rows.get(self.selected_key)
        if row and row["kind"] == "group":
            if self.query_one("#filter", Input).value.strip():
                self.set_status("Search reveals matching groups. Esc clears search before folding.")
                return
            if row["path"] in self.collapsed:
                self.collapsed.remove(row["path"])
            else:
                self.collapsed.add(row["path"])
            self.populate_table()

    def action_tree_left(self):
        if not self.navigable():
            return
        row = self.visible_rows.get(self.selected_key)
        if row and row["kind"] == "group" and row["expanded"] and not self.query_one("#filter", Input).value:
            self.action_fold()
        elif row and row["parent_key"]:
            self.select_key(row["parent_key"])

    def action_tree_right(self):
        if not self.navigable():
            return
        row = self.visible_rows.get(self.selected_key)
        if row and row["kind"] == "group":
            if not row["expanded"]:
                self.action_fold()
            else:
                child = next((r for r in self.visible_rows.values() if r["parent_key"] == row["key"]), None)
                if child:
                    self.select_key(child["key"])

    @on(Input.Changed, "#filter")
    def filter_changed(self):
        self.populate_table()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self):
        self.query_one("#agents", DataTable).focus()

    def action_filter(self):
        if self.ready():
            self.hide_inspector(restore_focus=False)
            search = self.query_one("#filter", Input)
            search.display = True
            # Widget.focus() is deferred; the rest of a terminal input batch
            # must see this focus immediately, without a queued table refocus.
            self.screen.set_focus(search)

    def action_clear_filter(self):
        if self.draft:
            self.cancel_draft()
            return
        if self.moving and not self.operating:
            self.cancel_move()
            return
        self.hide_inspector()
        self.query_one("#filter", Input).value = ""
        self.query_one("#filter").display = False
        self.set_status("")
        self.query_one("#agents", DataTable).focus()

    def action_refresh(self):
        self.refresh_snapshot()

    def action_help(self):
        if self.ready():
            self.push_screen(Details("Keyboard & mouse", KEYS + self.plugin_help()))

    def core_keys(self):
        """Every key the dashboard itself binds: plugins never get these."""
        names = set()
        for binding in [*type(self).BINDINGS, *AgentTable.BINDINGS]:
            names.update(binding.key.split(",") if isinstance(binding, Binding) else [])
        return names | {"ctrl+p", "ctrl+q", "tab", "shift+tab", "ctrl+c"}

    @work(group="plugin-registry", exclusive=True, exit_on_error=False)
    async def load_plugin_registry(self):
        """Find the keys and screens of the plugins on in this tree, off the event loop."""
        root, generation = self.root, self._scope_generation
        try:
            registry = await asyncio.to_thread(self.plugin_service.registry, root, self.core_keys())
        except Exception as error:
            self.set_status(f"Plugins could not be read: {error}", error=True)
            return
        if generation != self._scope_generation:
            return
        # Only plugin bindings are replaced; the app's own stay untouched.
        bindings = self._bindings.key_to_bindings
        for key in [key for key, items in bindings.items() if any(item.action.startswith("plugin_key(") for item in items)]:
            del bindings[key]
        self.plugin_keys = registry["keys"]
        self.plugin_screens = registry["screens"]
        self.plugin_clashes = registry["clashes"]
        for key, (plugin, label, *_rest) in self.plugin_keys.items():
            self.bind(key, f"plugin_key({key!r})", description=f"{plugin}: {label}", show=False)
        self.refresh_bindings()

    def plugin_help(self):
        """The `?` help lines for plugin keys, screens and clashes; empty without plugins."""
        lines = [f"{key:<14} {plugin}: {label}" for key, (plugin, label, *_rest) in sorted(self.plugin_keys.items())]
        lines += [f"Screen         {plugin}: {name} (command palette)" for plugin, name in sorted(self.plugin_screens)]
        lines += [f"Not bound: {text}" for text in self.plugin_clashes]
        return "\n\nPLUGINS\n" + "\n".join(lines) if lines else ""

    def action_plugin_key(self, key):
        entry = self.plugin_keys.get(key)
        if entry is None or not self.ready():
            return
        scope, row = self.target_scope(), self.visible_rows.get(self.selected_key)
        row = dict(row) if row else None
        label = f"{entry[0]}: {entry[1]}"
        self.submit_operation(label, self.plugin_service.run_key, scope, entry, row,
                              after=lambda text: self.set_status(text) if text else None)

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand("Plugins", "Turn plugins on or off and edit their settings", self.action_plugins)
        for (plugin, name), (loaded, reference) in sorted(self.plugin_screens.items()):
            yield SystemCommand(f"{plugin}: {name}", f"Open {plugin}'s {name} screen",
                                lambda plugin=plugin, loaded=loaded, reference=reference: self.open_plugin_screen(plugin, loaded, reference))

    def action_plugins(self):
        if self.ready():
            root = self.target_scope()
            self.push_screen(PluginsScreen(root, self.scope_name(root), self.plugin_service), self.plugins_changed)

    def plugins_changed(self, changed):
        if changed:
            columns.reset_plugin_caches()  # resolved plugin columns and panels may be out of date
            self.refresh_snapshot()
            self.load_plugin_registry()

    @work(group="plugin-screen", exit_on_error=False)
    async def open_plugin_screen(self, plugin, loaded, reference):
        if not self.ready():
            return
        root = self.target_scope()
        try:
            screen = await asyncio.to_thread(self.plugin_service.screen_class, loaded, reference)
            if plugin not in await asyncio.to_thread(self.plugin_service.on_here, root):
                self.set_status(f"{plugin} is off in {root.name}: turn it on from the Plugins screen", error=True)
                return
            self.push_screen(screen(root=root, backend=self.backend))
        except Exception as error:
            self.set_status(f"{plugin}: screen failed: {error}", error=True)

    def action_details(self):
        if self.ready() and (row := self.visible_rows.get(self.selected_key)):
            fields = (("summary", "CURRENT WORK"), ("task", "INITIAL PROMPT"), ("phase", "REPORTED PHASE"),
                      ("live", "TERMINAL"), ("status", "HARNESS STATUS"), ("reason", "OBSERVATION"),
                      ("usage_detail", "NATIVE SESSION USAGE"), ("universe", "UNIVERSE"), ("id", "AGENT ID")) if self.selected else (
                          ("description", "DESCRIPTION"), ("path", "CONFIGURATION & STATE"), ("total", "AGENTS INCLUDING SUBVERSES"))
            content = "\n\n".join(f"{label}\n{row.get(key, '—')}" for key, label in fields
                                    if key != "task" or row.get(key))
            self.push_screen(Details(row["name"], content))

    def action_preview(self):
        if self.ready():
            if self.inspector.row:
                self.hide_inspector()
            else:
                self.cancel_inspector_timer()
                self.open_inspector(self.selected_key)

    def cancel_inspector_timer(self):
        if self.inspector_timer:
            self.inspector_timer.stop()
            self.inspector_timer = None

    def queue_inspector(self, key):
        """Retarget an already open inspector after the selection settles."""
        if self.ready():
            self.cancel_inspector_timer()
            self.inspector_timer = self.set_timer(0.12, lambda: self.open_inspector(key))

    def open_inspector(self, key):
        self.inspector_timer = None
        if self.ready() and (row := self.visible_rows.get(key)):
            self.inspector.show(row)

    def hide_inspector(self, *, restore_focus=True):
        self.cancel_inspector_timer()
        self.inspector.close()
        if restore_focus:
            self.query_one("#agents", DataTable).focus()

    def target_scope(self):
        row = self.visible_rows.get(self.selected_key)
        return Path(row["universe"] if row["kind"] == "agent" else row["path"]) if row else self.root

    def scope_name(self, root):
        return next((g["name"] for g in (self.snapshot or {}).get("groups", []) if g["path"] == str(root)), root.name)

    def action_quit(self):
        if len(self.screen_stack) > 1:
            self.screen.dismiss(None)
        elif self.draft and not self.operating:
            self.cancel_draft()
        elif self.moving and not self.operating:
            self.cancel_move()
        elif not self.operating:
            self.exit()
        else:
            self.set_status("An action is still running. You can quit when it finishes.")


def start(root, backend=None):
    MultiverseApp(root, backend).run()
