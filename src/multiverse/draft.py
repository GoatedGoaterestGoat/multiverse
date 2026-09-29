"""In-memory drafts, edited directly in the hierarchy like the V1 monitor.

A draft is a pending row. It adds itself to a snapshot copy, renders its own
cells, validates what only the dashboard knows, and names the backend
operation that creates it. The dashboard stays unaware of each kind's fields.
"""
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text

from .dashboard import agent_key, clean, group_key
from .universe import slug

HINT = "#7f849c"
# A dot-prefixed name can never be a real subverse, so the draft row cannot collide.
PENDING = ".draft"


@dataclass
class Draft:
    scope: Path
    previous_key: str | None
    values: dict
    active: str = "name"
    cursor: int = 0
    select_all: bool = True

    fields = ("name",)
    labels = {"name": "Name"}
    columns = ("name", "summary")
    operation = verb = ""

    def __post_init__(self):
        self.cursor = len(self.values[self.active])

    def label(self):
        return self.labels[self.active]

    def focus(self, name):
        self.active = name
        self.cursor = len(self.values[name])
        self.select_all = True

    def focus_column(self, column):
        """Focus the field shown in a clicked table column."""
        self.focus(column if column in self.fields else "name")

    def insert(self, text):
        if self.select_all:
            self.values[self.active], self.cursor = "", 0
        value = self.values[self.active]
        self.values[self.active] = value[:self.cursor] + text + value[self.cursor:]
        self.cursor += len(text)
        self.select_all = False

    def paste(self, text):
        # Every field is a single line; keep what was meant, not a stray newline.
        lines = text.strip().splitlines()
        self.insert(lines[0] if lines else "")

    def edit(self, event):
        key, value = event.key, self.values[self.active]
        if key in ("tab", "shift+tab"):
            fields = self.fields
            self.focus(fields[(fields.index(self.active) + (-1 if key == "shift+tab" else 1)) % len(fields)])
        elif key in ("left", "right", "home", "end"):
            self.cursor = {"left": max(0, self.cursor - 1), "right": min(len(value), self.cursor + 1),
                           "home": 0, "end": len(value)}[key]
            self.select_all = False
        elif key == "ctrl+a":
            self.select_all = True
        elif key == "ctrl+u":
            self.values[self.active], self.cursor, self.select_all = "", 0, False
        elif key in ("backspace", "delete"):
            if self.select_all:
                self.values[self.active], self.cursor = "", 0
            elif key == "backspace" and self.cursor:
                self.values[self.active] = value[:self.cursor - 1] + value[self.cursor:]
                self.cursor -= 1
            elif key == "delete":
                self.values[self.active] = value[:self.cursor] + value[self.cursor + 1:]
            self.select_all = False
        elif event.character and event.character.isprintable():
            self.insert(event.character)

    def cell(self, name):
        text = Text(clean(self.values[name]) or "default", style="#a6adc8")
        if name == self.active:
            if self.select_all:
                text.stylize("reverse")
            else:
                text = Text(clean(self.values[name]) + " ", style="#cdd6f4")
                text.stylize("reverse", self.cursor, self.cursor + 1)
        return text

    def problem(self, values, snapshot):
        """A (field, message) the dashboard can show before calling the backend."""
        return None


class AgentDraft(Draft):
    fields = ("name", "harness", "model")
    labels = {"name": "Name", "harness": "Harness", "model": "Model"}
    columns = ("name", "harness", "summary")
    operation, verb = "spawn", "Spawn agent"

    def __init__(self, scope, previous_key, values, harnesses=()):
        super().__init__(scope, previous_key, values)
        self.harnesses = tuple(harnesses)

    @property
    def key(self):
        return agent_key({"universe": str(self.scope), "id": "__draft__"})

    def focus_column(self, column):
        super().focus_column("model" if column == "summary" else column)

    def insert(self, text):
        if self.active != "harness":
            super().insert(text)

    def edit(self, event):
        key = event.key
        if self.active != "harness" or key in ("tab", "shift+tab"):
            return super().edit(event)
        if self.harnesses and key in ("left", "up", "right", "down", "space", "home", "end"):
            current = self.values["harness"]
            index = self.harnesses.index(current) if current in self.harnesses else -1
            index = (index + (-1 if key in ("left", "up") else 1)) % len(self.harnesses)
            if key in ("home", "end"):
                index = 0 if key == "home" else len(self.harnesses) - 1
            self.values["harness"] = self.harnesses[index]

    def cell(self, name):
        if name == "harness":
            return Text(clean(self.values[name]) + " ▾", style="reverse" if name == self.active else "#a6adc8")
        return super().cell(name)

    def column_cell(self, column):
        return Text("Model: ", style=HINT) + self.cell("model") if column == "summary" else self.cell(column)

    def add_to(self, snapshot):
        # Harnesses are read from each snapshot so a removed one needs a new choice.
        self.harnesses = tuple(next((g.get("harnesses", []) for g in snapshot["groups"]
                                     if g["path"] == str(self.scope)), []))
        row = {"id": "__draft__", "universe": str(self.scope), "name": self.values["name"],
               "harness": self.values["harness"], "live": "new", "phase": "draft", "activity": "—"}
        return {**snapshot, "agents": [*snapshot["agents"], row]}

    def problem(self, values, snapshot):
        for key in ("name", "harness"):
            if not values[key]:
                return key, f"{self.labels[key]} is required."
            if key == "harness" and values[key] not in self.harnesses:
                return key, "Choose a harness configured in this group."
            try:
                slug(values[key])
            except ValueError as error:
                return key, str(error)
            if key == "name" and any(a["name"] == values["name"] and Path(a["universe"]) == self.scope
                                     for a in snapshot["agents"]):
                return key, "That name is already used in this group."
        return None

    def created_key(self, agent):
        return agent_key({**agent, "universe": str(self.scope)})


class SubverseDraft(Draft):
    """Name only; the backend validates it and a failure keeps the draft."""
    labels = {"name": "Subverse name"}
    operation, verb = "subverse", "Create subverse"

    @property
    def key(self):
        return group_key(self.scope / "subverses" / PENDING)

    def column_cell(self, _column):
        return Text("New subverse · Enter creates · Esc cancels", style=HINT)

    def add_to(self, snapshot):
        parent = next((g for g in snapshot["groups"] if g["path"] == str(self.scope)), {})
        group = {"path": str(self.scope / "subverses" / PENDING), "parent": str(self.scope),
                 "name": self.values["name"], "description": "", "accent": parent.get("accent", "#a6adc8"),
                 "harnesses": [], "alive": 0, "blocked": 0, "total": 0}
        return {**snapshot, "groups": [*snapshot["groups"], group]}

    @staticmethod
    def created_key(path):
        return group_key(path)
