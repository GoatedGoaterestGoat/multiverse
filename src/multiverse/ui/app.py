"""The dashboard app: layout, key bindings, refreshing from the backend and moving between scopes.

The table (fleet.py), tree editing (editing.py) and agent commands (commands.py)
are mixins of MultiverseApp. Their key actions stay app actions, so bindings and
check_action see them; Textual @on handlers must stay on this class.
"""
import asyncio
from pathlib import Path
import sys

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Provider
from textual.containers import Horizontal
from textual.content import Content
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, OptionList, Static, TextArea
from textual.css.query import NoMatches
from textual.worker import WorkerFailed

from ..display import clean
from ..plugins import loader
from . import columns, theme
from .backend import LocalBackend
from .commands import AgentCommands
from .editing import TreeEditing
from .fleet import FleetView
from .inspector import Inspector
from .panels import LocalView
from .plugin_service import PluginService, error_text, plugin_of, universe_name
from .plugins_screen import PluginsScreen
from .screens import KEYS, Details
from .tree import group_key, rate_limits
from .widgets import AgentTable, Breadcrumb, Fleet, HarnessMenu


HOST_REFRESH = 15.0  # seconds between reads of the plugin host's state


class PluginCommands(Provider):
    """The command palette's plugin entries. A plugin's title is text, so it is shown as Text: Textual's own
    provider would read `[/x]` in it as markup."""

    async def discover(self):
        for title, help_text, callback in self.app.plugin_palette():
            yield DiscoveryHit(Text(title), callback, text=title, help=help_text)

    async def search(self, query):
        matcher = self.matcher(query)
        for title, help_text, callback in self.app.plugin_palette():
            score, offsets = matcher.fuzzy_search.match(matcher.query, title)
            if score > 0:
                # Content(title), not matcher.highlight(title): that reads the title as markup.
                shown = Content(title)
                for offset in offsets:
                    if not title[offset].isspace():
                        shown = shown.stylize(matcher.match_style, offset, offset + 1)
                yield Hit(score, shown, callback, text=title, help=help_text)


class ContainsPluginErrors:
    """For the app and its main screen, whose message loops are the dashboard. A plugin's failure that escapes one of
    their message handlers (a plugin widget's render() raising, or its text reading as broken markup, while Textual
    updates or lays out the screen; a callback a plugin's widget asked them to run) is that plugin's and is contained
    here. Otherwise Textual ends that message loop after reporting the error, and with it the dashboard: the main
    screen's widgets are torn down, or the app quits. Textual guards a layout refresh this way, but not an update or a
    callback. Anything not a plugin's still raises."""

    async def _dispatch_message(self, message):
        try:
            return await super()._dispatch_message(message)
        except Exception as error:
            if plugin_of(error) is None:
                raise
            self.app._handle_exception(error)


class MainScreen(ContainsPluginErrors, Screen):
    """The dashboard's own screen."""


class MultiverseApp(FleetView, TreeEditing, AgentCommands, ContainsPluginErrors, App):
    CSS_PATH = "app.tcss"
    COMMANDS = App.COMMANDS | {PluginCommands}
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
        Binding("r", "resume", "Resume", show=False), Binding("P", "plugins", "Plugins", show=False),
        Binding("f5", "refresh", "Refresh", show=False),
    ]

    def __init__(self, root, backend=None, *, refresh_interval=1.5, plugin_service=None):
        super().__init__()
        self.plugin_service = plugin_service or PluginService()
        self.plugin_actions, self.plugin_screens = {}, {}
        self.registry_generation = None  # loader.generation() when the palette was last read
        self.host = None  # the plugin host: {needed, running, stopped, ...}; None until read
        self.host_failed = False
        self.host_user_stopped = False  # stopped from the Plugins screen with h: do not start it again until h does
        self.root = Path(root)
        self.home = self.root  # where the dashboard was opened: the navigation boundary and where plugin screens come from
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

    def get_default_screen(self):
        return MainScreen(id="_default")

    def get_css_variables(self):
        # app.tcss names colors as $mv-NAME; theme.py holds their values.
        return {**super().get_css_variables(), **theme.css_variables()}

    def on_mount(self):
        self.apply_size(self.size.width, self.size.height)
        self.refresh_snapshot()
        self.load_plugin_registry()
        self.check_host()
        if self.refresh_interval:
            self.set_interval(self.refresh_interval, self.refresh_snapshot)
            self.set_interval(HOST_REFRESH, lambda: self.check_host(start=False))
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
        if action == "filter":
            # Handle the opening slash before buffered keys are forwarded to
            # the old widget, but leave literal slashes in editors and drafts.
            return self.ready() and not isinstance(self.focused, (Input, TextArea))
        return True

    def refresh_snapshot(self):
        if self.registry_generation is not None and loader.generation() != self.registry_generation:
            self.load_plugin_registry()  # a plugin finished loading (or failed to) since: its palette entries
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
            self.show_totals()
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

    def show_totals(self):
        """The header counts: agents, the account's rate limits, plugin errors and a plugin host that is needed but off."""
        if not self.snapshot:
            return
        totals = self.snapshot["counts"]
        limits = rate_limits(self.snapshot["agents"])
        failures = self.plugin_service.error_count() + len((self.host or {}).get("stopped") or ())
        line = Text((limits + " · " if limits else "")
                    + f'{totals["alive"]} live · {totals["waiting"]} waiting · {totals["blocked"]} blocked · {totals["total"]} agents'
                    + (f' · {failures} plugin error{"s" if failures != 1 else ""}' if failures else ''))
        if self.host and self.host.get("needed") and not self.host.get("running"):
            line.append(" · host off", style=theme.PEACH)
        self.query_one("#totals", Static).update(line)

    @work(group="plugin-host", exclusive=True, exit_on_error=False)
    async def check_host(self, start=True):
        """Read the plugin host's state and, when plugins need it and it is not running, start it (once per change)."""
        if self.backend.demo:
            return
        root = self.root
        try:
            state = await asyncio.to_thread(self.plugin_service.host_state, root)
            if (state and start and state["needed"] and not state["running"] and not self.host_failed
                    and not self.host_user_stopped):
                self.set_status("Starting the plugin host…")
                try:
                    await asyncio.to_thread(self.plugin_service.start_host, root)
                    self.set_status("")
                except Exception as error:
                    self.host_failed = True  # not retried until something changes
                    self.set_status(f"The plugin host could not start: {error}", error=True)
                state = await asyncio.to_thread(self.plugin_service.host_state, root)
        except Exception as error:
            self.set_status(f"The plugin host could not be checked: {error}", error=True)
            return
        self.host = state
        self.show_totals()

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

    @work(group="plugin-registry", exclusive=True, exit_on_error=False)
    async def load_plugin_registry(self):
        """Find the palette actions and screens of the plugins on in this tree, off the event loop."""
        # From the opened root, not the portal being shown: the palette stays the same as you move between
        # subverses, and each entry says "<plugin> is off in <scope>" where it is off.
        self.registry_generation = loader.generation()
        try:
            registry = await asyncio.to_thread(self.plugin_service.registry, self.home)
        except Exception as error:
            self.set_status(f"Plugins could not be read: {error}", error=True)
            return
        self.plugin_actions = registry["actions"]
        self.plugin_screens = registry["screens"]

    def plugin_help(self):
        """The `?` help lines for what plugins add to the command palette; empty without plugins."""
        lines = [f"{'Ctrl+p':<14} {plugin}: {name}" for plugin, name in sorted(self.plugin_actions)]
        lines += [f"{'Ctrl+p':<14} {plugin}: {name} (screen)" for plugin, name in sorted(self.plugin_screens)]
        return "\n\nPLUGINS (command palette)\n" + "\n".join(lines) if lines else ""

    def run_plugin_action(self, key):
        entry = self.plugin_actions.get(key)
        if entry is None or not self.ready():
            return
        scope, row = self.target_scope(), self.visible_rows.get(self.selected_key)
        row = dict(row) if row else None
        self.submit_operation(f"{entry[0]}: {entry[1]}", self.plugin_service.run_action, scope, entry, row,
                              after=lambda text: self.set_status(text) if text else None)

    async def run_action(self, action, default_namespace=None, namespaces=None):
        """Run an action (a key binding, a button). One a plugin's screen or widget raises is that plugin's
        failure: it is contained here, because an exception that reaches the app's message loop ends the loop
        and with it the dashboard, with no message."""
        try:
            return await super().run_action(action, default_namespace, namespaces)
        except Exception as error:
            if plugin_of(error) is None:
                raise
            self._handle_exception(error)
            return True

    def _handle_exception(self, error):
        """An exception that came out of a plugin's code on the event loop (its screen composing, mounting or
        handling a key, a worker or a widget it made, one that cannot render) is that plugin's failure, not the
        dashboard's: record it, close the plugin's screen, take its failing widgets out and say so. Anything else
        ends the app as Textual does."""
        found = plugin_of(error)
        if found is None:
            return super()._handle_exception(error)
        name, loaded, package = found
        self._close_dead_pump(sys._getframe(1), package)
        error = getattr(error, "error", error) if isinstance(error, WorkerFailed) else error  # say what the worker raised
        if loaded is not None:
            self.plugin_service.record(loaded, "screen or widget", error, self.home)
        message = f"{name}: its screen or widget failed: {error_text(error)}"
        self.call_later(self.close_plugin_screens, package, message)  # off this call: the failing widget is still unwinding

    def _close_dead_pump(self, caller, package):
        """Textual reports a widget's failure from inside its message loop and then ends that loop, but the widget still
        takes messages that nobody will ever read; anything that waits for every widget to settle (Textual's test pilot,
        this dashboard's own driver) would wait on it forever until it is taken out. Mark it closing at once, so
        nothing is queued for it. Only when the report comes from where the loop really ends (its start-up, or a
        message it could not handle), never from a timer or a worker, after which the widget lives on; and only for
        the failing plugin's own widget or screen, never one of the dashboard's."""
        if caller.f_code.co_name not in ("_pre_process", "_process_messages_loop"):
            return
        pump = caller.f_locals.get("self")
        if pump is None or pump is self or not hasattr(pump, "_closing"):
            return
        owners = getattr(pump, "ancestors_with_self", [pump])
        if any(type(node).__module__ == package or type(node).__module__.startswith(package + ".")
               or getattr(node, "mv_plugin_package", None) == package for node in owners):
            pump._closing = True

    @staticmethod
    async def _drain(pump):
        """Run the callbacks still queued for a widget whose message loop has ended, as its loop would have: a wait
        for every widget to settle (one began before it died) gets its answer. Other messages for it are dropped."""
        from textual import events
        from textual._callback import invoke
        queue = pump._message_queue
        while not queue.empty():
            message = queue.get_nowait()
            if isinstance(message, events.Callback):
                try:
                    await invoke(message.callback)
                except Exception:
                    pass

    async def close_plugin_screens(self, package, message=""):
        """Close the failing plugin's screens, and only its: from the lowest one in the stack up (what it opened above
        itself goes with it), leaving any other plugin's screen and its unsaved input alone. Then take the plugin's
        widgets out of every screen that stays.

        A widget whose message loop ended with the failure is left attached by Textual with nobody to process its
        messages, and one that cannot render fails every layout; either keeps the screen from ever settling. Where a
        plugin's widgets were part of the local view, one line says what happened in their place. The status line says
        so last, once all of that is done: a failure shown there is already contained."""
        try:
            await self._close_plugin_screens(package, message)
        finally:
            try:
                self.set_status(message, error=True)
            except NoMatches:  # the dashboard is closing: there is no status line left to say it on
                pass

    async def _close_plugin_screens(self, package, message):
        def theirs(node):
            module = type(node).__module__
            return module == package or module.startswith(package + ".") \
                or getattr(node, "mv_plugin_package", None) == package

        stack = self.screen_stack
        lowest = next((index for index, screen in enumerate(stack) if index and theirs(screen)), None)
        if lowest is not None:
            while len(self.screen_stack) > lowest:
                await self.pop_screen()
        views = []
        for screen in self.screen_stack:
            for widget in list(screen.walk_children(with_self=False)):
                if not widget.is_attached:
                    continue
                task = getattr(widget, "_task", None)
                ours = theirs(widget)
                if ours and isinstance(widget.parent, LocalView) and widget.parent not in views:
                    views.append(widget.parent)
                if task is not None and task.done() and not widget.is_running:
                    widget._closing = True
                    await self._drain(widget)
                    try:
                        await widget._message_loop_exit()  # what a widget's loop does when it ends: unmount and detach
                    except Exception:
                        pass
                elif ours:
                    await widget.remove()
        for view in views:
            await view.mount(Static(message, markup=False, classes="view-error"))

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand("Plugins", "Turn plugins on or off and edit their settings", self.action_plugins)

    def plugin_palette(self):
        """[(title, help, callback)] of what plugins add to the command palette, in a stable order.

        The titles are plugin text: PluginCommands shows them as Text, never as markup."""
        entries = []
        for key in sorted(self.plugin_actions):
            plugin, name = key
            entries.append((f"{plugin}: {name}", f"{plugin}: run for the selected row or group",
                            lambda key=key: self.run_plugin_action(key)))
        for (plugin, name), (loaded, reference) in sorted(self.plugin_screens.items()):
            entries.append((f"{plugin}: {name}", f"{plugin}: open its screen",
                            lambda plugin=plugin, loaded=loaded, reference=reference, title=f"{plugin}: {name}":
                            self.open_plugin_screen(plugin, loaded, reference, title)))
        return entries

    def action_plugins(self):
        if self.ready():
            root = self.target_scope()
            self.push_screen(PluginsScreen(root, self.scope_name(root), self.plugin_service), self.plugins_changed)

    def plugins_changed(self, changed):
        if isinstance(changed, str) and changed:  # a plugin screen left a status line
            self.set_status(changed)
        if changed:
            columns.reset_plugin_caches()  # resolved plugin columns and panels may be out of date
            self.host_failed = False
            self.check_host()
            self.refresh_snapshot()
            self.load_plugin_registry()

    @work(group="plugin-screen", exit_on_error=False)
    async def open_plugin_screen(self, plugin, loaded, reference, title=None):
        """Open a plugin's screen from the palette, over whatever screen is open (the Plugins screen, another plugin's).
        Only an operation, a draft or a move under way keeps it from opening, and the status line says so."""
        if self.operating or self.draft is not None or self.moving is not None:
            what = "an operation" if self.operating else "a draft" if self.draft is not None else "a move"
            self.set_status(f"{title or plugin}: not opened while {what} is under way; finish it first", error=True)
            return
        root = self.target_scope()
        try:
            screen = await asyncio.to_thread(self.plugin_service.screen_class, loaded, reference)
            if plugin not in await asyncio.to_thread(self.plugin_service.on_here, root):
                self.set_status(f"{plugin} is off in {universe_name(root)}: turn it on from the Plugins screen (P)", error=True)
                return
            self.push_screen(screen(root=root, backend=self.backend), self.plugin_screen_closed)
        except Exception as error:
            await asyncio.to_thread(self.plugin_service.record, loaded, f"screen {reference}", error, root)
            self.set_status(f"{plugin}: screen failed: {error_text(error, kind=False)}", error=True)

    def plugin_screen_closed(self, result):
        """A plugin screen may leave a status line by dismissing with text."""
        if isinstance(result, str) and result:
            self.set_status(result)

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
