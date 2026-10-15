"""Agent table columns: a title, a width and how each cell renders.

A view's `columns` list names keys of COLUMNS. A column renders agent rows and
group (subverse) rows separately; add one by registering a Column, or a
subclass that overrides `agent` or `group_value`, under a new key.

Plugins add columns too (`mv.dashboard.column`). COLUMNS stays core only;
`lookup` also finds the plugin columns the current view named. The backend
resolves them into PLUGIN_COLUMNS and computes their cells (`plugin_cells`) off
the event loop, so rendering only reads what a row already carries.
"""
from dataclasses import dataclass, field
import threading

from rich.text import Text

from ..display import clean
from . import theme

# Below this table width, columns that are not `compact` are hidden.
COMPACT_WIDTH = 110

# Words that carry their own color wherever an agent cell shows them.
STATE_COLORS = {"alive": theme.GREEN, "ending": theme.PEACH, "stopped": theme.OVERLAY1, "dormant": theme.OVERLAY1,
                "unknown": theme.YELLOW, "blocked": theme.YELLOW, "done": theme.TEAL, "working": theme.BLUE,
                "waiting": theme.PEACH, "disconnected": f"bold {theme.RED}"}


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    width: int
    compact: bool = True    # still shown in a narrow table
    flexible: bool = False  # takes the width the other columns leave; the first one registered wins

    def render(self, row):
        return self.group(row) if row["kind"] == "group" else self.agent(row)

    def agent(self, row):
        value = clean(row.get(self.key, "—"))
        return Text(value, style=STATE_COLORS.get(value, theme.SUBTEXT0))

    def group(self, row):
        return Text(self.group_value(row), style=f'{row["accent"]} on {theme.BASE}')

    def group_value(self, row):
        return ""


class NameColumn(Column):
    """Tree guides in each ancestor's accent, a fold arrow on groups, and a dot on agents waiting for input."""
    def render(self, row):
        color = row["accent"]
        group = row["kind"] == "group"
        label = Text(style=f"bold {color} on {theme.BASE}" if group else color)
        for guide, accent in row["guides"]:
            label.append(guide, style=f"not bold {accent}")
        label.append(("▾ " if row["expanded"] else "▸ ") if group else "  ")
        label.append(clean(row["name"]))
        if not group and row.get("status") == "waiting":
            label.append(" ●", style=f"bold {theme.PEACH}")
        return label


class LiveColumn(Column):
    def group_value(self, row):
        return f'{row["alive"]}/{row["total"]}'


class StatusColumn(Column):
    def agent(self, row):
        text = super().agent(row)
        if text.plain == "waiting":
            text.style = f"bold reverse {theme.PEACH}"
        return text


class ContextColumn(Column):
    LEVELS = {"warn": theme.YELLOW, "high": f"bold {theme.RED}"}

    def agent(self, row):
        text = super().agent(row)
        if row.get("context_level"):
            text.style = self.LEVELS[row["context_level"]]
        return text


class SummaryColumn(Column):
    def group_value(self, row):
        return ((f'{row["waiting"]} waiting · ' if row["waiting"] else "")
                + (f'{row["blocked"]} blocked · ' if row["blocked"] else "") + clean(row["description"]))


class TaskColumn(Column):
    def group_value(self, row):
        return clean(row["description"])


COLUMNS = {column.key: column for column in (
    NameColumn("name", "Universe / agent", 26),
    Column("harness", "Harness", 7, compact=False),
    LiveColumn("live", "Live", 7),
    ContextColumn("context", "Context", 10, compact=False),
    Column("cost", "Cost", 7, compact=False),
    StatusColumn("status", "Status", 9, compact=False),
    Column("phase", "Phase", 8),
    SummaryColumn("summary", "Working on", 48, flexible=True),
    Column("id", "ID", 12, compact=False),
    Column("universe", "Universe", 24),
    TaskColumn("task", "Task", 48, flexible=True),
    Column("updated_at", "Updated", 26, compact=False),
)}


@dataclass(frozen=True)
class PluginColumn(Column):
    """A plugin's column. Rows carry its cells in `plugin_cells`, computed by `cells` off the event loop.

    An agent whose universe has the plugin off, a failure, or a call that ran
    out of time shows `—`; a failure counts as the plugin's error.
    """
    plugin: str = ""
    loaded: object = field(default=None, compare=False, repr=False)
    spec: object = field(default=None, compare=False, repr=False)

    def agent(self, row):
        value = (row.get("plugin_cells") or {}).get(self.key)
        return Text("—" if value is None else value, style=theme.SUBTEXT0)

    def group_value(self, row):
        return (row.get("plugin_cells") or {}).get(self.key) or ""


@dataclass(frozen=True)
class MissingColumn(Column):
    """A plugin column the view names that no plugin on here provides (or two do): `—` throughout."""

    def agent(self, row):
        return Text("—", style=theme.SUBTEXT0)


# Plugin columns the current view names, by the key it names them with; replaced whole by the backend.
PLUGIN_COLUMNS = {}


_runs = {}  # (id(loaded), key, group) -> {"run": a Run still going or None, "last": {row key: text}, "slow": bool}


class Run:
    """One pass of a plugin column over its rows, filled row by row so a pass that outlives its budget still counts."""

    def __init__(self):
        self.partial = {}  # row key -> text or None (the plugin had no value), as each row is answered
        self.finished = threading.Event()


def row_key(row, group):
    return str(row.get("path") if group else row.get("id") or row.get("name"))


def fill_cells(rows, group=False):
    """Give each row its `plugin_cells`: {column key: text} for the plugin columns the view names.

    One call per column for all `rows`, inside one time budget (`loader.guarded`), so a slow column costs
    one budget per refresh however many rows there are. Only rows whose universe has the plugin on are
    given to it. The rows it answered before the budget ran out are shown; the pass goes on in its thread,
    later refreshes read what it has by then (and start no second pass meanwhile), and a column that has
    been slow waits only a moment for the next pass. A row not answered yet shows `…`; one the plugin has
    no value for shows `—`; a row that makes it raise costs one recorded error.
    """
    from ..plugins import loader
    for key, column in PLUGIN_COLUMNS.items():
        if not isinstance(column, PluginColumn):
            continue
        function = column.spec.group if group else column.spec.value
        wanted = [row for row in rows if column.plugin in row.get("plugins", ())]
        if function is None or not wanted:
            continue
        what = f"column {column.spec.key}" + (" (subverses)" if group else "")
        where = wanted[0].get("path") if group else wanted[0].get("universe")
        entry = _runs.setdefault((id(column.loaded), key, group), {"run": None, "last": {}, "slow": False})
        run = entry["run"]
        if run is not None and run.finished.is_set():  # a pass that ran long has landed: it is the latest full answer
            entry["last"], entry["run"], run = dict(run.partial), None, None
        if run is None:
            run = Run()

            def answer(rows=wanted, function=function, what=what, where=where, run=run):
                failure = None
                try:
                    for row in rows:
                        try:
                            value = function(dict(row))
                        except Exception as error:  # one row's trouble is not the others'
                            failure, value = failure or error, None
                        run.partial[row_key(row, group)] = None if value is None else clean(str(value))
                    if failure is not None:
                        loader.record(column.loaded, what, failure, where)
                finally:
                    run.finished.set()

            budget = 0.2 if entry["slow"] else loader.SYNCHRONOUS_BUDGET
            outcome = loader.guarded(column.loaded, what, answer, root=where, timeout=budget)
            if outcome.state == "busy":  # nothing started: an earlier pass still holds the plugin's call
                pass
            elif run.finished.is_set():
                entry["slow"] = False
                entry["last"], entry["run"] = dict(run.partial), None
            else:
                entry["slow"], entry["run"] = True, run
        current = entry["run"].partial if entry["run"] is not None else {}
        for row in wanted:
            rk = row_key(row, group)
            value = current[rk] if rk in current else entry["last"].get(rk, "…" if entry["run"] is not None else None)
            if value is not None:
                row.setdefault("plugin_cells", {})[key] = value


def reset_plugin_caches():
    """Forget resolved plugin columns and panels, after a plugin or its settings change."""
    global PLUGIN_COLUMNS
    PLUGIN_COLUMNS = {}
    from . import panels
    panels._plugin_panels.clear()


def lookup(key):
    """The column a view's key names: core first, then the plugin columns resolved for the view."""
    return COLUMNS.get(key) or PLUGIN_COLUMNS.get(key) or MissingColumn(key, key.rpartition(":")[2][:12], 8)


def resolve_plugin_columns(scopes, keys):
    """PluginColumn (or MissingColumn) for each non-core key, from the plugins on in `scopes`.

    `plugin:key` comes from that plugin wherever in the tree it is on; a short
    key from the one plugin on in the first scope that registers it. Two
    plugins with one short key make it a MissingColumn.
    """
    from .. import plugins
    wanted = [key for key in keys if key not in COLUMNS]
    found = {}
    if not wanted:
        return found
    offered = [plugins.dashboard_columns(scope) for scope in scopes]
    for key in wanted:
        owner = next((columns[key] for columns in offered if key in columns), None)
        if owner is None and ":" not in key:
            claimed = {loaded.name for columns in offered for name, (loaded, _) in columns.items()
                       if name.endswith(":" + key)}
            if len(claimed) > 1:
                found[key] = MissingColumn(key, key[:12], 8)
                continue
        if owner is None:
            found[key] = MissingColumn(key, key.rpartition(":")[2][:12], 8)
            continue
        loaded, spec = owner
        found[key] = PluginColumn(key, spec.title, spec.width, spec.compact, spec.flexible,
                                  plugin=loaded.name, loaded=loaded, spec=spec)
    return found
