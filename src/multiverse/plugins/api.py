"""The mv2 plugin API, version 1. A plugin imports this module and nothing else from `multiverse`.

    from multiverse.plugins import api

    def register(mv: api.Plugin):
        mv.dashboard.column("uptime", "Up", 8, lambda row: ...)

`register(mv)` runs once in each process that loads the plugin (`mv.process`:
cli, dashboard, host or helper). It only declares: commands, columns, settings and
the rest are called later, each inside an error boundary, so a failing plugin
loses its own contribution and nothing else. Name dashboard screens and panels
by "module:Class" inside the plugin, so the command line never imports Textual.

The client (`mv.universe()`, `Universe`, `Agent`) is mv2's own operations, the
same functions `mv2` and the dashboard call. What a plugin may do follows its
manifest's `permissions`: a call it did not ask for raises `Error`. That keeps
a plugin's reach readable; it is not a sandbox.

Versioning: VERSION's minor rises when something is added, its major when
something changes or goes. Fields of `record` (an operation's full result)
are not covered.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path

from . import harness, loader, settings as settings_files
from .harness import Contribution, LaunchInfo, SessionInfo

VERSION = loader.API_VERSION
DELETE = settings_files.DELETE
BOOTSTRAP_LINES = 2
BOOTSTRAP_CHARACTERS = 300


class Error(ValueError):
    """An operation a plugin asked for could not be done; the message says why."""


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Error:
        raise
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        raise Error(str(error)) from error


class _Points:
    """One group of extension points; each method checks the manifest's `extends` first."""

    def __init__(self, loaded):
        self._loaded = loaded

    def _allow(self, point):
        if point not in self._loaded.manifest.extends:
            raise Error(f'{self._loaded.name} registers {point} but its plugin.json does not list it in "extends"')
        return self._loaded.registrations


class Cli(_Points):
    def command(self, name, configure, run, cli_run=None):
        """An `mv2 NAME` command, shaped like the tools: configure(parser), run(root, **arguments)
        returning a record, and optionally cli_run(root, **arguments) returning readable text."""
        registrations = self._allow("cli")
        if name not in self._loaded.manifest.commands:
            raise Error(f'{self._loaded.name} registers command {name!r}; declare it in plugin.json "commands"')
        if not callable(configure) or not callable(run) or (cli_run is not None and not callable(cli_run)):
            raise Error("configure, run and cli_run must be functions")
        registrations.commands[name] = loader.Command(name, configure, run, cli_run)


class Bootstrap(_Points):
    def line(self, text):
        """One line appended to every agent's bootstrap in universes where the plugin is on.

        At most 2 lines and 300 characters per plugin: the bootstrap stays short."""
        registrations = self._allow("bootstrap")
        if not isinstance(text, str) or len(text.strip().splitlines()) != 1:
            raise Error("A bootstrap line must be one line of text")
        lines = [*registrations.bootstrap, text.strip()]
        if len(lines) > BOOTSTRAP_LINES or sum(map(len, lines)) > BOOTSTRAP_CHARACTERS:
            raise Error(f"A plugin adds at most {BOOTSTRAP_LINES} bootstrap lines and "
                        f"{BOOTSTRAP_CHARACTERS} characters")
        registrations.bootstrap.append(text.strip())


class Doctor(_Points):
    def check(self, function):
        """function(universe: Universe) -> list of problems as sentences, for `mv2 doctor`. Usable as a decorator."""
        self._allow("doctor").doctor.append(function)
        return function


class Dashboard(_Points):
    def column(self, key, title, width, value, *, compact=False, flexible=False, group=None):
        """An agent column views can name as KEY (while unique) or PLUGIN:KEY.

        value(row) -> str for an agent row; group(row) -> str for a subverse row
        (blank without it). `compact` keeps it in narrow tables."""
        registrations = self._allow("dashboard.column")
        if not isinstance(key, str) or not key.replace("_", "").replace("-", "").isalnum():
            raise Error("A column key is letters, digits, hyphens and underscores")
        if not callable(value) or (group is not None and not callable(group)):
            raise Error("value and group must be functions of the row")
        if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= 120:
            raise Error("A column width is 1 to 120")
        registrations.columns[key] = loader.Column(key, str(title), width, value, bool(compact), bool(flexible), group)

    def panel(self, kind, render=None, *, title=None, auto=False):
        """A local view widget type a view names as `kind` (or PLUGIN:kind).

        render(PanelContext) returns Markdown, Table, Artifacts or a list of them
        (API 1.5): plain data the dashboard draws, refreshed with the agent list,
        off its event loop and within 2 s. With auto=True the panel also shows at
        the end of every scope's local view where the plugin is on, unless that
        view's "plugin_panels" leaves it out. `title` defaults to the plugin's name.

        The older form, render="module:Class" (a Panel class with parse and
        compose, which imports the dashboard), still works. Usable as a decorator factory."""
        if render is None:
            return lambda decorated: self.panel(kind, decorated, title=title, auto=auto)
        registrations = self._allow("dashboard.panel")
        if isinstance(render, str):
            registrations.panels[str(kind)] = _reference(render)
            return render
        if not callable(render):
            raise Error('A panel renders with a function of its context, or names "module:Class"')
        registrations.data_panels[str(kind)] = loader.DataPanel(str(kind), render,
                                                                 None if title is None else str(title), bool(auto))
        return render

    def inspector(self, function):
        """function(row) -> a line for the agent inspector, or None. Usable as a decorator."""
        self._allow("dashboard.inspector").inspector.append(function)
        return function

    def theme(self, colors):
        """A palette by theme.py name ({"BLUE": "#89b4fa"}), used where plugins.json names this plugin as `theme`."""
        registrations = self._allow("dashboard.theme")
        if not isinstance(colors, dict):
            raise Error("A theme maps color names to #rrggbb values")
        registrations.theme = dict(colors)

    def settings(self, fields=None, *, screen=None):
        """This plugin's settings in the dashboard, opened from the plugin settings screen (P) (API 1.5).

        Without this, the dashboard builds fields from the plugin's settings
        schema. `fields` (a list of Field) chooses and labels them instead, with
        the same look and checks. `screen="module:Class"` is the plugin's own
        screen for settings fields cannot express, built as Class(root=scope,
        backend=app.backend); a string it dismisses with becomes the status line."""
        self._allow("dashboard.settings")
        if (fields is None) == (screen is None):
            raise Error("settings() takes either fields or screen")
        registrations = self._loaded.registrations
        if screen is not None:
            registrations.settings_screen = _reference(screen)
            return
        fields = list(fields)
        if not fields or not all(isinstance(item, Field) for item in fields):
            raise Error("settings(fields=...) takes a list of api.Field")
        if self._loaded.manifest.settings is None:
            raise Error(f'{self._loaded.name} shows settings fields but declares no "settings" in plugin.json')
        registrations.settings_fields = fields

    def action(self, name, function=None):
        """A command palette entry, "PLUGIN: name", for something that is not a setting (API 1.5).

        function(context) runs off the screen's event loop and may return a
        status line; context.row is a copy of the selected row (or None),
        context.scope the selected universe's Path. It runs only where the plugin
        is on. Usable as a decorator factory."""
        if function is None:
            return lambda decorated: self.action(name, decorated)
        registrations = self._allow("dashboard.action")
        if not isinstance(name, str) or not name.strip() or not callable(function):
            raise Error("action(name, function) needs a name and a function")
        registrations.actions[name.strip()] = function
        return function

    def screen(self, name, reference):
        """A Textual screen by "module:Class", opened from the command palette as "PLUGIN: NAME".

        The dashboard builds it as Class(root=Path, backend=app.backend): `root` is
        the selected universe, where the plugin is on. A screen does its file and
        tool work in workers, as the dashboard's own screens do."""
        self._allow("dashboard.screen").screens[str(name)] = _reference(reference)


@dataclass(frozen=True)
class Capability:
    """A capability's answer for one session: supported, unavailable or error, why, and how."""
    state: str
    reason: str = ""
    mechanism: str | None = None

    def __post_init__(self):
        if self.state not in ("supported", "unavailable", "error"):
            raise Error("A capability is supported, unavailable or error")


class Launch(_Points):
    def contribute(self, family, function=None):
        """function(LaunchInfo) -> Contribution or None, for every launch of a `family` harness
        (claude, codex or pi) in universes where the plugin is on. It runs in the process that
        launches, within 2 s; it may not set MV2_* variables. Usable as a decorator factory."""
        if function is None:
            return lambda decorated: self.contribute(family, decorated)
        registrations = self._allow("launch")
        if family not in ("claude", "codex", "pi") or not callable(function):
            raise Error("contribute(family, function): family is claude, codex or pi")
        registrations.launch.setdefault(family, []).append(function)
        return function


class Capabilities(_Points):
    def add(self, name, function=None):
        """A capability `mv2 capabilities` reports for every agent here: function(SessionInfo) -> Capability.
        Names mv2 answers itself are refused."""
        if function is None:
            return lambda decorated: self.add(name, decorated)
        registrations = self._allow("capabilities")
        if not isinstance(name, str) or not name or not callable(function):
            raise Error("add(name, function) needs a capability name and a function")
        registrations.capabilities[name] = lambda info: _capability(function(info))
        return function


def _capability(answer):
    if not isinstance(answer, Capability):
        raise TypeError("a capability function returns api.Capability")
    return answer.state, answer.reason, answer.mechanism


class Usage(_Points):
    def adjust(self, function):
        """function(SessionInfo, usage) -> usage: change what `mv2 get-usage` and the dashboard report,
        after the harness has measured it. Return the record unchanged when there is nothing to do."""
        self._allow("usage").usage.append(function)
        return function


class Statusline(_Points):
    def write(self, agent, name, text):
        """Show `text` as status-line item `name` for `agent` (an Agent, or its directory); empty removes it.
        The item must be declared in plugin.json `statusline`."""
        self._allow("statusline")
        if name not in {item.name for item in self._loaded.manifest.statusline}:
            raise Error(f'{self._loaded.name} writes status-line item {name!r}; declare it in plugin.json "statusline"')
        harness.write_item(agent.directory if isinstance(agent, Agent) else agent, name, text)


@dataclass(frozen=True)
class Event:
    """One recorded event, as a handler receives it. `age` is seconds since it happened, when it is read."""
    type: str
    seq: int
    at: str
    universe: "Universe"
    agent: dict | None = None  # {"id", "name"}
    launch: str | None = None
    data: dict = field(default_factory=dict)
    age: float = 0.0


class Events(_Points):
    def on(self, kind, function=None):
        """Run function(Event) in the plugin host for each event of `kind` (a type or a pattern such as
        "agent.*") in universes where the plugin is on. The manifest's `events` must subscribe to it,
        which is what makes mv2 record it. Handlers may see an event more than once (after a restart):
        make them idempotent, and skip old events by `event.age`. Usable as a decorator factory."""
        if function is None:
            return lambda decorated: self.on(kind, decorated)
        registrations = self._allow("events")
        import fnmatch
        if not any(fnmatch.fnmatchcase(kind, pattern) for pattern in self._loaded.manifest.events):
            raise Error(f'{self._loaded.name} handles {kind!r} events; subscribe to them in plugin.json "events"')
        if not callable(function):
            raise Error("on(kind, function) needs a function")
        registrations.handlers.append((kind, function))
        return function

    def emit(self, kind, data=None, universe=None, agent=None):
        """Record a PLUGIN.kind event in a universe's journal, for any plugin that subscribes to it."""
        from . import events
        if not isinstance(kind, str) or not kind or "." in kind:
            raise Error("emit(kind): a short name; it is recorded as PLUGIN.kind")
        events.emit(_root(universe), f"{self._loaded.name}.{kind}",
                    agent={"id": agent.id, "name": agent.name} if isinstance(agent, Agent) else agent, data=data)


@dataclass(frozen=True)
class Field:
    """One setting on a plugin's settings screen: where it lives in the file and how it is edited.

    path: dotted key ("default.budget"). kind: text, int, float, bool or choice
    (`options` its values). A blank field means the setting's default, shown as
    `placeholder`; clearing one removes the key."""
    path: str
    label: str
    kind: str = "text"
    options: tuple = ()
    placeholder: str = ""
    help: str = ""

    def __post_init__(self):
        if self.kind not in ("text", "int", "float", "bool", "choice"):
            raise Error("A field's kind is text, int, float, bool or choice")
        if self.kind == "choice" and not self.options:
            raise Error("A choice field needs its options")
        if not isinstance(self.path, str) or not self.path or self.path.startswith("schema"):
            raise Error("A field's path is a dotted settings key (not schema)")


@dataclass(frozen=True)
class Markdown:
    """A block of Markdown in a plugin panel. Links are not opened automatically."""
    text: str


@dataclass(frozen=True)
class Table:
    """A table in a plugin panel: column titles, and rows of the same length. Cells are literal text."""
    columns: tuple
    rows: tuple = ()


@dataclass(frozen=True)
class Artifact:
    """A file in a plugin panel's Artifacts: a path relative to the universe directory."""
    label: str
    path: str
    description: str = ""


@dataclass(frozen=True)
class Artifacts:
    items: tuple


@dataclass(frozen=True)
class PanelContext:
    """What a panel's render receives. `agents` are the scope tree's agents, empty without agents.read."""
    universe: "Universe"
    widget: dict
    agents: tuple
    plugins: frozenset


def blocks(result, title):
    """A render's result as the dashboard's widget objects ({"type": "markdown"|"table"|"artifacts", ...}).
    Raises Error naming what cannot be drawn."""
    items = result if isinstance(result, (list, tuple)) else [result]
    found = []
    for item in items:
        if isinstance(item, Markdown):
            found.append({"type": "markdown", "title": title, "text": str(item.text)})
        elif isinstance(item, Table):
            columns = [str(column) for column in item.columns]
            rows = [[str(cell) for cell in row] for row in item.rows]
            if not columns or any(len(row) != len(columns) for row in rows):
                raise Error("a Table needs columns, and rows of the same length")
            found.append({"type": "table", "title": title, "columns": columns, "rows": rows})
        elif isinstance(item, Artifacts):
            entries = []
            for entry in item.items:
                entry = Artifact(**entry) if isinstance(entry, dict) else entry
                if not isinstance(entry, Artifact):
                    raise Error("Artifacts holds Artifact(label, path, description) items")
                entries.append({"label": str(entry.label), "path": str(entry.path),
                                "description": str(entry.description)})
            found.append({"type": "artifacts", "title": title, "items": entries})
        else:
            raise Error(f"a panel renders Markdown, Table or Artifacts, not {type(item).__name__}")
    return found


def panel_context(loaded, root, widget, rows, plugins_on):
    """The PanelContext a data panel of `loaded` gets in the scope at `root`."""
    mv = Plugin(loaded)
    agents = tuple(Agent(dict(row), mv) for row in rows if "id" in row and "name" in row and "universe" in row) \
        if mv.can("agents.read") else ()
    return PanelContext(mv.universe(root), dict(widget or {}), agents, frozenset(plugins_on))


@dataclass
class ServiceContext:
    """What a service's start() receives: the universes the host runs for, and an event set when it must stop."""
    universes: list
    stopping: object  # threading.Event
    log: object = None


class Helpers(_Points):
    def command(self, name, *arguments, universe=None):
        """The argv that runs this plugin's helper action `name` (to start a sibling, e.g. a detached worker)."""
        import sys
        if name not in self._loaded.registrations.helpers:
            raise Error(f"{self._loaded.name} has no helper action {name!r}")
        scope = ["-u", str(_root(universe))] if universe is not None else []
        return [sys.executable, "-m", "multiverse.plugins", *scope, "run", self._loaded.name, name, *map(str, arguments)]

    def action(self, name, function=None):
        """`python -m multiverse.plugins run PLUGIN NAME ARGS...` runs function(ARGS) -> exit code.
        Harness extensions call mv2 this way. Usable as a decorator factory."""
        if function is None:
            return lambda decorated: self.action(name, decorated)
        registrations = self._allow("helper")
        if not isinstance(name, str) or not name or not callable(function):
            raise Error("action(name, function) needs a name and a function")
        registrations.helpers[name] = function
        return function


def _reference(value):
    if not isinstance(value, str) or ":" not in value:
        raise Error('Name dashboard code as "module:Class" inside the plugin')
    return value


class Files:
    """mv2's own file protocol, for a plugin's state: whole-file JSON writes and exclusive locks."""

    @staticmethod
    def read_json(path):
        """A JSON file's value; raises Error when it is missing or not JSON."""
        from ..store import read
        return _call(read, path)

    @staticmethod
    def write_json(path, value):
        """Write JSON so a reader sees the old or the new file, never part of one (parents are created)."""
        from ..store import write
        return _call(write, path, value)

    @staticmethod
    def lock(path):
        """An exclusive lock on `path` while the `with` block runs, across processes (it waits)."""
        from ..store import lock
        return lock(path)

    @staticmethod
    def now():
        """The time now, as mv2 records it (ISO 8601, UTC)."""
        from ..store import now
        return now()


files = Files()


class Text:
    """How mv2 shows text and numbers to people, so a plugin's screens and lines look like the rest (API 1.7)."""

    @staticmethod
    def clean(value):
        """Printable text only: terminal control characters become spaces."""
        from .. import display
        return display.clean(value)

    @staticmethod
    def tokens(value):
        """A token count as mv2 shows it: '82k', '8.2k', '1.0m' or a plain number."""
        from .. import display
        return display.tokens(value)


text = Text()


@dataclass(frozen=True)
class HarnessInfo:
    """A harness configured in a universe, as its launches see it."""
    name: str
    family: str          # claude, codex, pi, or "" for a custom command harness
    executable: str      # what a launch runs (harness.json's executable, else the default)
    unset_env: tuple     # variables a launch removes from its environment
    bridge: bool         # whether launches use mv2's bridge (an auto setting counts as on)
    settings: dict       # harness.json's settings, as written
    home: Path | None = None  # the harness's own data directory, where mv2 knows it (API 1.7):
                              # Claude's CLAUDE_CONFIG_DIR or ~/.claude, Codex's CODEX_HOME or ~/.codex,
                              # Pi's PI_CODING_AGENT_DIR when set; None otherwise


def _harness_home(family, environment=None):
    """A harness family's data directory as this process's environment names it, or None."""
    source = os.environ if environment is None else environment
    if family == "claude":
        from ..harnesses.claude import claude_home
        return claude_home(source)
    if family == "codex":
        return Path(source.get("CODEX_HOME") or "~/.codex").expanduser()
    if family == "pi" and source.get("PI_CODING_AGENT_DIR"):
        return Path(source["PI_CODING_AGENT_DIR"]).expanduser()
    return None


class SettingsAccess:
    """This plugin's `plugin-settings/NAME.json` in a universe."""

    def __init__(self, loaded):
        self._loaded = loaded

    def _universe(self, universe):
        return _root(universe)

    def read(self, universe=None):
        """The settings in effect: (values, problem). values is {} while problem says why they cannot be used."""
        found = settings_files.read(self._universe(universe), self._loaded.manifest,
                                    self._loaded.registrations.settings_check)
        return found.values, found.problem

    def edit(self, changes, universe=None):
        """Change only the named settings ({"default.budget": 60000}, or api.DELETE to remove one)."""
        if self._loaded.manifest.settings is None:
            raise Error(f'{self._loaded.name} declares no "settings" in plugin.json')
        found = _call(settings_files.edit, self._universe(universe), self._loaded.manifest, changes,
                      self._loaded.registrations.settings_check)
        return found.values

    def check(self, function):
        """function(settings), or function(settings, universe directory), raises ValueError naming what is
        wrong. It sees the whole file. Usable as a decorator."""
        self._loaded.registrations.settings_check = function
        return function

    def migrate(self, function):
        """function(data, source) brings older settings to this version, for `mv2 plugins migrate`.

        `source` is the settings version the file was written for (a number), or
        "legacy" for the pre-plugin file plugin.json names (settings.legacy). It
        returns the new settings (with "schema" set to the current version). For
        "legacy" it may return (settings, on): on (true or false) also turns the
        plugin on or off in that universe's plugins.json, where it says nothing
        yet. Usable as a decorator."""
        self._loaded.registrations.settings_migrate = function
        return function


def _root(universe):
    if isinstance(universe, Universe):
        return universe.path
    from .. import universe as universes
    return _call(universes.resolve, universe)


class Plugin:
    """What register(mv) receives: this plugin's identity, its extension points and the client."""

    def __init__(self, loaded):
        self._loaded = loaded
        manifest = loaded.manifest
        self.name, self.version, self.directory = manifest.name, manifest.version, manifest.directory
        self.cli, self.bootstrap, self.doctor = Cli(loaded), Bootstrap(loaded), Doctor(loaded)
        self.dashboard = Dashboard(loaded)
        self.settings = SettingsAccess(loaded)
        self.launch, self.capabilities, self.usage = Launch(loaded), Capabilities(loaded), Usage(loaded)
        self.statusline, self.helper = Statusline(loaded), Helpers(loaded)
        self.events = Events(loaded)

    def service(self, start, stop=None):
        """Long-running work in the plugin host: start(ServiceContext) runs in a thread of the plugin's own
        worker process until it returns or context.stopping is set; stop() is called when the host stops it.
        A service that crashes is restarted with backoff, at most 3 times in 5 minutes."""
        if "service" not in self._loaded.manifest.extends:
            raise Error(f'{self.name} registers a service but its plugin.json does not list "service" in "extends"')
        if not callable(start) or (stop is not None and not callable(stop)):
            raise Error("service(start, stop) needs functions")
        self._loaded.registrations.services.append((start, stop))
        return start

    @property
    def process(self):
        """cli, dashboard, host or helper."""
        return loader.process

    def can(self, permission):
        return permission in self._loaded.manifest.permissions

    def universe(self, path=None):
        """A universe: `path`, else the one this process works in (MV2_UNIVERSE, else the current directory)."""
        return Universe(_root(path), self)

    def agent_at(self, directory):
        """The agent whose directory (state/agents/<id>) this is, in whichever universe holds it (needs agents.read)."""
        directory = Path(directory).resolve()
        root = directory.parent.parent.parent
        if directory.parent.name != "agents" or not (root / "universe.json").is_file():
            raise Error(f"{directory} is not an agent's directory")
        return self.universe(root).agent(directory.name)

    def current_agent(self):
        """The agent this process runs for (MV2_AGENT_DIR, else MV2_AGENT_ID), or None outside an agent."""
        if os.environ.get("MV2_AGENT_DIR"):
            return self.agent_at(os.environ["MV2_AGENT_DIR"])
        identity = os.environ.get("MV2_AGENT_ID")
        return self.universe().agent(identity) if identity else None

    def harness_info(self, universe, harness):
        """HarnessInfo for a harness configured in a universe (its harnesses/NAME/harness.json)."""
        from .. import harnesses
        root = _root(universe)
        adapter = _call(harnesses.load, root, harness)
        spec = getattr(adapter, "spec", {}) or {}
        settings = getattr(adapter, "settings", {}) or {}
        executable = (spec.get("spawn") or [None])[0]
        family = getattr(adapter, "family", "") or ""
        return HarnessInfo(harness, family, executable, tuple(spec.get("unset_env", ())),
                           bool(settings.get("inbox_bridge")), dict(settings), _harness_home(family))

    def state_dir(self, universe=None):
        """state/plugins/NAME/ of a universe, created on first use."""
        directory = _root(universe) / "state" / "plugins" / self.name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def agent_state_dir(self, agent):
        """state/agents/<id>/NAME/ of an agent, created on first use."""
        directory = agent.universe.path / "state" / "agents" / agent.id / self.name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def log(self, text, universe=None):
        """Append a line to state/plugins/NAME/plugin.log."""
        from ..store import now
        try:
            with (self.state_dir(universe) / "plugin.log").open("a") as handle:
                handle.write(f"{now()} {text}\n")
        except (OSError, ValueError):
            pass

    def _need(self, permission):
        if not self.can(permission):
            raise Error(f'{self.name} did not ask for permission {permission!r} in plugin.json')


class Universe:
    """A universe or subverse, as a plugin sees it."""

    def __init__(self, path, plugin):
        self.path, self._plugin = Path(path), plugin

    def __repr__(self):
        return f"Universe({str(self.path)!r})"

    def __eq__(self, other):
        return isinstance(other, Universe) and other.path.resolve() == self.path.resolve()

    def __hash__(self):
        return hash(self.path.resolve())

    @property
    def _meta(self):
        from ..store import read
        return _call(read, self.path / "universe.json")

    @property
    def id(self):
        return self._meta["id"]

    @property
    def name(self):
        return self._meta["name"]

    @property
    def parent(self):
        from .. import universe as universes
        found = universes.parent(self.path)
        return Universe(found, self._plugin) if found else None

    def subverses(self):
        from .. import universe as universes
        return [Universe(child, self._plugin) for child in universes.children(self.path)]

    def agents(self, recursive=False):
        """Agents with their state and reported work (needs agents.read)."""
        self._plugin._need("agents.read")
        from ..tools import list_agents
        return [Agent(row, self._plugin) for row in _call(list_agents.run, self.path, recursive=recursive, json=True)]

    def agent(self, reference):
        """One agent by name, unique ID prefix or ID (needs agents.read)."""
        self._plugin._need("agents.read")
        from ..universe import agents
        identity = _call(agents.directory, self.path, reference).name
        for found in self.agents():
            if found.id == identity:
                return found
        raise Error(f"No agent named {reference} here")

    def spawn(self, name, *, harness="codex", task=None, model=None, wait=False):
        """Start a new agent (needs agents.control); returns it."""
        self._plugin._need("agents.control")
        from ..tools import spawn
        _call(spawn.run, self.path, name=name, task=task, harness=harness, model=model, wait=wait)
        return self.agent(name)

    def catalog(self):
        """Skills, commands, workflows and harnesses here: [{kind, name, description, path}]."""
        from ..universe import catalog
        return _call(catalog.catalog, self.path)

    def command(self, name, arguments=""):
        """A command, workflow or skill's prompt text with `arguments` appended, as `mv2 command` prints it."""
        from ..universe import catalog
        return _call(catalog.command, self.path, name, arguments)

    def refresh(self, apply=False):
        """The template refresh plan, applied when `apply` (needs universe.write to apply)."""
        if apply:
            self._plugin._need("universe.write")
        from ..tools import refresh
        return _call(refresh.run, self.path, apply=apply, json=True)

    def doctor(self):
        """mv2 doctor's result: {ok, problems} (needs agents.read)."""
        self._plugin._need("agents.read")
        from ..tools import doctor
        return _call(doctor.run, self.path)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    delivery: str
    state: str
    reason: str = ""
    record: dict = field(default_factory=dict)


class Agent:
    """One agent, as the listing showed it when it was read; `reload()` reads it again."""

    def __init__(self, row, plugin):
        self.record, self._plugin = dict(row), plugin
        self.id, self.name = row["id"], row["name"]
        self.harness = row.get("harness")
        self.live = row.get("live", "unknown")  # alive, ending, stopped, dormant or unknown
        self.status = row.get("status", "unknown")  # working, waiting, idle, disconnected or unknown
        self.phase, self.summary, self.task = row.get("phase"), row.get("summary", ""), row.get("task", "")
        self.universe = Universe(row["universe"], plugin)

    def __repr__(self):
        return f"Agent({self.name!r}, live={self.live!r}, status={self.status!r})"

    @property
    def directory(self):
        return self.universe.path / "state" / "agents" / self.id

    def reload(self):
        return self.universe.agent(self.id)

    def session(self):
        """This agent's session as the harness side sees it: native ID, launch, state, native mode and plugins."""
        family = ""
        try:
            family = self._plugin.harness_info(self.universe, self.harness).family
        except Error:
            pass
        runtime = self.record.get("runtime") or {}
        return SessionInfo(self.universe.path, self.id, self.harness or "", family,
                           self.record.get("native_session_id"), runtime.get("launch_id"),
                           runtime.get("state", "unknown"), runtime.get("native_mode"), runtime.get("plugins"))

    def terminal(self, lines=30):
        """The last `lines` lines of the agent's terminal, or why there is none (needs agents.read)."""
        self._plugin._need("agents.read")
        from ..tools import inspect
        capture = _call(inspect.run, self.universe.path, agent=self.id)
        return "\n".join(inspect.tail(capture, lines)) if isinstance(capture, str) else str(capture)

    def usage(self):
        self._plugin._need("agents.read")
        from ..tools import get_usage
        return _call(get_usage.run, self.universe.path, agent=self.id)

    def capabilities(self):
        self._plugin._need("agents.read")
        from ..tools import capabilities
        return _call(capabilities.run, self.universe.path, agent=self.id)

    def resume(self, *, wait=False):
        self._plugin._need("agents.control")
        from ..tools import resume
        return _call(resume.run, self.universe.path, agent=self.id, wait=wait, json=True)

    def kill(self):
        self._plugin._need("agents.control")
        from ..tools import kill
        return _call(kill.run, self.universe.path, agent=self.id)

    def send(self, text, delivery="steer"):
        """Send as this plugin: the agent reads "Message from plugin NAME" (needs messages.send).

        steer and follow-up wake a stopped or dormant agent; inbox-only never does."""
        self._plugin._need("messages.send")
        from ..tools import send
        sender = {"name": f"plugin {self._plugin.name}", "plugin": self._plugin.name, "agent_id": None}
        record = _call(send.run, self.universe.path, agent=self.id, text=text, sender=sender, delivery=delivery)
        native = record.get("native_delivery") or {}
        return SendResult(bool(record.get("ok", True)), record.get("delivery", delivery), str(record.get("state", "")),
                          str(record.get("reason") or native.get("reason") or ""), record)

    def ask(self, question):
        """A side question answered from the agent's conversation, never added to it (needs messages.ask)."""
        self._plugin._need("messages.ask")
        from ..tools import ask
        result = _call(ask.run, self.universe.path, agent=self.id, question=question)
        if result.get("state") != "ok":
            raise Error(result.get("reason") or "The agent did not answer")
        return (result.get("value") or {}).get("answer", "")

    def inbox(self):
        """Unread inbox-only messages, left unread (needs messages.read): [{at, from, text, delivery}]."""
        self._plugin._need("messages.read")
        from ..universe import mailbox
        found = _call(mailbox.inbox, self.directory, limit=None, peek=True)
        return [{key: message[key] for key in ("at", "from", "text", "delivery") if key in message}
                for message in found["messages"]]

    def history(self, last=20):
        """The last messages, read or not, oldest first (needs messages.read)."""
        self._plugin._need("messages.read")
        from ..universe import mailbox
        found = _call(mailbox.inbox, self.directory, limit=None, all_messages=True, peek=True)
        return [{key: message[key] for key in ("at", "from", "text", "delivery") if key in message}
                for message in found["messages"][-last:]]


def current_agent(plugin):
    """The agent this process runs for, or None outside an agent (kept for API 1.0 callers)."""
    return plugin.current_agent()
