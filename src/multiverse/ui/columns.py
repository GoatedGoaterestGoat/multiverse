"""Agent table columns: a title, a width and how each cell renders.

A view's `columns` list names keys of COLUMNS. A column renders agent rows and
group (subverse) rows separately; add one by registering a Column, or a
subclass that overrides `agent` or `group_value`, under a new key.
"""
from dataclasses import dataclass

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
