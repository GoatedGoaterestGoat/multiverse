"""The Plugins screen: which plugins are on in a scope, and each one's settings.

`PluginsScreen` lists the plugins a scope sees with their state, turns them on
and off (`plugins.json` of that scope) and opens their settings. A plugin that
registered its own settings screen gets it; every other plugin gets
`PluginSettingsScreen`, a field screen generated from the JSON Schema its
manifest ships. All plugin work goes through the app's PluginService, off the
event loop, and a failure shows on the screen's error line.
"""
import asyncio
from pathlib import Path

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Label, Static

from ..display import clean
from ..plugins import loader
from ..plugins.settings import DELETE
from .plugin_service import universe_name
from .settings import BOOL_OPTIONS, MISSING, NULL, FieldScreen, convert, field, shown

STATES = {"on": "on", "loaded": "on", "off": "off", "failed": "failed to load", "unusable": "unusable",
          "untrusted": "needs trust", "changed": "changed: needs trust", "incompatible": "incompatible",
          "invalid": "invalid", "missing": "missing", "safe-mode": "safe mode"}


def state_text(item):
    return STATES.get(item.get("state"), clean(item.get("state") or "unknown"))


PROVIDES = (("columns", "columns"), ("panels", "widgets"), ("screens", "screens"), ("actions", "palette actions"),
            ("commands", "mv2 commands"), ("statusline", "status-line items"), ("events", "events"),
            ("services", "services"))


def provided(provides):
    """What a plugin adds, as `columns a, b · widgets c`; empty when nothing is known or it adds nothing."""
    if not isinstance(provides, dict):
        return ""
    shown = []
    for key, label in PROVIDES:
        value = provides.get(key)
        if isinstance(value, (list, tuple)) and value:
            shown.append(f"{label} {', '.join(clean(str(name)) for name in value)}")
        elif isinstance(value, int) and not isinstance(value, bool) and value:
            shown.append(f"{value} {label}")  # a count, e.g. services
    return " · ".join(shown)


def describe(item):
    """The selected plugin in a few lines: what it is, why it is not working and what it last did wrong."""
    lines = [f"{clean(item['name'])} {clean(item.get('version') or '')} · {clean(item.get('source') or '')}".rstrip()]
    # First, where the person looks: a plugin that came with a project must not silently replace one that came with mv2.
    overridden = {str(over.get("directory")) for over in item.get("overrides") or ()}
    for over in item.get("overrides") or ():
        lines.append(f"Overrides the {clean(over.get('source'))} {clean(item['name'])} plugin ({clean(over.get('directory'))})")
    if item.get("description"):
        lines.append(clean(item["description"]))
    if item.get("problem"):
        lines.append("Problem: " + clean(item["problem"]))
    adds = provided(item.get("provides"))
    if adds:
        lines.append("Adds here: " + adds)
    if item.get("permissions"):
        lines.append("Allowed to: " + "; ".join(clean(text) for text in item["permissions"]))
    for shadow in item.get("shadowed") or ():
        if str(shadow.get("directory")) in overridden:
            continue
        lines.append(f"Shadows a {clean(shadow.get('source'))} plugin at {clean(shadow.get('directory'))}")
    settings = item.get("settings")
    if settings:
        lines.append("Settings: " + clean(settings.get("path") or "") + (" (unusable)" if settings.get("problem") else ""))
    lines += ["Error: " + clean(text) for text in (item.get("errors") or [])[-3:]]
    return "\n".join(lines)


class PluginsScreen(ModalScreen):
    """Plugins in one scope: Space turns one on or off there, Enter opens its settings."""
    DEFAULT_CSS = """
    PluginsScreen .cerror { height: auto; color: $mv-red; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("space", "toggle", "On / off"),
                Binding("enter", "settings", "Configure"), Binding("h", "host", "Host")]

    def __init__(self, root, name="", service=None):
        super().__init__()
        self.root, self.scope_name, self.service = Path(root), name or Path(root).name, service
        self.items, self.problems, self.host = [], [], None
        self.changed = self.busy = False
        self.note = None  # a status line a plugin screen left

    def compose(self):
        with Vertical(id="plugins-dialog"):
            yield Label("Plugins · " + clean(self.scope_name), classes="detail-title", markup=False)
            yield DataTable(id="plugins-table", cursor_type="row", zebra_stripes=False)
            with VerticalScroll(id="plugin-about"):
                yield Static("Reading…", id="plugin-details", markup=False)
            yield Static("", classes="cerror", markup=False)
        yield Footer()

    def on_mount(self):
        self.service = self.service or self.app.plugin_service
        self.query_one("#plugins-table", DataTable).add_columns("Plugin", "Here", "State", "Version", "Errors")
        self.generation = loader.generation()
        self.load()
        self.set_interval(1.0, self.follow_loading)

    def follow_loading(self):
        """A plugin that finished loading (or failed to) since the list was read changes its state here."""
        if loader.generation() != self.generation and not self.busy:
            self.generation = loader.generation()
            self.load()

    def error(self, message=""):
        self.query_one(".cerror", Static).update(clean(message))

    def selected(self):
        table = self.query_one("#plugins-table", DataTable)
        if not table.row_count:
            return None
        name = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        return next((item for item in self.items if item["name"] == name), None)

    @work(group="plugins-load", exit_on_error=False)
    async def load(self, keep=None):
        """The list first, then the universe's plugin problems and the host: each shows as soon as it is read, so a
        plugin still loading (whose problems are only known once it has) never keeps the list from showing."""
        try:
            items = await asyncio.to_thread(self.service.listing, self.root)
        except Exception as error:
            self.query_one("#plugin-details", Static).update("Plugins unavailable.")
            self.error(str(error))
            return
        self.items = items
        self.fill(keep)
        try:
            self.problems = await asyncio.to_thread(self.service.problems, self.root)
            self.describe_selected()
            self.host = await asyncio.to_thread(self.service.host_state, self.root)
            self.describe_selected()
        except Exception as error:
            self.error(str(error))

    def fill(self, keep=None):
        table = self.query_one("#plugins-table", DataTable)
        keep = keep or (self.selected() or {}).get("name")
        table.clear()
        for item in self.items:
            # Text, not str: a plugin's words are never Rich markup.
            table.add_row(*(Text(cell) for cell in (
                clean(item["name"]), "on" if item["on"] else "off", state_text(item), clean(item.get("version") or "—"),
                str(len(item.get("errors") or ())) if item.get("errors") else "")), key=item["name"])
        if keep and any(item["name"] == keep for item in self.items):
            table.move_cursor(row=[item["name"] for item in self.items].index(keep), animate=False)
        self.describe_selected()

    def describe_selected(self):
        item = self.selected()
        text = describe(item) if item else "No plugins are installed here."
        if self.host and (self.host["needed"] or self.host["running"] or self.host.get("problem")):
            state = "running" if self.host["running"] else "off (h starts it)" if self.host["needed"] else "off"
            if self.host.get("problem"):
                state += " · status unknown"
            text = f"Plugin host: {state}" + "".join(f"\n  {clean(name)}: {clean(why)}" for name, why in self.host["stopped"].items()) \
                + (f"\n  {clean(self.host['problem'])}" if self.host.get("problem") else "") + "\n\n" + text
        if self.problems:
            text += "\n\n" + "\n".join("• " + clean(problem) for problem in self.problems)
        self.query_one("#plugin-details", Static).update(text)

    def on_data_table_row_highlighted(self, event):
        self.describe_selected()

    def on_data_table_row_selected(self, event):
        self.action_settings()

    def action_close(self):
        self.dismiss(self.note or self.changed)

    def action_toggle(self):
        item = self.selected()
        if item is None or self.busy:
            return
        self.busy = True
        self.error("")
        self.switch(item["name"], not item["on"])

    @work(group="plugins-toggle", exit_on_error=False)
    async def switch(self, name, on):
        try:
            await asyncio.to_thread(self.service.set_enabled, self.root, name, on)
        except Exception as error:
            self.busy = False
            self.error(str(error))
            return
        self.busy = False
        self.changed = True
        self.error("")
        self.load(keep=name)

    def action_host(self):
        """h: start the plugin host when it is off, stop it when it runs (it outlives the dashboard otherwise)."""
        if self.busy:
            return
        if self.host is None:
            self.error("The plugin host's state could not be read.")
        elif self.host["running"]:
            self.run_host(False)
        elif self.host["needed"]:
            self.run_host(True)
        else:
            self.error("No plugin here needs the plugin host.")

    def run_host(self, start):
        self.busy = True
        self.error("Starting the plugin host…" if start else "Stopping the plugin host…")
        self.change_host(start)

    @work(group="plugins-host", exit_on_error=False)
    async def change_host(self, start):
        try:
            await asyncio.to_thread(self.service.start_host if start else self.service.stop_host, self.root)
        except Exception as error:
            self.busy = False
            self.error(f"The plugin host could not {'start' if start else 'stop'}: {error}")
            self.load()
            return
        self.busy = False
        self.changed = True
        self.app.host_user_stopped = not start  # a host stopped here stays stopped until h starts it again
        self.error("")
        self.load()

    def action_settings(self):
        item = self.selected()
        if item is None or self.busy:
            return
        settings = item.get("settings")
        if not settings:
            self.error(f"{item['name']} has no settings.")
            return
        self.error("")
        if settings.get("screen"):
            self.open_own_screen(item)
        elif settings.get("fields") is None and settings.get("schema") is None:
            self.error(f"{item['name']}'s settings cannot be edited here: {settings.get('problem') or 'no schema'}")
        else:
            self.app.push_screen(PluginSettingsScreen(self.root, self.service, item, self.scope_name), self.edited)

    @work(group="plugins-screen", exit_on_error=False)
    async def open_own_screen(self, item):
        try:
            screen = await asyncio.to_thread(self.service.settings_screen, self.root, item["name"], item["settings"]["screen"])
            self.app.push_screen(screen(root=self.root, backend=self.app.backend), self.edited)
        except Exception as error:
            self.error(f"{item['name']}'s settings screen failed: {error}")

    def edited(self, saved):
        if saved:
            self.changed = True
        if isinstance(saved, str) and saved:
            self.note = saved  # shown on the dashboard's status line when this screen closes
        self.load()


def declared_fields(items, values):
    """[Field] for the fields a plugin declared with mv.dashboard.settings(fields): text, int, float, bool or choice."""
    fields = []
    for item in items:
        where = tuple(str(item["path"]).split("."))
        kind, options = item.get("kind", "text"), item.get("options") or ()
        if kind == "bool":
            kind, options = "choice", BOOL_OPTIONS
        elif kind == "choice":
            options = tuple((value, shown(value)) for value in options)
        made = field(".".join(where), where, clean(item.get("label") or ".".join(where)), kind, values,
                     item.get("placeholder", ""), options)
        made.help = clean(item.get("help") or "")
        fields.append(made)
    return fields


def schema_fields(schema, values, path=()):
    """([Field], [names]) for a settings schema: one field per scalar property, objects flattened.

    Properties that need a richer editor (arrays, objects without properties)
    are returned by name, to be edited in the settings file.
    """
    fields, skipped = [], []
    for name, spec in (schema.get("properties") or {}).items():
        where = (*path, name)
        label = clean(spec.get("title") or ".".join(where))
        key = ".".join(where)
        kinds = spec.get("type")
        kinds = kinds if isinstance(kinds, list) else [kinds] if kinds else []
        plain = [kind for kind in kinds if kind != "null"]
        default = spec.get("default", "")
        if spec.get("properties") and plain == ["object"]:
            inner, left = schema_fields(spec, values, where)
            fields += inner
            skipped += left
        elif isinstance(spec.get("enum"), list) and all(isinstance(item, (str, int, float, bool)) or item is None
                                                         for item in spec["enum"]):
            fields.append(field(key, where, label, "choice", values, default,
                                tuple((NULL if item is None else item, shown(item)) for item in spec["enum"])))
        elif plain == ["boolean"]:
            fields.append(field(key, where, label, "choice", values, default, BOOL_OPTIONS))
        elif plain == ["integer"]:
            fields.append(field(key, where, label, "int", values, default))
        elif plain == ["number"]:
            fields.append(field(key, where, label, "float", values, default))
        elif plain == ["string"]:
            fields.append(field(key, where, label, "nullable" if "null" in kinds else "text", values, default))
        else:
            skipped.append(key)
    return fields, skipped


class PluginSettingsScreen(FieldScreen):
    """A plugin's settings in one scope, generated from the schema its manifest ships.

    A blank field means the plugin's default; saving changes only the edited
    keys and writes nothing unless the whole result passes the schema and the
    plugin's own check.
    """

    def __init__(self, root, service, item, scope_name=""):
        super().__init__(root, service)
        self.item = item
        self.service = service
        self.skipped = []
        self.scope_name = scope_name or universe_name(root)

    def compose(self):
        with Vertical(id="plugins-dialog"):
            yield Label(f"Settings · {clean(self.item['name'])} · {clean(self.scope_name)}", classes="detail-title", markup=False)
            yield Static("", id="plugin-settings-problem", markup=False)
            yield VerticalScroll(id="plugin-settings-body")
            yield Static("", classes="cerror", markup=False)
        yield Footer()

    def on_mount(self):
        settings = self.item["settings"]
        problem = settings.get("problem")
        banner = self.query_one("#plugin-settings-problem", Static)
        banner.display = bool(problem)
        banner.update(f"This settings file cannot be used: {clean(problem)}" if problem else "")
        if settings.get("fields") is not None:
            fields, self.skipped = declared_fields(settings["fields"], settings.get("values") or {}), []
        else:
            fields, self.skipped = schema_fields(settings["schema"], settings.get("values") or {})
        body = self.query_one("#plugin-settings-body", VerticalScroll)
        rows = self.field_rows(fields)
        for row, made in zip(rows, fields):
            body.mount(row)
            if made.help:
                body.mount(Static(made.help, classes="fhelp", markup=False))
        if self.skipped:
            body.mount(Static("Edit these in " + clean(settings.get("path") or "the settings file") + ": "
                              + ", ".join(clean(name) for name in self.skipped), classes="cwarn", markup=False))
        if not fields:
            body.mount(Static("This plugin has no settings that can be edited here.", classes="cwarn", markup=False))

    def edits(self):
        """{path: value or DELETE} for the fields that differ from what they started as."""
        changes = {}
        values = self.values()
        for item in self.fields:
            if values[item.key] == item.initial:
                continue
            value = convert(item, values[item.key])
            changes[item.path] = DELETE if value is MISSING else value
        return changes

    def action_save(self):
        if self.saving:
            return
        try:
            changes = self.edits()
        except ValueError as error:
            self.error(str(error))
            return
        if not changes:
            self.dismiss(None)
            return
        self.saving = True
        self.error("Saving…")
        self.persist(changes)

    @work(group="plugins-save", exit_on_error=False)
    async def persist(self, changes):
        try:
            await asyncio.to_thread(self.service.edit_settings, self.root, self.item["name"], changes)
        except Exception as error:
            self.saving = False
            self.error(str(error))
            return
        self.saving = False
        self.dismiss(True)
