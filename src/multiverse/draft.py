"""In-memory agent draft, edited directly in the hierarchy like the V1 monitor."""
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text

from .dashboard import agent_key, clean

FIELDS = ("name", "harness", "model")
LABELS = {"name": "Name", "harness": "Harness", "model": "Model"}


@dataclass
class AgentDraft:
    scope: Path
    previous_key: str | None
    values: dict
    harnesses: tuple[str, ...]
    active: str = "name"
    cursor: int = 0
    select_all: bool = True

    def __post_init__(self):
        self.cursor = len(self.values[self.active])

    @property
    def key(self):
        return agent_key({"universe": str(self.scope), "id": "__draft__"})

    def focus(self, name):
        self.active = name
        self.cursor = len(self.values[name])
        self.select_all = True

    def insert(self, text):
        if self.active == "harness":
            return
        if self.select_all:
            self.values[self.active], self.cursor = "", 0
        value = self.values[self.active]
        self.values[self.active] = value[:self.cursor] + text + value[self.cursor:]
        self.cursor += len(text)
        self.select_all = False

    def edit(self, event):
        key, value = event.key, self.values[self.active]
        if key in ("tab", "shift+tab"):
            self.focus(FIELDS[(FIELDS.index(self.active) + (-1 if key == "shift+tab" else 1)) % len(FIELDS)])
        elif self.active == "harness":
            if self.harnesses and key in ("left", "up", "right", "down", "space", "home", "end"):
                current = self.values["harness"]
                index = self.harnesses.index(current) if current in self.harnesses else -1
                index = (index + (-1 if key in ("left", "up") else 1)) % len(self.harnesses)
                if key in ("home", "end"):
                    index = 0 if key == "home" else len(self.harnesses) - 1
                self.values["harness"] = self.harnesses[index]
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
        if name == "harness":
            return Text(clean(self.values[name]) + " ▾", style="reverse" if name == self.active else "#a6adc8")
        text = Text(clean(self.values[name]) or "default", style="#a6adc8")
        if name == self.active:
            if self.select_all:
                text.stylize("reverse")
            else:
                text = Text(clean(self.values[name]) + " ", style="#cdd6f4")
                text.stylize("reverse", self.cursor, self.cursor + 1)
        return text

    def row(self):
        return {"id": "__draft__", "universe": str(self.scope), "name": self.values["name"],
                "harness": self.values["harness"], "summary": "", "task": "",
                "live": "new", "phase": "draft", "activity": "—"}
