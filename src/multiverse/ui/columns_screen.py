"""The Columns screen: which columns the agent list shows, and in what order.

It edits the listed universe's `views/default.json`, the file its columns come from: Space turns the selected
column on or off, Shift+Up / Shift+Down moves it, Enter writes the file and Esc discards. Only the file's `columns`
value changes. The marks (Unread, Effort, Blocked on) are not columns of their own, so they sit under the column
they add to and move with it. A file that changed on disk since the screen read it is never overwritten: the screen
reads it again and says so. A file it can't read is shown read-only, with why.
"""
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Label, Static

from ..display import clean
from . import views
from .columns import BLOCKED_ON, COLUMNS, EFFORT, UNREAD, core_key

HOSTS = {UNREAD: "name", EFFORT: "model", BLOCKED_ON: "summary"}  # each mark adds to that column's cell
MARK_TITLES = {UNREAD: "Unread count, on the name", EFFORT: "Effort, in the Model cell",
               BLOCKED_ON: "Blocked on, in Working on"}
HIDDEN = {"universe"}  # never drawn as a column: the tree shows each agent's universe


class ColumnsScreen(ModalScreen):
    """Toggle and order the agent columns; dismisses True once it has written the view file."""
    BINDINGS = [Binding("escape", "close", "Discard"), Binding("space", "toggle", "On / off"),
                Binding("enter", "save", "Save"),
                Binding("shift+up", "move(-1)", "Move up", priority=True),
                Binding("shift+down", "move(1)", "Move down", priority=True)]

    def __init__(self, root, name="", plugins=None):
        super().__init__()
        self.root, self.scope_name = root, name
        self.plugins = dict(plugins or {})  # {qualified key: title} of the plugin columns on here
        self.entries = []  # [{key, on}] in screen order; the marks are kept apart, under their column
        self.marks = {}  # {mark: on}
        self.read_as = None
        self.problem = None  # why the file can't be edited, when it can't
        self.kept = []
        self.changed = False

    def compose(self):
        with Vertical(id="columns-dialog"):
            yield Label("Columns · " + clean(self.scope_name), classes="detail-title", markup=False)
            yield Static("", id="columns-file", markup=False)
            yield DataTable(id="columns-table", cursor_type="row", zebra_stripes=False, show_header=False)
            yield Static("", classes="cerror", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#columns-table", DataTable).add_columns("On", "Column")
        self.load()

    def load(self, note=""):
        """Read the file again; `note` says why, on the error line."""
        try:
            keys, self.read_as = views.editable_columns(self.root)
            self.problem = None
        except (OSError, ValueError) as error:
            keys, self.problem, self.read_as = list(views.DEFAULT_COLUMNS), str(error), None
        shown = [key for key in keys if key not in HOSTS and key not in HIDDEN]
        offered = [key for key in COLUMNS if key not in HIDDEN] + list(self.plugins)
        order = ["name", *(key for key in shown if key != "name")]
        core = list(COLUMNS)
        for key in offered:  # a column that is off waits at its usual place, so turning it on puts it there
            if key in order:
                continue
            later = [i for i, other in enumerate(order) if key in core and other in core
                     and core.index(other) > core.index(key) and other != "name"]
            order.insert(later[0] if later else len(order), key)
        self.entries = [{"key": key, "on": key == "name" or key in shown} for key in order]
        self.marks = {mark: mark in keys for mark in HOSTS}
        self.kept = [key for key in keys if key in HIDDEN]  # listed, but never drawn: kept as they are
        self.changed = False
        self.query_one("#columns-file", Static).update(
            f"{views.view_file(self.root)}" + (" · read-only" if self.problem else ""))
        self.error(self.problem and f"This view can't be edited here: {self.problem}" or note)
        self.draw()

    def title_of(self, key):
        if key in COLUMNS:
            return COLUMNS[key].title
        if core_key(key):
            return f"{COLUMNS[core_key(key)].title} (core's own)"
        if key in self.plugins:
            return f"{self.plugins[key]} ({key.partition(':')[0]})"
        return f"{key} (no plugin here provides it)"

    def host_entry(self, mark):
        """The entry a mark sits under: the shown one of its column and core's own of it (`core:model`), as the
        dashboard reads it from either; with neither shown, the column itself."""
        host = HOSTS[mark]
        hosts = [entry for entry in self.entries if entry["key"] == host or core_key(entry["key"]) == host]
        return next((entry for entry in hosts if entry["on"]), None) or next(
            (entry for entry in hosts if entry["key"] == host), None) or next(iter(hosts), None)

    def rows(self):
        """[(row key, on text, column text)] as drawn: each mark right under its column."""
        hosted = {}
        for mark in HOSTS:
            entry = self.host_entry(mark)
            if entry:
                hosted.setdefault(entry["key"], []).append(mark)
        out = []
        for entry in self.entries:
            note = " (always shown)" if entry["key"] == "name" else ""
            out.append((entry["key"], "●" if entry["on"] else "○", self.title_of(entry["key"]) + note))
            for mark in hosted.get(entry["key"], ()):
                needs = "" if entry["on"] else f" (needs {self.title_of(entry['key'])})"
                out.append((mark, "  ●" if self.marks[mark] else "  ○", "  " + MARK_TITLES[mark] + needs))
        return out

    def draw(self, keep=None):
        table = self.query_one("#columns-table", DataTable)
        keep = keep or self.selected()
        table.clear()
        for key, on, title in self.rows():
            table.add_row(Text(on), Text(title), key=key)
        keys = [key for key, _, _ in self.rows()]
        if keep in keys:
            table.move_cursor(row=keys.index(keep))

    def selected(self):
        table = self.query_one("#columns-table", DataTable)
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def error(self, message=""):
        self.query_one(".cerror", Static).update(clean(message or ""))

    def editable(self):
        if self.problem:
            self.error(f"This view can't be edited here: {self.problem}")
        return not self.problem

    def action_toggle(self):
        key = self.selected()
        if not key or not self.editable():
            return
        if key in HOSTS:
            self.marks[key] = not self.marks[key]
        elif key == "name":
            self.error("Universe / agent is always shown")
            return
        else:
            entry = next(entry for entry in self.entries if entry["key"] == key)
            entry["on"] = not entry["on"]
        self.changed = True
        self.error("")
        self.draw(keep=key)

    def action_move(self, step):
        key = self.selected()
        if not key or not self.editable():
            return
        if key in HOSTS:
            self.error(f"{MARK_TITLES[key].split(',')[0]} moves with its column")
            return
        index = next(i for i, entry in enumerate(self.entries) if entry["key"] == key)
        target = index + step
        if key == "name" or target < 1 or target >= len(self.entries):  # Universe / agent stays first
            return
        self.entries[index], self.entries[target] = self.entries[target], self.entries[index]
        self.changed = True
        self.error("")
        self.draw(keep=key)

    def keys(self):
        """The columns value to write: the columns that are on, in order, each mark that is on right after its
        column (or at the end while that column is off, so it comes back with it)."""
        out, later = [], []
        for entry in self.entries:
            if entry["on"]:
                out.append(entry["key"])
            out.extend(mark for mark in HOSTS if self.marks[mark] and (self.host_entry(mark) or {}).get("key")
                       == entry["key"] and entry["on"])
        later = [mark for mark in HOSTS if self.marks[mark] and mark not in out]
        return out + later + self.kept

    def on_data_table_row_selected(self, event):
        event.stop()
        self.action_save()

    def action_save(self):
        if not self.editable():
            return
        if not self.changed:
            self.dismiss(False)
            return
        try:
            views.write_columns(self.root, self.keys(), self.read_as)
        except views.ViewChanged:
            self.load(note="The view file changed on disk since this screen read it, so nothing was written. "
                           "It now shows the file as it is.")
            return
        except (OSError, ValueError) as error:
            self.error(f"The view file could not be written: {error}")
            return
        self.dismiss(True)

    def action_close(self):
        self.dismiss(False)
