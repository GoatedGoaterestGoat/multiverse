"""The Plugins screen: which plugins are on in a scope, and each one's settings.

`PluginsScreen` lists the plugins a scope sees with their state, turns them on
and off (`plugins.json` of that scope), asks which access to grant one that can
work with less than it asks for, and opens their settings. A plugin that
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
from ..plugins import access as plugin_access, loader
from ..plugins.settings import DELETE
from .columns import COLUMNS
from .plugin_service import universe_name
from .screens import ChoiceScreen
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
    """What a plugin adds, as `columns a, b · widgets c`; empty when nothing is known or it adds nothing. A column
    standing in for a core one is said by core's title ("replaces the Context column"), so it is never silent."""
    if not isinstance(provides, dict):
        return ""
    shown = []
    replaced = [clean(COLUMNS[key].title) for key in provides.get("replaces") or () if key in COLUMNS]
    if replaced:
        shown.append(f"replaces the {' and '.join(replaced)} column{'s' if len(replaced) > 1 else ''}")
    for key, label in PROVIDES:
        value = provides.get(key)
        if isinstance(value, (list, tuple)) and value:
            shown.append(f"{label} {', '.join(clean(str(name)) for name in value)}")
        elif isinstance(value, int) and not isinstance(value, bool) and value:
            shown.append(f"{value} {label}")  # a count, e.g. services
    return " · ".join(shown)


def file_stamp(path):
    """(modified time, size, inode) of a file, or None when it is missing: a change by any writer changes it."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size, info.st_ino


def levels(item):
    """The access levels a person may grant this plugin when turning it on here, highest first; empty when there is
    nothing to choose (it asks for what it needs at least, or the universe allows no more)."""
    found = item.get("access") if isinstance(item.get("access"), dict) else {}
    order = plugin_access.LEVELS
    request, minimum, ceiling = found.get("request"), found.get("minimum"), found.get("ceiling")
    if request not in order or minimum not in order:
        return []
    top = min(order.index(request), order.index(ceiling)) if ceiling in order else order.index(request)
    allowed = order[order.index(minimum):top + 1]
    return list(reversed(allowed)) if len(allowed) > 1 else []


def access_line(item):
    """Its access here, or what it asks for while it is off, in mv2 plugins info's words, with what caps it here (the
    process's MV2_EFFECTS, the universe's access_ceiling); None for a plugin that says nothing about access."""
    found = item.get("access") if isinstance(item.get("access"), dict) else {}
    request, minimum, level = found.get("request"), found.get("minimum"), found.get("level")
    if not request:
        return None
    asks = f"it asks for {clean(request)}" + (f", at least {clean(minimum)}" if minimum and minimum != request
                                              else ", and needs it")
    if not level:
        return f"Access: {asks}; it has none while it is off"
    line = f"Access here: {clean(level)} ({asks})"
    caps = {entry.get("by"): entry.get("level") for entry in found.get("limited_by") or () if isinstance(entry, dict)}
    if caps.get("MV2_EFFECTS"):
        line += f"; MV2_EFFECTS caps every plugin in this process at {clean(caps['MV2_EFFECTS'])}"
    if caps.get("access_ceiling"):
        line += f"; this universe's access_ceiling allows at most {clean(caps['access_ceiling'])}"
    return line


def permission_lines(item):
    """What the plugin may do here: what its access level allows, then what that level holds back, each with the
    level it needs. A plugin that is off has no level yet, so its line says what it asks for."""
    texts = lambda values: "; ".join(clean(str(value)) for value in values)
    found = item.get("access") if isinstance(item.get("access"), dict) else {}
    if not item.get("on") or found.get("allowed") is None:  # off (no level), or a listing that doesn't say
        if not item.get("permissions"):
            return []
        return [("Allowed to: " if item.get("on") and "allowed" not in found else "Asks to: ") + texts(item["permissions"])]
    lines = ["Allowed to: " + texts(found["allowed"])] if found["allowed"] else []
    held = [entry for entry in found.get("held_back") or () if isinstance(entry, dict)]
    if held:  # with the one change that would give it what it asks for (a grant, the ceiling or MV2_EFFECTS)
        hint = f" ({clean(str(found['raise']))})" if found.get("raise") else ""
        lines.append(f"Held back by its {clean(str(found.get('level')))} access{hint}: "
                     + "; ".join(f"{clean(str(entry.get('text')))} (needs {clean(str(entry.get('needs')))})" for entry in held))
    return lines


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
    access = access_line(item)
    if access:
        lines.append(access)
    lines += permission_lines(item)
    for shadow in item.get("shadowed") or ():
        if str(shadow.get("directory")) in overridden:
            continue
        lines.append(f"Shadows a {clean(shadow.get('source'))} plugin at {clean(shadow.get('directory'))}")
    settings = item.get("settings")
    if settings:
        lines.append("Settings: " + clean(settings.get("path") or "") + (" (unusable)" if settings.get("problem") else ""))
        lines += ["Note: " + clean(note) for note in settings.get("notes") or ()]  # its own check's advice (1.15)
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
        self.changed = self.busy = self.opening = False
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
        self.generation, self.stamp = loader.generation(), file_stamp(self.root / "plugins.json")
        self.load()
        self.set_interval(1.0, self.follow)

    def follow(self):
        """Read the list again when a plugin finished loading (or failed to), or plugins.json changed from outside
        (a grant, an access_ceiling, a plugin turned on or off), since it was read. Never while a chooser or a
        screen of its own is open on top, or a change it started is under way: the next tick catches up."""
        if self.busy or self.opening or self.app.screen is not self:
            return
        stamp = file_stamp(self.root / "plugins.json")
        if loader.generation() != self.generation or stamp != self.stamp:
            self.generation, self.stamp = loader.generation(), stamp
            self.load(keep=(self.selected() or {}).get("name"))

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
        # The universe's problems include each plugin's settings notes, so that one is never only on a screen nobody
        # opened; the selected plugin's own are already its Note lines above, and are not named twice.
        notes = ((item or {}).get("settings") or {}).get("notes") or ()
        problems = [problem for problem in self.problems
                    if not (item and problem.startswith(f"{item['name']}: ") and any(problem.endswith(note) for note in notes))]
        if problems:
            text += "\n\n" + "\n".join("• " + clean(problem) for problem in problems)
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
        self.busy = True  # also while the access chooser is open, so a held Space opens one
        self.error("")
        offered = [] if item["on"] else levels(item)
        if not offered:
            self.switch(item["name"], not item["on"])
            return
        name, why = item["name"], (item.get("access") or {}).get("why")

        def chosen(level):
            if level is None:  # Esc: it stays off, and no grant is recorded
                self.busy = False
                return
            self.switch(name, True, level)

        rows = [(level, f"{level.capitalize()}: {plugin_access.MEANING[level]}"
                 + (" (what it asks for)" if index == 0 else "")) for index, level in enumerate(offered)]
        self.app.push_screen(ChoiceScreen(f"Turn on {name} here", rows, description=clean(
            (f"{why} " if why else "") + f"Choose the access {name} gets in this universe; it uses only what fits. "
            "Esc leaves it off.")), chosen)

    @work(group="plugins-toggle", exit_on_error=False)
    async def switch(self, name, on, grant=None):
        try:
            if grant is None:
                await asyncio.to_thread(self.service.set_enabled, self.root, name, on)
            else:
                await asyncio.to_thread(self.service.set_enabled, self.root, name, on, grant=grant)
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
        if item is None or self.busy or self.opening:  # opening: a held Enter must not stack identical screens
            return
        settings = item.get("settings")
        if not settings:
            self.error(f"{item['name']} has no settings.")
            return
        self.error("")
        if settings.get("screen"):
            self.opening = True
            self.open_own_screen(item)
        elif settings.get("fields") is None and settings.get("own_view"):
            self.opening = True
            self.open_own_view(item)  # not loaded here (off, or not yet): learned now, never by the listing
        elif settings.get("fields") is None and settings.get("schema") is None:
            self.error(f"{item['name']}'s settings cannot be edited here: {settings.get('problem') or 'no schema'}")
        else:
            self.app.push_screen(PluginSettingsScreen(self.root, self.service, item, self.scope_name), self.edited)

    @work(group="plugins-screen", exit_on_error=False)
    async def open_own_view(self, item):
        """A plugin's own settings screen or fields, loading it only now (it may be off here), or why not."""
        name = item["name"]
        try:
            view = await asyncio.to_thread(self.service.settings_view, self.root, name)
            if view.get("screen"):
                screen = await asyncio.to_thread(self.service.settings_screen, self.root, name, view["screen"])
                self.app.push_screen(screen(root=self.root, backend=self.app.backend), self.edited)
            elif view.get("loading"):  # its own screen is a moment away: not the form its schema generates instead
                self.error(f"{name} is still loading; press Enter again in a moment.")
            elif view.get("fields") is not None or item["settings"].get("schema") is not None:
                # Its own fields, else the screen built from its schema, as for a plugin that failed to load.
                shown = {**item, "settings": {**item["settings"], "fields": view.get("fields")}}
                self.app.push_screen(PluginSettingsScreen(self.root, self.service, shown, self.scope_name), self.edited)
            else:
                self.error(f"{name}'s settings cannot be edited here: {view.get('problem') or 'no schema'}")
        except Exception as error:
            self.error(f"{name}'s settings screen failed: {error}")
        finally:
            self.opening = False

    @work(group="plugins-screen", exit_on_error=False)
    async def open_own_screen(self, item):
        try:
            screen = await asyncio.to_thread(self.service.settings_screen, self.root, item["name"], item["settings"]["screen"])
            self.app.push_screen(screen(root=self.root, backend=self.app.backend), self.edited)
        except Exception as error:
            self.error(f"{item['name']}'s settings screen failed: {error}")
        finally:
            self.opening = False

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
            yield Static("", id="plugin-settings-notes", markup=False)
            yield VerticalScroll(id="plugin-settings-body")
            yield Static("", classes="cerror", markup=False)
        yield Footer()

    def on_mount(self):
        settings = self.item["settings"]
        problem = settings.get("problem")
        banner = self.query_one("#plugin-settings-problem", Static)
        banner.display = bool(problem)
        banner.update(f"This settings file cannot be used: {clean(problem)}" if problem else "")
        notes = self.query_one("#plugin-settings-notes", Static)  # advice from its check; the file is still usable
        notes.display = bool(settings.get("notes"))
        notes.update("\n".join("Note: " + clean(note) for note in settings.get("notes") or ()))
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
