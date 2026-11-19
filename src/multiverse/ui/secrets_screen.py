"""The Secrets screen: a plugin's secrets in one universe, set, replaced or cleared, never shown.

`SecretsScreen` lists what the plugin declares (`PluginService.secrets_view`): each secret says whether it is set
here, comes from a parent universe, or is missing. A value is never typed into the dashboard: every key Textual reads
becomes an event its logs can record. Enter suspends the dashboard and asks in the terminal with `getpass`, as
`mv2 plugins secret` does, so the value goes from the terminal straight to `set_secret`, never through a widget, a
message or a worker. Ctrl+D clears the selected one, after asking. Core's errors never hold a value.
"""
import asyncio
import getpass
from contextlib import contextmanager
from pathlib import Path
import signal
import warnings

from rich.text import Text
from textual import work
from textual.app import SuspendNotSupported
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Label, Static

from ..display import clean
from .plugin_service import universe_name
from .screens import ChoiceScreen


@contextmanager
def interruptible():
    """Ctrl+C raising KeyboardInterrupt again while the terminal prompt runs. The dashboard runs under asyncio,
    whose handler only cancels the main task: the prompt would go on reading, and the next Enter end the dashboard."""
    try:
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    except ValueError:  # not the main thread: nothing to change
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class SecretsScreen(ModalScreen):
    """Set, replace or clear a plugin's secrets here; dismisses with a status line when something changed."""
    DEFAULT_CSS = """
    SecretsScreen { align: center middle; background: $mv-crust 75%; }
    SecretsScreen #secrets-table { height: auto; max-height: 60%; }
    SecretsScreen .fhelp { height: auto; color: $mv-overlay1; }
    SecretsScreen .cerror { height: auto; color: $mv-red; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("enter", "set", "Set or replace"),
                Binding("ctrl+d", "clear", "Clear here")]

    def __init__(self, root, service, name, secrets, scope_name=""):
        super().__init__()
        self.root, self.service, self.plugin, self.secrets = Path(root), service, name, list(secrets)
        self.scope_name = scope_name or universe_name(root)
        self.done = []  # what changed, for the status line
        self.busy = False

    def compose(self):
        with Vertical(id="plugins-dialog"):
            yield Label(f"Secrets · {clean(self.plugin)} · {clean(self.scope_name)}", classes="detail-title",
                        markup=False)
            yield Static("A value is never shown. Enter asks for the selected one in the terminal, with typing hidden; "
                         "Ctrl+D clears it here.", classes="fhelp", markup=False)
            yield DataTable(id="secrets-table", cursor_type="row", zebra_stripes=False)
            yield Static("", id="secret-help", classes="fhelp", markup=False)
            yield Static("", classes="cerror", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#secrets-table", DataTable).add_columns("Secret", "Here")
        self.draw()

    def label(self, secret):
        return clean(secret.get("label") or secret["key"])

    def status(self, secret):
        if not secret.get("set"):
            return "Not set · required" if secret.get("required") else "Not set"
        if secret.get("inherited"):
            return f"Set in {clean(universe_name(Path(secret['from'])))}, used here too"
        return "Set here"

    def draw(self):
        table = self.query_one("#secrets-table", DataTable)
        row = table.cursor_row if table.row_count else 0
        table.clear()
        for secret in self.secrets:
            table.add_row(Text(self.label(secret)), Text(self.status(secret)), key=secret["key"])
        if self.secrets:
            table.move_cursor(row=min(row, len(self.secrets) - 1))
        self.describe()

    def selected(self):
        table = self.query_one("#secrets-table", DataTable)
        return self.secrets[table.cursor_row] if table.row_count else None

    def describe(self):
        secret = self.selected()
        text = clean(secret.get("help") or "") if secret else f"{clean(self.plugin)} declares no secrets."
        if secret and secret.get("inherited"):
            text = (text + "\n" if text else "") + "A value set here overrides it here."
        self.query_one("#secret-help", Static).update(text)

    def on_data_table_row_highlighted(self, event):
        self.describe()

    def on_data_table_row_selected(self, event):
        event.stop()
        self.action_set()

    def error(self, message=""):
        self.query_one(".cerror", Static).update(clean(message or ""))

    def action_set(self):
        """Ask for the value in the terminal and set it from there: it never passes through Textual."""
        secret = self.selected()
        if secret is None or self.busy:
            return
        label, key = self.label(secret), secret["key"]
        outcome, problem, unhidden = None, None, False
        try:
            with self.app.suspend():
                print(f"\nSet {clean(self.plugin)}'s {label} in {clean(self.scope_name)}. Typing is hidden; an empty "
                      "line keeps it as it is.", flush=True)
                try:
                    # Where getpass can't turn echo off it warns, then reads with echo on: the warning, raised
                    # here before it reads, refuses instead.
                    with warnings.catch_warnings(), interruptible():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        value = getpass.getpass(f"{label}: ")
                except getpass.GetPassWarning:
                    value, unhidden = "", True
                except (EOFError, KeyboardInterrupt):
                    value = ""
                if value:
                    try:
                        store = self.service.set_secret(self.root, self.plugin, key, value)
                        outcome = f"set {key}" + (f" ({clean(store)})" if isinstance(store, str) and store else "")
                    except Exception as error:
                        problem = str(error)  # core's reason never holds the value
                value = None
        except SuspendNotSupported:
            self.error(f"This terminal can't ask for a secret here: run mv2 plugins secret {self.plugin} {key}")
            return
        if unhidden:
            self.error(f"This terminal can't hide typing, so nothing was asked: run mv2 plugins secret {self.plugin} "
                       f"{key} in one that can.")
            return
        if outcome:
            self.done.append(outcome)
        self.error(problem or ("" if outcome else f"{label}: nothing set."))
        self.reload()

    def action_clear(self):
        secret = self.selected()
        if secret is None or self.busy:
            return
        label = self.label(secret)
        if not secret.get("set"):
            self.error(f"{label} isn't set.")
            return
        if secret.get("inherited"):
            self.error(f"{label} comes from {clean(universe_name(Path(secret['from'])))}: clear it there.")
            return

        def chosen(choice):
            if choice == "clear":
                self.busy = True
                self.clear(secret["key"])

        self.app.push_screen(ChoiceScreen(f"Clear {label} here", [("clear", "Clear it"), ("keep", "Keep it")],
                                          description=f"{clean(self.plugin)} can't use it here once it's cleared, "
                                                      "unless a parent universe has one."), chosen)

    @work(group="plugins-secrets", exit_on_error=False, description="clear a plugin's secret")
    async def clear(self, key):
        try:
            await asyncio.to_thread(self.service.clear_secret, self.root, self.plugin, key)
            self.done.append(f"cleared {key}")
            self.error("")
        except Exception as error:
            self.error(str(error))
        self.busy = False
        self.reload()

    @work(group="plugins-secrets-read", exit_on_error=False, description="read a plugin's secrets")
    async def reload(self):
        try:  # what is set now, drawn while the plugin declares the same secrets, as the rows do
            found = await asyncio.to_thread(self.service.secrets_view, self.root, self.plugin)
            if [secret["key"] for secret in found] == [secret["key"] for secret in self.secrets]:
                self.secrets = found
        except Exception as error:
            self.error(str(error))
        self.draw()

    def action_close(self):
        if self.busy:  # closing would leave the clear running, and its outcome unsaid
            self.error("Clearing: wait for it to finish.")
            return
        self.dismiss(f"{clean(self.plugin)}: " + ", ".join(self.done) if self.done else None)
