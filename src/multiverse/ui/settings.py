"""Editing JSON settings in the dashboard: fields, a one-line choice widget and a base screen.

A Field says where a setting lives in its file and how it is edited. A save
starts from the file exactly as written and changes only the edited fields;
`delete` removes a cleared key and the objects it leaves empty.
"""
import math
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from ..display import clean

MISSING = object()
BOOL_OPTIONS = ((True, 'true'), (False, 'false'))


@dataclass
class Field:
    """One editable setting: where it lives in the file and how it is edited.

    A widget's value is a string (`int`, `float`, `text`, `nullable`) or one of
    `options` (`choice`, None meaning not set). `initial` is the value the
    widget starts with, so an edit is any difference from it.
    """
    key: str
    path: tuple
    label: str
    kind: str
    options: tuple = ()
    placeholder: str = ''
    initial: object = ''
    group: str | None = None
    role: str | None = None


def lookup(data, path):
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return MISSING
        data = data[key]
    return data


def shown(value):
    return 'null' if value is None else 'true' if value is True else 'false' if value is False else str(value)


def field(key, path, label, kind, data, placeholder='', options=(), **extra):
    """A field whose starting value is what `data` holds at `path`."""
    value = lookup(data, path)
    if kind == 'choice':
        if not any(item[0] is None for item in options):
            options = ((None, f'default: {shown(placeholder)}' if placeholder != '' else 'default'), *options)
        initial = None if value is MISSING else value
        if initial is not None and initial not in [item[0] for item in options]:
            options = (*options, (initial, shown(initial)))
    else:
        initial = '' if value is MISSING else shown(value)
    return Field(key, tuple(path), label, kind, tuple(options), shown(placeholder) if placeholder != '' else '', initial, **extra)


def convert(item, value):
    """A widget value as it is written in the file; MISSING when it is blank."""
    if item.kind == 'choice':
        return MISSING if value is None else value
    text = value.strip()
    if not text:
        return MISSING
    if item.kind == 'text':
        return text
    if item.kind == 'nullable':
        return None if text == 'null' else text
    try:
        number = int(text)
    except ValueError:
        if item.kind == 'int':
            raise ValueError(f'{item.label} must be a whole number') from None
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f'{item.label} must be a number') from None
        if not math.isfinite(number):
            raise ValueError(f'{item.label} must be a number') from None
    return number


def delete(data, path):
    """Remove a key and the objects it leaves empty."""
    parents = [data]
    for key in path[:-1]:
        parents.append(parents[-1].get(key) if isinstance(parents[-1], dict) else None)
    if not isinstance(parents[-1], dict):
        return
    parents[-1].pop(path[-1], None)
    for depth in range(len(path) - 1, 0, -1):
        if parents[depth] == {}:
            parents[depth - 1].pop(path[depth - 1], None)
        else:
            break


def put(data, path, value):
    for key in path[:-1]:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    data[path[-1]] = value


class Choice(Static, can_focus=True):
    """A one-line selector: left, right, space or Enter cycles through the options."""
    BINDINGS = [Binding("right,space,enter", "step(1)", "Next", show=False), Binding("left", "step(-1)", "Previous", show=False)]

    class Changed(Message):
        def __init__(self, choice):
            super().__init__()
            self.choice = choice

    def __init__(self, options, value, **kwargs):
        super().__init__("", markup=False, **kwargs)
        self.options = list(options)
        self.index = next((i for i, item in enumerate(self.options) if item[0] == value and type(item[0]) is type(value)), 0)

    def on_mount(self):
        self.show()

    @property
    def value(self):
        return self.options[self.index][0]

    def show(self):
        self.update(Text(f'◂ {self.options[self.index][1]} ▸'))

    def action_step(self, step):
        self.index = (self.index + int(step)) % len(self.options)
        self.show()
        self.post_message(self.Changed(self))


class FieldScreen(ModalScreen):
    """Shared behaviour of the screens that edit fields: widgets, visibility and the error line."""
    BINDINGS = [Binding("escape", "close", "Close"), Binding("ctrl+enter", "save", "Save")]

    def __init__(self, root, backend):
        super().__init__()
        self.root, self.backend = Path(root), backend
        self.fields, self.rows, self.widgets = [], {}, {}
        self.saving = False

    def error(self, message=""):
        self.query_one(".cerror", Static).update(clean(message))

    def field_rows(self, items):
        """Rows for fields, in order, recorded so visibility can follow the chosen values."""
        rows = []
        for item in items:
            if item.kind == 'choice':
                widget = Choice(item.options, item.initial, id=f'w{len(self.fields)}', classes='fchoice')
            else:
                widget = Input(value=item.initial, placeholder=item.placeholder, id=f'w{len(self.fields)}')
            self.fields.append(item)
            self.widgets[item.key] = widget
            row = Horizontal(Label(item.label, classes='flabel', markup=False), widget, classes='frow')
            self.rows[item.key] = row
            rows.append(row)
        return rows

    def values(self):
        return {key: widget.value for key, widget in self.widgets.items()}

    def applies(self, item, values):
        """Hook: whether a field applies, given every field's current value.

        Not `visible`: that is Textual's own property on every widget, this screen included.
        """
        return True

    def refresh_rows(self):
        values = self.values()
        for item in self.fields:
            self.rows[item.key].display = self.applies(item, values)

    def on_input_changed(self, event):
        self.refresh_rows()
        self.changed()

    def on_choice_changed(self, event):
        self.refresh_rows()
        self.changed()

    def changed(self):
        """Hook: a field was edited."""

    def action_close(self):
        self.dismiss(None)
