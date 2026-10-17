"""Modal screens: button-free forms, full details, and the keyboard help text."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, Static, TextArea

from ..display import clean
from ..universe import slug

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
P              Plugins of the selected group: on / off here, what each adds, and its settings
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
↑ / ↓           Choose a configured harness; Enter creates the agent
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
