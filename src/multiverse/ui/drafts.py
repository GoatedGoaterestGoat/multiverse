"""In-memory drafts, edited directly in the hierarchy like the V1 monitor.

A draft is a pending row. It adds itself to a snapshot copy, renders its own
cells, validates what only the dashboard knows, and names the backend
operation that creates it. The dashboard stays unaware of each kind's fields.
"""
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text

from ..display import clean
from ..universe import slug
from . import theme
from .tree import agent_key, group_key

HINT = theme.OVERLAY1
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
        text = Text(clean(self.values[name]) or "default", style=theme.SUBTEXT0)
        if name == self.active:
            if self.select_all:
                text.stylize("reverse")
            else:
                text = Text(clean(self.values[name]) + " ", style=theme.TEXT)
                text.stylize("reverse", self.cursor, self.cursor + 1)
        return text

    def problem(self, values, snapshot):
        """A (field, message) the dashboard can show before calling the backend."""
        return None


class AgentDraft(Draft):
    """Name, harness, model and, where the picked model takes one, effort.

    A harness with a model list (core's, narrowed by the universe's harness settings) offers it as a picker, with
    `default` (leave it unset, for the harness to decide) only where core allows that; where the harness settings
    configure the model that leaves it unset (core's `default_model`), the effort follows that model. Where core also takes IDs it
    doesn't list (no subset is set), a model can be typed beside the list; a harness without a list takes a typed
    model. The list is read off the event
    loop and kept in `choices`, per harness: absent while it is being read, None when the harness has none."""
    labels = {"name": "Name", "harness": "Harness", "model": "Model", "effort": "Effort"}
    columns = ("name", "harness", "summary")
    operation, verb = "spawn", "Spawn agent"

    def __init__(self, scope, previous_key, values, harnesses=()):
        super().__init__(scope, previous_key, {"model": "", "effort": "", **values})
        self.harnesses = tuple(harnesses)
        self.choices = {}
        self.asking = set()  # harnesses whose list is being read

    @property
    def fields(self):
        return ("name", "harness", "model", "effort") if self.efforts() else ("name", "harness", "model")

    @property
    def key(self):
        return agent_key({"universe": str(self.scope), "id": "__draft__"})

    def listed(self):
        """The chosen harness's model list, or None while it is being read, or when it has none."""
        found = self.choices.get(self.values["harness"])
        return found if isinstance(found, dict) and found.get("models") else None

    def typed(self):
        """Whether a model may also be typed beside the list: core takes IDs it doesn't list."""
        return bool((self.listed() or {}).get("free_form"))

    def reading(self):
        return self.values["harness"] not in self.choices

    def model(self):
        listed = self.listed()
        return next((m for m in listed["models"] if m["id"] == self.values["model"]), None) if listed else None

    def configured(self):
        """The listed model leaving the model unset launches (core's default_model), whatever is picked now."""
        listed = self.listed()
        wanted = (listed or {}).get("default_model")
        return next((m for m in listed["models"] if m["id"] == wanted), None) if wanted else None

    def effective(self):
        """The listed model the effort follows: the one picked, or with `default` the one the harness settings
        configure (core's default_model, never the table's), which leaving the model unset launches."""
        listed = self.listed()
        if not self.values["model"]:
            return self.configured()
        return next((m for m in listed["models"] if m["id"] == self.values["model"]), None) if listed else None

    def efforts(self):
        model = self.effective()
        return tuple(model.get("efforts") or ()) if model else ()

    def unset_allowed(self, field):
        """Whether core lets this field be left to the harness: a narrowed list may not, since the harness's own
        choice could fall outside it."""
        if field == "model":
            return (self.listed() or {}).get("unset", True) is not False
        return (self.effective() or {}).get("effort_unset", True) is not False

    def options(self, field=None):
        """What the picker for `field` (the active one) offers, `""` meaning the harness default; None to type."""
        field = field or self.active
        if field == "harness":
            return self.harnesses
        if field == "model" and (listed := self.listed()):
            ids = tuple(m["id"] for m in listed["models"])
            return ("", *ids) if self.unset_allowed("model") else ids
        if field == "effort" and (efforts := self.efforts()):
            return ("", *efforts) if self.unset_allowed("effort") else efforts
        return None

    def option_label(self, field, value):
        if not value and not self.unset_allowed(field):
            return f"choose a{'n' if field == 'effort' else ''} {field}"
        if field == "model" and not value and (configured := self.configured()):
            return f"default ({clean(configured.get('name') or configured['id'])})"
        if field == "model" and value:
            model = next((m for m in self.listed()["models"] if m["id"] == value), None)
            name = clean(model.get("name") or value) if model else clean(value)
            return name if name == clean(value) else f"{name} ({clean(value)})"
        return clean(value) or "default"

    def choose(self, field, value):
        self.values[field] = value
        if field == "harness":
            self.values["model"] = self.values["effort"] = ""
        if field == self.active:  # typing next replaces the pick
            self.cursor, self.select_all = len(value), True
        self.settle()

    def settle(self):
        """Keep the model and effort among what the lists now offer, and focus off a field that went away."""
        if ((listed := self.listed()) and not self.typed()
                and self.values["model"] not in {m["id"] for m in listed["models"]}):
            self.values["model"] = ""
        if self.values["effort"] not in self.efforts():
            self.values["effort"] = ""
        if self.active not in self.fields:
            self.focus("model")

    def focus_column(self, column):
        super().focus_column("model" if column == "summary" else column)

    def insert(self, text):
        if self.active == "name" or (self.active == "model" and not self.reading()
                                     and (self.options() is None or self.typed())):
            super().insert(text)
            self.settle()

    def edit(self, event):
        key = event.key
        if key in ("tab", "shift+tab"):
            return super().edit(event)
        if self.active == "model" and self.reading():
            return  # nothing to edit until the harness's list is read
        options = self.options()
        if options is None or (self.active == "model" and self.typed() and key not in ("up", "down")):
            super().edit(event)  # typing, or editing a typed model; ↑↓ still choose from the list
            return self.settle()
        if options and key in ("left", "up", "right", "down", "space", "home", "end"):
            current = self.values[self.active]
            backward = key in ("left", "up")
            # Nothing chosen yet: the first step forward is the first choice, the first step back the last.
            index = options.index(current) if current in options else (len(options) if backward else -1)
            index = (index + (-1 if backward else 1)) % len(options)
            if key in ("home", "end"):
                index = 0 if key == "home" else len(options) - 1
            self.choose(self.active, options[index])

    def cell(self, name):
        if name == "harness":
            return Text(clean(self.values[name]) + " ▾", style="reverse" if name == self.active else theme.SUBTEXT0)
        if name == "model" and self.reading():
            return Text("reading models…", style="reverse" if name == self.active else HINT)
        if (name == "model" and self.typed() and self.values[name]
                and (self.model() is None or (name == self.active and not self.select_all))):
            return super().cell(name)  # a typed ID, or one being typed
        if name in ("model", "effort") and self.options(name) is not None:
            model = self.model() if name == "model" else None
            label = clean(model.get("name") or self.values[name]) if model else self.option_label(name, self.values[name])
            return Text(label + " ▾", style="reverse" if name == self.active else theme.SUBTEXT0)
        return super().cell(name)

    def column_cell(self, column):
        if column != "summary":
            return self.cell(column)
        text = Text("Model: ", style=HINT) + self.cell("model")
        if self.efforts():
            text += Text(" · Effort: ", style=HINT) + self.cell("effort")
        return text

    def add_to(self, snapshot):
        # Harnesses are read from each snapshot so a removed one needs a new choice.
        self.harnesses = tuple(next((g.get("harnesses", []) for g in snapshot["groups"]
                                     if g["path"] == str(self.scope)), []))
        row = {"id": "__draft__", "universe": str(self.scope), "name": self.values["name"],
               "harness": self.values["harness"], "live": "new", "phase": "draft", "status": "—"}
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
        if self.reading():
            return "model", "Still reading this harness's models; try again in a moment."
        for key in ("model", "effort"):
            if key in self.fields and self.options(key) is not None and not values[key] and not self.unset_allowed(key):
                return key, f"Choose a{'n' if key == 'effort' else ''} {key}: this harness's settings allow only those listed."
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
                 "name": self.values["name"], "description": "", "accent": parent.get("accent", theme.SUBTEXT0),
                 "harnesses": [], "alive": 0, "blocked": 0, "waiting": 0, "total": 0}
        return {**snapshot, "groups": [*snapshot["groups"], group]}

    @staticmethod
    def created_key(path):
        return group_key(path)
