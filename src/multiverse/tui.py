"""Compact Textual hierarchy. Backends own I/O; widgets own presentation."""
import asyncio
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, OptionList, Static, TextArea

from .dashboard import COLUMNS, LocalBackend, agent_key, group_key, hierarchy, rate_limits
from .display import clean
from .draft import AgentDraft, SubverseDraft
from .compaction_screens import CompactionScreen
from .inspector import Inspector
from .universe import slug
from .views import LocalView

KEYS = """NAVIGATION
↑ / k, ↓ / j   Select a row
← / h          Fold a group, or go to its parent
→ / l          Expand a group, or select its first child
Space          Fold / expand the selected group
Enter          Enter a subverse; attach to a live agent
Backspace      Return to the previous universe, restoring its context
Tab            Move between hierarchy, local view and artifact links
/              Search every subverse, including folded groups
Esc            Clear search, cancel a move, or close the current dialog

AGENTS & GROUPS
n              New agent in the selected group
m              Message the selected agent
K / s          Kill (with confirmation); s is an alias
r              Resume a stopped or dormant agent
v              Full agent details or group configuration
p              Show / hide the inspector (opens only with this key)
c              Compaction settings for the selected scope; Enter on an agent opens its plan
g              New subverse inside the selected group (inline, name only)
x              Move the selected agent or subverse: choose a group, then Enter or x
d              Delete the selected agent or subverse (to trash, with confirmation)
F5             Refresh
q              Quit the dashboard; agents keep running

FORMS
Tab / Shift-Tab  Move between fields
Ctrl+Enter       Submit
Esc              Cancel

INLINE NEW AGENT
n                Add a draft row in the selected group
Tab / Shift-Tab  Name → harness → model
↑ / ↓           Choose a configured harness; Enter confirms it
Enter            Create; Esc discards the draft

MOUSE
Click a group to fold / expand; double-click to enter it.
Click the breadcrumb to return to the previous universe.
Double-click an agent to attach. Scroll the list normally.
The key hints along the bottom are clickable.

Ctrl-b d detaches from an attached agent and returns to the dashboard that
opened it, including when that dashboard runs inside tmux. The dashboard and
agent keep running. On the dashboard itself, Ctrl-b d detaches normally.
These are the default tmux keys; custom prefixes and bindings are preserved.
Ctrl-b L is tmux's stock last-session action, not the dashboard return key."""


class Form(ModalScreen[dict | None]):
    """Button-free forms; cancellation never dispatches an action."""
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+enter", "submit", "Submit")]

    def __init__(self, title, fields=(), description=""):
        super().__init__()
        self.form_title, self.fields, self.description = title, fields, description

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.form_title, id="dialog-title", markup=False)
            if self.description:
                yield Static(self.description, id="dialog-description", markup=False)
            with VerticalScroll(id="form-fields"):
                for key, label, default, multiline, required in self.fields:
                    yield Label(label + (" *" if required else ""), markup=False)
                    if multiline:
                        yield TextArea(default, id=f"field-{key}", classes="multiline", soft_wrap=True)
                    else:
                        yield Input(value=default, id=f"field-{key}")
            yield Static("", id="form-error", markup=False)
        yield Footer()

    def on_mount(self):
        if self.fields:
            self.query_one(f"#field-{self.fields[0][0]}").focus()

    def action_cancel(self):
        self.dismiss(None)

    def action_submit(self):
        values = {}
        for key, label, _default, multiline, required in self.fields:
            widget = self.query_one(f"#field-{key}")
            value = (widget.text if multiline else widget.value).strip()
            if required and not value:
                self.query_one("#form-error", Static).update(f"{label} is required.")
                widget.focus()
                return
            if key in ("name", "harness") and value:
                try:
                    slug(value)
                except ValueError as error:
                    self.query_one("#form-error", Static).update(str(error))
                    widget.focus()
                    return
            values[key] = value
        self.dismiss(values)


class Details(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, title, content):
        super().__init__()
        self.detail_title, self.content = title, content

    def compose(self):
        with Vertical(id="details-dialog"):
            yield Label(clean(self.detail_title), classes="detail-title", markup=False)
            with VerticalScroll(id="full-details"):
                yield Static(self.content, markup=False)
        yield Footer()

    def action_close(self):
        self.dismiss()


class HarnessMenu(OptionList, can_focus=False):
    """Mouse-selectable menu; keyboard input stays with the inline draft."""


class AgentTable(DataTable):
    BINDINGS = [
        Binding("enter", "app.open", "Open"),
        Binding("space", "app.fold", "Fold", show=False),
        Binding("left,h", "app.tree_left", "Fold / parent", show=False),
        Binding("right,l", "app.tree_right", "Expand / child", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    async def _on_click(self, event):
        if self.app.draft:
            if not self.app.operating:
                row = event.style.meta.get("row", -1)
                if not 0 <= row < self.row_count or self.ordered_rows[row].key.value != self.app.draft.key:
                    return
                column = event.style.meta.get("column", -1)
                if 0 <= column < len(self.ordered_columns):
                    field = str(self.ordered_columns[column].key.value)
                    self.app.draft.focus_column(field)
                    self.app.populate_table()
            return
        # Textual's default emits RowSelected only on an already selected cell.
        # Folder rows should fold on the first click; agent activation is double-click.
        await super()._on_click(event)
        row = event.style.meta.get("row", -1)
        if 0 <= row < self.row_count and not event.style.meta.get("out_of_bounds"):
            self.app.click_row(str(self.ordered_rows[row].key.value), event.chain)

    def on_key(self, event):
        if self.app.draft:
            event.stop()
            event.prevent_default()
            self.app.draft_key(event)
        elif event.key in ("up", "down", "left", "right", "j", "k", "h", "l", "home", "end", "pageup", "pagedown"):
            self.app.cancel_inspector_timer()

    def on_paste(self, event):
        if self.app.draft:
            event.stop()
            event.prevent_default()
            if not self.app.operating:
                self.app.draft.paste(event.text)
                self.app.populate_table()


class Fleet(Vertical):
    def on_resize(self):
        # Child resize occurs after layout: use the actual available column width.
        self.call_later(self.app.populate_table)


class Breadcrumb(Static):
    def on_click(self):
        self.app.action_back(from_mouse=True)


class MultiverseApp(App):
    CSS_PATH = "tui.tcss"
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

    def __init__(self, root, backend=None, *, refresh_interval=1.5):
        super().__init__()
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

    def on_mount(self):
        self.apply_size(self.size.width, self.size.height)
        self.refresh_snapshot()
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
            self.query_one("#totals", Static).update(
                (limits + " · " if limits else "")
                + f'{totals["alive"]} live · {totals["waiting"]} waiting · {totals["blocked"]} blocked · {totals["total"]} agents')
            await self.query_one("#local-view", LocalView).show_view(root, snapshot.get("widgets", []))
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

    def populate_table(self):
        if not self.snapshot:
            return
        self.set_class(self.draft is not None, "drafting")
        search = self.query_one("#filter", Input).value
        data = self.snapshot
        if self.draft:
            data = self.draft.add_to(data)
        projected = hierarchy(data, self.collapsed, search)
        for row in projected:
            if self.draft and row["key"] == self.draft.key:
                row["kind"] = "draft"
        available = self.query_one("#fleet").content_size.width - 1
        fields = ["name", *(key for key in self.snapshot["columns"] if key not in ("name", "universe"))]
        if available < 110:
            fields = [key for key in fields if key not in ("harness", "status", "context", "cost", "id", "updated_at")]
        if self.draft:
            fields = list(self.draft.columns)
        widths = {key: COLUMNS[key][1] for key in fields}
        widths.update(name=26 if available >= 68 else 23)
        for key, width in (("live", 7), ("phase", 8), ("harness", 7), ("status", 9)):
            if key in widths:
                widths[key] = width
        if self.draft and "harness" in widths:
            widths["harness"] = min(16, max(9, max((len(name) + 2 for name in self.draft.harnesses), default=9)))
        flexible = next((key for key in ("summary", "task") if key in fields), None)
        if flexible:
            widths[flexible] = max(6, available - 2 * len(fields) - sum(w for k, w in widths.items() if k != flexible))
        self.visible_rows = {row["key"]: row for row in projected}
        self.visible_agents = {key: row for key, row in self.visible_rows.items() if row["kind"] == "agent"}
        keys = list(self.visible_rows)
        if self.selected_key not in keys:
            # Prefer a surviving ancestor after folding/removal; otherwise first agent.
            prior = self.selected_key
            candidates = []
            if prior and prior.startswith("group:"):
                scope = Path(prior[6:])
            elif self.selected:
                scope = Path(self.selected["universe"])
            else:
                scope = None
            while scope and scope.is_relative_to(self.root):
                candidates.append(group_key(scope))
                if scope == self.root:
                    break
                scope = scope.parent.parent
            self.selected_key = next((key for key in candidates if key in keys),
                                     next(iter(self.visible_agents), keys[0] if keys else None))
        table = self.query_one("#agents", DataTable)
        rendered = [(row["key"], [self.draft_cell(row, field) if row["kind"] == "draft" else self.cell(row, field)
                                  for field in fields]) for row in projected]
        if self.moving:
            for key, cells in rendered:
                if key == self.moving["key"]:
                    cells[0].append("  ⇢ moving", style="italic #f9e2af")
        signature = (list(widths.items()), rendered)
        if signature != self._table_signature:
            with self.batch_update(), table.prevent(DataTable.RowHighlighted, DataTable.RowSelected):
                table.clear(columns=True)
                for field in fields:
                    table.add_column("Universe / agent" if field == "name" else COLUMNS[field][0], key=field, width=widths[field])
                for key, cells in rendered:
                    table.add_row(*cells, key=key)
                if keys:
                    table.move_cursor(row=keys.index(self.selected_key), animate=False)
            self._table_signature = signature
        self.query_one("#empty", Static).display = not keys
        self.query_one("#empty", Static).update("No matches. Esc clears search.")
        table.display = bool(keys)
        self.show_selection(self.selected_key)
        self.call_after_refresh(self.position_harness_menu)
        if self.inspector.row:
            self.inspector.update_row(self.visible_rows.get(self.inspector.row["key"]))

    def draft_cell(self, row, field):
        if field == "name":
            label = self.cell(row, field)[:sum(len(guide) for guide, _color in row["guides"])]
            label.append("+ ", style="#94e2d5")
            label.append_text(self.draft.cell("name"))
            return label
        return self.draft.column_cell(field)

    def scope_harnesses(self, scope):
        return next((g.get("harnesses", []) for g in self.snapshot["groups"] if g["path"] == str(scope)), [])

    def position_harness_menu(self):
        menu = self.query_one("#harness-menu", HarnessMenu)
        draft = self.draft
        menu.display = bool(draft and draft.active == "harness" and draft.harnesses and not self.operating)
        if not menu.display:
            return
        if self._harness_options != draft.harnesses:
            menu.clear_options().add_options(Text(name) for name in draft.harnesses)
            self._harness_options = draft.harnesses
        value = draft.values["harness"]
        menu.highlighted = draft.harnesses.index(value) if value in draft.harnesses else None
        table = self.query_one("#agents", DataTable)
        columns = table.ordered_columns
        column = next(i for i, item in enumerate(columns) if item.key.value == "harness")
        x = table.content_region.x + sum(c.width + table.cell_padding * 2 for c in columns[:column]) - int(table.scroll_x)
        row_y = table.content_region.y + table.header_height + list(table.rows).index(draft.key) - int(table.scroll_y)
        if not table.content_region.y + table.header_height <= row_y < table.content_region.bottom:
            menu.display = False
            return
        width = min(32, max(16, max(len(name) for name in draft.harnesses) + 4))
        height = min(8, len(draft.harnesses) + 2)
        x = max(0, min(x, self.size.width - width))
        y = row_y + 1
        if y + height > self.size.height - 2:
            y = max(1, row_y - height)
        menu.styles.width, menu.styles.height = width, height
        menu.styles.offset = (x, y)
        menu.scroll_to_highlight()

    @on(OptionList.OptionSelected, "#harness-menu")
    def choose_harness(self, event):
        event.stop()
        if self.draft and not self.operating and event.option_index < len(self.draft.harnesses):
            self.draft.values["harness"] = self.draft.harnesses[event.option_index]
            self.draft.focus("model")
            self.populate_table()
            self.query_one("#agents", DataTable).focus()

    @staticmethod
    def cell(row, field):
        color = row["accent"]
        if field == "name":
            group = row["kind"] == "group"
            label = Text(style=f"bold {color} on #1e1e2e" if group else color)
            for guide, accent in row["guides"]:
                label.append(guide, style=f"not bold {accent}")
            label.append(("▾ " if row["expanded"] else "▸ ") if group else "  ")
            label.append(clean(row["name"]))
            if not group and row.get("status") == "waiting":
                label.append(" ●", style="bold #fab387")
            return label
        if row["kind"] == "group":
            value = {"live": f'{row["alive"]}/{row["total"]}',
                     "phase": "",
                     "summary": (f'{row["waiting"]} waiting · ' if row["waiting"] else "")
                                + (f'{row["blocked"]} blocked · ' if row["blocked"] else "") + clean(row["description"]),
                     "task": clean(row["description"])}.get(field, "")
            return Text(value, style=f'{color} on #1e1e2e')
        value = clean(row.get(field, "—"))
        color = {"alive": "#a6e3a1", "ending": "#fab387", "stopped": "#7f849c", "dormant": "#7f849c", "unknown": "#f9e2af",
                 "blocked": "#f9e2af", "done": "#94e2d5", "working": "#89b4fa",
                 "waiting": "#fab387", "disconnected": "bold #f38ba8"}.get(value, "#a6adc8")
        if field == "context" and row.get("context_level"):
            return Text(value, style={"warn": "#f9e2af", "high": "bold #f38ba8"}[row["context_level"]])
        if field == "status" and value == "waiting":
            return Text(value, style="bold reverse #fab387")
        return Text(value, style=color)

    def show_selection(self, key):
        row = self.visible_rows.get(key)
        self.selected_key = key
        self.selected = row if row and row["kind"] == "agent" else None
        if row and row["kind"] == "draft":
            if self.draft.active == "harness":
                value = clean(self.draft.values["harness"])
                hint = "↑↓ choose · Enter confirm · Tab next" if self.draft.harnesses else "No harnesses configured in this group"
                self.query_one("#selection", Static).update(f"Harness: {value} ▾ · {hint}")
                return
            label = Text(f"{self.draft.label()}: ", style="#94e2d5")
            value = self.draft.cell(self.draft.active)
            # Keep the insertion point visible while editing a long value.
            budget = max(8, self.size.width - len(label.plain) - 4)
            start = max(0, self.draft.cursor - budget + 1)
            label.append_text(value[start:start + budget])
            self.query_one("#selection", Static).update(label)
            return
        if row:
            content = self.scope_trail(row)
            content += " · " + clean(row.get("summary") or row.get("description") or "n new agent · g new subverse")
            self.query_one("#selection", Static).update(content)
        else:
            self.query_one("#selection", Static).update("/ search · n new agent · g new subverse · ? keys")

    def scope_trail(self, row):
        path = Path(row["path"] if row["kind"] == "group" else row["universe"])
        groups = {g["path"]: g["name"] for g in self.snapshot["groups"]}
        parts = []
        while path.is_relative_to(self.root):
            parts.append(clean(groups.get(str(path), path.name)))
            if path == self.root:
                break
            path = path.parent.parent
        return " / ".join(reversed(parts))

    @on(DataTable.RowHighlighted, "#agents")
    def highlight(self, event):
        key = str(event.row_key.value)
        if self.draft:
            return
        if key in self.visible_rows:
            self.show_selection(key)
            if self.inspector.row:
                self.queue_inspector(key)

    def click_row(self, key, chain):
        if not self.navigable():
            return
        self.show_selection(key)
        if self.moving:
            return
        row = self.visible_rows.get(key)
        if row and row["kind"] == "group":
            self.enter_scope(Path(row["path"])) if chain == 2 else self.action_fold()
        elif chain == 2:
            self.action_attach()

    def select_key(self, key):
        keys = list(self.visible_rows)
        if key in keys:
            self.query_one("#agents", DataTable).move_cursor(row=keys.index(key), animate=False)
            self.show_selection(key)

    def select_cursor(self):
        """Select the row under the table cursor, if the table has focus.

        RowHighlighted is queued. A fast arrow+Enter can arrive before it,
        so activation must use the actual cursor rather than its observer.
        """
        table = self.query_one("#agents", DataTable)
        if self.focused is not table or not 0 <= table.cursor_row < table.row_count:
            return False
        self.show_selection(str(table.ordered_rows[table.cursor_row].key.value))
        return True

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
            self.push_screen(Details("Keyboard & mouse", KEYS))

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

    def submit_operation(self, label, callback, *args, after=None):
        if self.operating:
            return
        self.operating = True
        self.set_status(label + "…")
        self.execute_operation(label, callback, args, after)

    @work(group="operation", exit_on_error=False)
    async def execute_operation(self, label, callback, args, after):
        succeeded, result = False, None
        try:
            result = await asyncio.to_thread(callback, *args)
            if label == "Send message":
                receipt = (result or {}).get('native_delivery', {})
                state = receipt.get('state', 'queued')
                detail = receipt.get('reason', 'delivery and an answer may follow')
                self.set_status(f"Message saved; {state}: {detail}", error=state in ('error', 'unavailable'))
            elif label == 'Kill agent' and (result or {}).get('native_cancellation'):
                native = result['native_cancellation']
                detail = native.get('reason', 'Native cancellation complete')
                if 'cancelled_submissions' in native:
                    detail += (f" ({native['cancelled_submissions']} queued cancelled, "
                               f"{native['interrupted_turns']} turn interrupts)")
                incomplete = native.get('state') != 'ok'
                self.set_status(('Terminal closed: ' if incomplete else 'Kill complete: ') + detail, error=incomplete)
            else:
                self.set_status(label + " complete.")
            succeeded = True
        except Exception as error:
            self.set_status(f"{label} failed: {error}", error=True)
            self.notify(str(error), title=label, severity="error", markup=False)
        finally:
            self.operating = False
        # A scan started before the action may contain old state. Finish it, then
        # read again so a completed mutation cannot be hidden until the next timer.
        if self.scanning:
            await self.workers.wait_for_complete([w for w in self.workers if w.group == "scan"])
        if succeeded and after:
            after(result)
        self.refresh_snapshot()

    def action_compaction(self):
        if self.ready():
            root = self.target_scope()
            self.push_screen(CompactionScreen(root, self.backend, self.scope_name(root)), self.compaction_saved)

    def compaction_saved(self, overview):
        if overview:
            self.set_status("Compaction settings saved. Restart agents for a changed enabled setting to apply.")

    def action_spawn(self):
        if self.ready():
            root = self.target_scope()
            if not self.snapshot:
                return
            options = self.scope_harnesses(root)
            if not options:
                self.set_status(f"No harnesses configured in {self.scope_name(root)}.", error=True)
                return
            names = {a["name"] for a in self.snapshot["agents"] if Path(a["universe"]) == root}
            harness = next((name for name in ("codex", "claude") if name in options), options[0])
            self.start_draft(AgentDraft(root, self.selected_key, {"name": self.unused("worker", names), "harness": harness,
                                                                  "model": ""}, options),
                             f"New agent in {self.scope_name(root)} · Tab field · Enter create · Esc cancel")

    @staticmethod
    def unused(base, names):
        name, number = base, 2
        while name in names:
            name, number = f"{base}-{number}", number + 1
        return name

    def subverse_names(self, root):
        return {Path(g["path"]).name for g in self.snapshot["groups"] if g["parent"] == str(root)}

    def start_draft(self, draft, message):
        self.hide_inspector()
        self.query_one("#filter", Input).value = ""
        self.query_one("#filter").display = False
        self.draft = draft
        self.selected_key = draft.key
        scope = draft.scope
        while scope.is_relative_to(self.root):
            self.collapsed.discard(str(scope))
            if scope == self.root:
                break
            scope = scope.parent.parent
        self.set_status(message)
        self.populate_table()
        self.query_one("#agents", DataTable).focus()

    def draft_key(self, event):
        if self.operating:
            return
        if event.key == "escape":
            self.cancel_draft()
        elif event.key == "enter" and self.draft.active == "harness":
            if self.draft.values["harness"] in self.draft.harnesses:
                self.draft.focus("model")
                self.populate_table()
        elif event.key in ("enter", "ctrl+enter"):
            self.commit_draft()
        else:
            self.draft.edit(event)
            self.populate_table()

    def cancel_draft(self):
        if self.draft and not self.operating:
            self.selected_key = self.draft.previous_key
            self.draft = None
            self.set_status("")
            self.populate_table()
            self.query_one("#agents", DataTable).focus()

    def commit_draft(self):
        draft = self.draft
        if not draft or self.operating:
            return
        values = {key: value.strip() for key, value in draft.values.items()}
        if problem := draft.problem(values, self.snapshot):
            field, message = problem
            draft.focus(field)
            self.set_status(message, error=True)
            self.populate_table()
            return
        self.submit_operation(draft.verb, getattr(self.backend, draft.operation), draft.scope, values, after=self.created)

    def created(self, result):
        draft, self.draft = self.draft, None
        self.collapsed.discard(str(draft.scope))
        self.selected_key = draft.created_key(result)

    def action_subverse(self):
        if self.ready():
            root = self.target_scope()
            if not self.snapshot:
                return
            self.start_draft(SubverseDraft(root, self.selected_key, {"name": self.unused("subverse", self.subverse_names(root))}),
                             f"New subverse in {self.scope_name(root)} · Enter create · Esc cancel")

    def movable(self, row, verb):
        """Explain why a row cannot be moved or deleted; the backend rechecks on disk."""
        if row["kind"] == "group" and Path(row["path"]) == self.root:
            return (f"Go back to the parent universe to {verb} {clean(row['name'])}." if self.history
                    else f"The top-level universe cannot be {verb}d here.")
        running = row["alive"] if row["kind"] == "group" else int(row["live"] in ('alive', 'ending'))
        if running:
            what = f"{running} running agent{'s' if running != 1 else ''} inside" if row["kind"] == "group" else "it"
            return f"Stop {what} first (s), then {verb} {clean(row['name'])}."
        return None

    def action_cut(self):
        if self.moving:
            self.place()
            return
        row = self.visible_rows.get(self.selected_key)
        if not self.ready() or not row or row["kind"] not in ("agent", "group"):
            return
        if problem := self.movable(row, "move"):
            self.set_status(problem, error=True)
            return
        self.hide_inspector()
        self.moving = dict(row)
        self.set_status(f"Moving {clean(row['name'])} · choose a group · Enter or x places it · Esc cancels")
        self.populate_table()

    def cancel_move(self):
        self.moving = None
        self.set_status("")
        self.populate_table()

    def place(self):
        if not self.navigable():
            return
        moving, destination = self.moving, self.target_scope()
        if moving["kind"] == "agent":
            self.submit_operation("Move agent", self.backend.move_agent, Path(moving["universe"]), moving["id"],
                                  destination, after=lambda result: self.moved(agent_key(result), destination))
        else:
            self.submit_operation("Move subverse", self.backend.move_subverse, Path(moving["path"]), destination,
                                  after=lambda result: self.moved(group_key(Path(result["universe"])), destination))

    def moved(self, key, destination):
        self.moving = None
        self.collapsed.discard(str(destination))
        self.selected_key = key

    def action_remove(self):
        row = self.visible_rows.get(self.selected_key)
        if not self.ready() or not row or row["kind"] not in ("agent", "group"):
            return
        if problem := self.movable(row, "delete"):
            self.set_status(problem, error=True)
            return
        name = clean(row["name"])
        if row["kind"] == "agent":
            detail = f"{name} moves to this universe's trash with its inbox and launch history."
            callback, args, label = self.backend.remove_agent, (Path(row["universe"]), row["id"]), "Delete agent"
        else:
            count = row["total"]
            detail = (f"{name}, its subverses and {count} agent{'s' if count != 1 else ''} move to the parent universe's trash.")
            callback, args, label = self.backend.remove_subverse, (Path(row["path"]),), "Delete subverse"
        self.push_screen(Form(f"Delete {name}?", description=
                              f"{detail} mv2 trash lists it; mv2 trash --empty deletes permanently. "
                              "Ctrl+Enter deletes. Esc cancels."),
                         lambda values: self.submit_operation(label, callback, *args) if values is not None else None)

    def action_message(self):
        if self.ready() and self.selected:
            agent = dict(self.selected)
            self.push_screen(Form(f'Message {agent["name"]}', [("text", "Message", "", True, True)]),
                             lambda values: self.submit_operation("Send message", self.backend.send, Path(agent["universe"]),
                                                                  agent["id"], values["text"]) if values is not None else None)

    def action_stop(self):
        self.action_kill()

    def action_kill(self):
        if self.ready() and self.selected and self.selected["live"] in ('alive', 'ending', 'stopped', 'dormant'):
            agent = dict(self.selected)
            description = ("Ctrl+Enter cancels native work only; no terminal remains to close."
                           if agent['live'] == 'dormant' else
                           "Ctrl+Enter ends running work and removes its tmux wrapper.")
            self.push_screen(Form(f'Kill {agent["name"]}?', description=description +
                                  " Files and conversation remain for resume. Esc cancels."),
                             lambda values: self.submit_operation("Kill agent", self.backend.kill, Path(agent["universe"]),
                                                                  agent["id"]) if values is not None else None)

    def action_resume(self):
        if self.ready() and self.selected and self.selected["live"] in ('stopped', 'dormant'):
            agent = dict(self.selected)
            self.submit_operation("Resume agent", self.backend.resume, Path(agent["universe"]), agent["id"])

    def action_attach(self):
        if self.ready() and self.selected and self.selected["live"] == "alive":
            self.operating = True
            self.attach_agent(dict(self.selected))

    @work(group="attach", exit_on_error=False)
    async def attach_agent(self, agent):
        try:
            if self.backend.demo:
                self.set_status("Demo terminal. In a live universe, Enter opens the agent's tmux pane.")
            else:
                attach_error = None
                with self.suspend():
                    # Textual resumes the terminal only after a normal exit.
                    # Report attach failures after leaving suspension.
                    try:
                        # Suspend stops the terminal writer. Do not yield to UI
                        # timers here: rendering can fill its queue and deadlock
                        # the event loop before the driver can be resumed.
                        self.backend.attach(Path(agent["universe"]), agent["id"])
                    except Exception as error:
                        attach_error = error
                if attach_error is not None:
                    raise attach_error
        except Exception as error:
            self.set_status(f"Attach failed: {error}", error=True)
        finally:
            self.operating = False
            self.refresh_snapshot()

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
