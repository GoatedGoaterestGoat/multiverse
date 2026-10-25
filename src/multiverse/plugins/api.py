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
from contextlib import contextmanager, ExitStack
import functools
from dataclasses import dataclass, field, fields
import os
from pathlib import Path

from . import access as levels, agent_files, harness, loader, settings as settings_files
from .harness import Contribution, LaunchInfo, SessionInfo

VERSION = loader.API_VERSION
DELETE = settings_files.DELETE
BOOTSTRAP_LINES = 2
BOOTSTRAP_CHARACTERS = 300


class Error(ValueError):
    """An operation a plugin asked for could not be done; the message says why."""


class Gone(Error, OSError):
    """A write was for an agent or a universe that is gone (removed, or moved): nothing of it is made again, so a
    late write never brings it back as an orphan. Stop working for it. Also an OSError, which is what
    agent_state_dir, state_dir and files.lock raised before 1.10, so code that catches that still does."""


class AgentGone(Gone):
    """The agent a write was for is gone: its folder (state/agents/<id>) was removed or moved."""


class UniverseGone(Gone):
    """The universe a write was for is gone: its root was removed or moved."""


class AccessDenied(Error):
    """A call above the access level this plugin has in that universe (none where it is off), or above the
    class its own code declared (effect=, mv.effect) (1.13). Never an OSError: an except OSError written for
    disk errors does not swallow it."""


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Error:
        raise
    except levels.Denied as error:
        raise AccessDenied(str(error)) from None
    except agent_files.Gone as error:
        raise _gone(error) from error
    except OSError as error:
        raise _os_error(error) from error
    except (ValueError, KeyError, RuntimeError) as error:
        raise Error(str(error)) from error


_OS_ERRORS = {}  # an OSError class -> the api.Error that is also it


def _os_error(error):
    """The api.Error for a filesystem or system failure, which is also the OSError it was (its class, errno and
    file names): what state_dir and agent_state_dir raised in 1.9, so a plugin's except OSError still catches it."""
    kind = type(error)
    twin = _OS_ERRORS.get(kind)
    if twin is None:
        try:
            twin = type(kind.__name__, (kind, Error), {"__module__": __name__})  # OSError first: it parses errno
        except TypeError:  # an OSError class that cannot be combined: the plain one
            twin = _OS_ERRORS.get(OSError) or type("OSError", (OSError, Error), {"__module__": __name__})
            _OS_ERRORS[OSError] = twin
        twin = _OS_ERRORS.setdefault(kind, twin)
    try:
        if error.errno is not None:
            return twin(error.errno, error.strerror, error.filename, None, error.filename2)
        return twin(*error.args)
    except Exception:  # a class with a constructor of its own
        plain = _os_error(OSError(error.errno, error.strerror)) if error.errno is not None else None
        return plain if plain is not None else Error(str(error))


def _effect(value):
    try:
        return levels.check(value, "effect")
    except ValueError as error:
        raise Error(str(error)) from None


def _declared(loaded, effect, label, function):
    """`function`, run under the class its registration declared (an observe column can't send)."""
    @functools.wraps(function)  # its signature too: mv2 reads a settings check's to pass the universe or not
    def declared(*arguments, **keywords):
        with levels.within(effect, f"{loaded.name}'s {label}", declared=True):
            return function(*arguments, **keywords)
    return declared


def _level_here(loaded, root):
    """The access level `loaded` has in universe `root` (None where it is off or not seen there)."""
    try:
        return loader.access_level(root, loader.entry(root, loaded.name))
    except (loader.PluginError, OSError, ValueError):
        return None


def _contribution(loaded, effect, label, function, root_of):
    """A contribution core runs for a person (a command, an action, a helper): refused before it runs when its
    declared class is above the plugin's level in that universe, else run under both."""
    @functools.wraps(function)
    def contributed(*arguments, **keywords):
        root = root_of(arguments, keywords)
        have = _level_here(loaded, root)
        if have is None:
            raise AccessDenied(f"{loaded.name} is off here; its {label} needs {effect}. "
                               f"Turn it on with: mv2 plugins enable {loaded.name}")
        if levels.rank(have) < levels.rank(effect):
            raise AccessDenied(f"{loaded.name} has {have} access here; its {label} needs {effect}. "
                               f"A person can grant it with: mv2 plugins grant {loaded.name} {effect}")
        with levels.within(have, loaded.name), levels.within(effect, f"{loaded.name}'s {label}", declared=True):
            return function(*arguments, **keywords)
    return contributed


def _gone(error):
    return (UniverseGone if isinstance(error, agent_files.UniverseGone) else AgentGone)(str(error))


class _Points:
    """One group of extension points; each method checks the manifest's `extends` first."""

    def __init__(self, loaded):
        self._loaded = loaded

    def _allow(self, point):
        if point not in self._loaded.manifest.extends:
            raise Error(f'{self._loaded.name} registers {point} but its plugin.json does not list it in "extends"')
        return self._loaded.registrations


class Cli(_Points):
    def command(self, name, configure, run, cli_run=None, *, effect="act"):
        """An `mv2 NAME` command, shaped like the tools: configure(parser), run(root, **arguments)
        returning a record, and optionally cli_run(root, **arguments) returning readable text.
        `effect` (1.13) is its class: observe, spend or act (the default). It runs only where the plugin's
        level reaches it, and an API call above it, made inside it, is refused."""
        effect = _effect(effect)
        registrations = self._allow("cli")
        if name not in self._loaded.manifest.commands:
            raise Error(f'{self._loaded.name} registers command {name!r}; declare it in plugin.json "commands"')
        if not callable(configure) or not callable(run) or (cli_run is not None and not callable(cli_run)):
            raise Error("configure, run and cli_run must be functions")
        root_of = lambda arguments, keywords: Path(arguments[0] if arguments else keywords.get("root") or _root(None))
        label = f"command {name}"
        registrations.commands[name] = loader.Command(
            name, configure, _contribution(self._loaded, effect, label, run, root_of),
            _contribution(self._loaded, effect, label, cli_run, root_of) if cli_run is not None else None)


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
        label = f"column {key}"  # drawn on every refresh: observe, so a column can't act
        registrations.columns[key] = loader.Column(
            key, str(title), width, _declared(self._loaded, "observe", label, value), bool(compact), bool(flexible),
            _declared(self._loaded, "observe", label, group) if group is not None else None)

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
        registrations.data_panels[str(kind)] = loader.DataPanel(
            str(kind), _declared(self._loaded, "observe", f"panel {kind}", render),
            None if title is None else str(title), bool(auto))
        return render

    def inspector(self, function):
        """function(row) -> a line for the agent inspector, or None. Usable as a decorator."""
        self._allow("dashboard.inspector").inspector.append(_declared(self._loaded, "observe", "inspector line",
                                                                      function))
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

    def action(self, name, function=None, *, effect="act"):
        """A command palette entry, "PLUGIN: name", for something that is not a setting (API 1.5).

        function(context) runs off the screen's event loop and may return a
        status line; context.row is a copy of the selected row (or None),
        context.scope the selected universe's Path. It runs only where the plugin
        is on, and where its level reaches `effect` (1.13; act unless declared).
        Usable as a decorator factory."""
        if function is None:
            return lambda decorated: self.action(name, decorated, effect=effect)
        effect = _effect(effect)
        registrations = self._allow("dashboard.action")
        if not isinstance(name, str) or not name.strip() or not callable(function):
            raise Error("action(name, function) needs a name and a function")
        root_of = lambda arguments, keywords: Path(getattr(arguments[0], "scope", None) or _root(None))
        registrations.actions[name.strip()] = _contribution(self._loaded, effect, f"action {name.strip()}", function,
                                                            root_of)
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
        declared = _declared(self._loaded, "observe", f"capability {name}", function)
        registrations.capabilities[name] = lambda info: _capability(declared(info))
        return function


def _capability(answer):
    if not isinstance(answer, Capability):
        raise TypeError("a capability function returns api.Capability")
    return answer.state, answer.reason, answer.mechanism


class Usage(_Points):
    def adjust(self, function):
        """function(SessionInfo, usage) -> usage: change what `mv2 get-usage` and the dashboard report,
        after the harness has measured it. Return the record unchanged when there is nothing to do."""
        self._allow("usage").usage.append(_declared(self._loaded, "observe", "usage adjustment", function))
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


def _blocks(result, title):
    """For core (plugins.panel_blocks), not plugins: a render's result as the dashboard's widget objects ({"type": "markdown"|"table"|"artifacts", ...}).
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


def _panel_context(loaded, root, widget, rows, plugins_on):
    """For core (plugins.panel_context), not plugins: the PanelContext a data panel of `loaded` gets at `root`."""
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

    def action(self, name, function=None, *, effect="act"):
        """`python -m multiverse.plugins run PLUGIN NAME ARGS...` runs function(ARGS) -> exit code.
        Harness extensions call mv2 this way. It runs only where the plugin's level reaches `effect`
        (1.13; act unless declared), in the universe the helper runs for. Usable as a decorator factory."""
        if function is None:
            return lambda decorated: self.action(name, decorated, effect=effect)
        effect = _effect(effect)
        registrations = self._allow("helper")
        if not isinstance(name, str) or not name or not callable(function):
            raise Error("action(name, function) needs a name and a function")
        registrations.helpers[name] = _contribution(self._loaded, effect, f"helper action {name}", function,
                                                    lambda arguments, keywords: _root(None))
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
        """Write JSON so a reader sees the old or the new file, never part of one. Its missing parents are made,
        but never an agent's folder (state/agents/<id>) or, for a plugin's state (state/plugins/...), its
        universe's root: AgentGone or UniverseGone for one that is gone, and nothing is made."""
        return _call(agent_files.write_json, path, value)

    @staticmethod
    @contextmanager
    def lock(path):
        """An exclusive lock on `path` while the `with` block runs, across processes (it waits). Its missing
        parents are made, as for write_json: AgentGone or UniverseGone for one that is gone."""
        with ExitStack() as stack:
            try:
                stack.enter_context(agent_files.lock(path))
            except agent_files.Gone as error:
                raise _gone(error) from error
            yield

    @staticmethod
    def ensure_dir(path):
        """Directory `path`, with its missing parents; returns it. Never an agent's folder or, for a plugin's state,
        its universe's root: below one, only while it exists (AgentGone or UniverseGone once it is gone). Use it,
        not a mkdir with parents, for anything under an agent or a plugin's state."""
        return _call(agent_files.ensure_dir, path)

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
                              # Pi's PI_CODING_AGENT_DIR when set; None otherwise. As this process's
                              # environment names it, or, asked for an agent (1.12), as its launches do
    storage: dict | None = None  # asked for an agent (1.12): its recorded storage variables for this harness,
                                 # each name to its value, or to None where the launch had it unset; None when
                                 # not asked for an agent, never launched, or not proven


def _agent_storage(agent, harness, family, keys):
    """(home, storage) of an agent's harness as its launches use them, from its records only.

    storage: the environment that selects the same store as the agent's launches: its storage variables (`keys`)
    as the shared evidence records them (universe.storage: the storage map, or the legacy launch evidence a resume
    adopts), each to its value or to None where the launch had it unset, with what resolves them (see
    _replayable); None when that isn't proven. A value and an absence stay apart even where they name one
    directory: an explicit CLAUDE_CONFIG_DIR moves Claude's login and global config.

    home: the session home core recorded, else
    the storage its next resume would replay (universe.storage, which lifecycle shares: the storage map, or the
    legacy launch evidence a resume adopts; a variable recorded as unset is its default under the launch's HOME,
    which must be recorded too). None (unknown) when a launched agent has no such proof, or a record can't be
    read. Only an agent that was never launched (no launch evidence of any kind: see _never_launched), so
    nothing of it in any store yet, gets this process's environment."""
    if not isinstance(agent, Agent):
        hint = f"mv.universe(universe).agent({agent!r})" if isinstance(agent, str) else "mv.universe(universe).agent(NAME)"
        raise Error(f"agent must be an Agent, not {type(agent).__name__}: pass {hint}")
    if agent.harness != harness:
        raise Error(f"{agent.name} runs harness {agent.harness!r}, not {harness!r}")
    from ..store import read
    try:
        properties = read(agent.directory / "properties.json")
    except (OSError, ValueError):
        return None, None
    if not isinstance(properties, dict):
        return None, None
    from ..universe import storage
    recorded = storage.recorded_environment(agent.directory, properties, family, keys)
    values = (_replayable(family, recorded, keys, properties.get("workspace"))
              if recorded is not None and keys and all(key in recorded for key in keys) else None)
    home = properties.get("native_session_home")
    if isinstance(home, str) and home:
        return Path(home), values
    if recorded is not None:
        if values is None:
            return None, None  # a value whose context isn't recorded: its home isn't known either
        return _recorded_home(family, {key: value for key, value in values.items() if value}), values
    if _never_launched(agent.directory, properties):
        return _harness_home(family), None  # nothing of it in any store yet: where this process's would put it
    return None, None


_HOME_VARIABLES = {"claude": ("CLAUDE_CONFIG_DIR", ".claude"), "codex": ("CODEX_HOME", ".codex"),
                   "pi": ("PI_CODING_AGENT_DIR", None)}


def _replayable(family, recorded, keys, workspace):
    """The storage variables `keys` from `recorded`, as an environment that selects the same store wherever it is
    replayed, or None when what resolves them isn't recorded. A relative value is joined, lexically as Claude's
    path.resolve does (no symlink resolution), to the directory it was set from: the agent's recorded workspace,
    where its launches start. An unset (None) or empty home variable stays as recorded and brings the launch's
    proven HOME, under which its default lies. Unset, empty and set stay apart."""
    variable = _HOME_VARIABLES.get(family, (None, None))[0]
    result = {}
    for key in keys:
        value = recorded[key]
        if isinstance(value, str) and value and not os.path.isabs(value):
            if not (isinstance(workspace, str) and os.path.isabs(workspace)):
                return None
            value = os.path.normpath(os.path.join(workspace, value))
        result[key] = value
    if variable in result and not result[variable]:
        if not recorded.get("HOME"):
            return None
        result["HOME"] = recorded["HOME"]
    return result


def _recorded_home(family, recorded):
    """A family's home from recorded launch values alone: its variable, else its default under the recorded
    launch HOME; None when neither is recorded (never this process's HOME or ~)."""
    variable, default = _HOME_VARIABLES.get(family, (None, None))
    if variable and recorded.get(variable):
        return Path(recorded[variable])
    if default and recorded.get("HOME"):
        return Path(recorded["HOME"]) / default
    return None


_LAUNCH_PROPERTIES = ("native_session_id", "native_session_home", "native_storage_environment",
                      "native_service_identity", "native_environment")


def _never_launched(directory, properties):
    """Whether an agent has no launch evidence of any kind: none of the launch properties, a runtime record that
    is missing or empty, no session journal, and no launch records. An older plain launch may have records but no
    bound session, and a damaged runtime is still a launch, so no one of these alone decides it."""
    if any(properties.get(key) not in (None, "", {}) for key in _LAUNCH_PROPERTIES):
        return False
    from ..store import read
    directory = Path(directory)
    try:
        runtime = directory / "runtime.json"
        if runtime.exists() and read(runtime) != {}:
            return False
        if (directory / "sessions.jsonl").exists():
            return False
        launches = directory / "launches"
        return not launches.exists() or (launches.is_dir() and not any(launches.iterdir()))
    except (OSError, ValueError):
        return False


def _harness_home(family, environment=None):
    """A harness family's data directory as this process's environment names it (or `environment`), or None."""
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
        root = self._universe(universe)
        with _acting(self._loaded, "act", root, "editing its settings"):
            found = _call(settings_files.edit, root, self._loaded.manifest, changes,
                          self._loaded.registrations.settings_check)
        return found.values

    def check(self, function):
        """function(settings), or function(settings, universe directory), raises ValueError naming what is
        wrong. It sees the whole file. Usable as a decorator."""
        self._loaded.registrations.settings_check = _declared(self._loaded, "observe", "settings check", function)
        return function

    def migrate(self, function):
        """function(data, source) brings older settings to this version: on first use, and for `mv2 plugins migrate`.

        `source` is the settings version the file was written for (a number), or
        "legacy" for the pre-plugin file plugin.json names (settings.legacy). It
        returns the new settings (with "schema" set to the current version). For
        "legacy" it may return (settings, on): on (true or false) also sets the
        plugin's switch in that universe's plugins.json, whatever it said before.
        It runs with that universe as the API's. Usable as a decorator."""
        self._loaded.registrations.settings_migrate = _declared(self._loaded, "observe", "settings migration",
                                                                function)
        return function


@contextmanager
def _acting(loaded, level, universe, what):
    """Run an API call of class `level` for `loaded` in `universe`: refused (AccessDenied) above its level
    there or above its own declared class; core's dispatch inside sees the same limit."""
    root = universe.path if isinstance(universe, Universe) else Path(universe)
    with levels.within(_level_here(loaded, root), loaded.name):
        try:
            levels.require(level, what)
        except levels.Denied as error:
            raise AccessDenied(str(error)) from None
        yield


class _Effect:
    """mv.effect(level): the class of the plugin's own functions, as a decorator or a with-block (1.13)."""

    def __init__(self, loaded, level):
        self._loaded, self.level = loaded, _effect(level)
        self._stack = []

    def __call__(self, function):
        @functools.wraps(function)
        def declared(*arguments, **keywords):
            with levels.within(self.level, f"{self._loaded.name}'s {getattr(function, '__name__', 'code')}",
                               declared=True):
                return function(*arguments, **keywords)
        return declared

    def __enter__(self):
        context = levels.within(self.level, f"{self._loaded.name}'s code", declared=True)
        context.__enter__()
        self._stack.append(context)
        return self

    def __exit__(self, *details):
        return self._stack.pop().__exit__(*details)


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
        return self._loaded.manifest.permitted(permission)

    def access(self, universe=None):
        """The access level this plugin has in `universe` (1.13): observe, spend or act, or None where it is off.
        Register or offer only what fits it."""
        return _level_here(self._loaded, _root(universe))

    def effect(self, level):
        """Declare the class of the plugin's own code (1.13): `@mv.effect("observe")` on a function, or
        `with mv.effect("observe"):`. An API call above it, made inside, is refused. It follows a context
        variable, not threads the plugin starts itself."""
        return _Effect(self._loaded, level)

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

    def harness_info(self, universe, harness, agent=None):
        """HarnessInfo for a harness configured in a universe (its harnesses/NAME/harness.json). With `agent`
        (an Agent of that harness; 1.12), `home` is the one that agent's launches use, from its records, not
        this process's environment, which a dashboard or the plugin host may not share with it."""
        from .. import harnesses
        root = _root(universe)
        # Without an agent it reads the universe's own harness definitions, which a plugin needs to configure
        # itself before it is turned on: outside the classes. For an agent it reads that agent's records: observe.
        with _acting(self._loaded, "observe", root, "harness_info for an agent") if agent is not None else ExitStack():
            adapter = _call(harnesses.load, root, harness)
        spec = getattr(adapter, "spec", {}) or {}
        settings = getattr(adapter, "settings", {}) or {}
        executable = (spec.get("spawn") or [None])[0]
        family = getattr(adapter, "family", "") or ""
        home, recorded = ((_harness_home(family), None) if agent is None
                          else _agent_storage(agent, harness, family, tuple(getattr(adapter, "storage_env", ()))))
        return HarnessInfo(harness, family, executable, tuple(spec.get("unset_env", ())),
                           bool(settings.get("inbox_bridge")), dict(settings), home, recorded)

    def state_dir(self, universe=None):
        """state/plugins/NAME/ of a universe, made on first use while the universe exists; UniverseGone once it
        was removed or moved (its root is never made again)."""
        root = _root(universe)
        return _call(agent_files.ensure_dir, root / "state" / "plugins" / self.name, root=root)

    def agent_state_dir(self, agent):
        """state/agents/<id>/NAME/ of an agent, made on first use while the agent's folder exists; AgentGone once
        it was removed or moved (its folder is never made again). Make what goes below it with
        api.files.ensure_dir, write_json or lock, never a mkdir with parents, which would bring a removed agent
        back as an orphan."""
        return _call(agent_files.ensure_dir, agent.universe.path / "state" / "agents" / agent.id / self.name)

    def log(self, text, universe=None):
        """Append a line to state/plugins/NAME/plugin.log; for a universe that is gone, nothing is written."""
        from ..store import now
        try:
            loader.append_log(self.state_dir(universe) / "plugin.log", f"{now()} {text}")
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

    def harness(self, name):
        """A harness configured here (1.13): .info is what mv.harness_info(universe, name) gives, and
        .models() and .check() say which models it takes. An unknown name raises api.Error."""
        return Harness(self, name, self._plugin.harness_info(self, name))

    def agents(self, recursive=False):
        """Agents with their state and reported work (needs agents.read)."""
        self._plugin._need("agents.read")
        from ..tools import list_agents
        with _acting(self._plugin._loaded, "observe", self, "listing agents"):
            return [Agent(row, self._plugin) for row in _call(list_agents.run, self.path, recursive=recursive,
                                                              json=True)]

    def agent(self, reference):
        """One agent by name, unique ID prefix or ID (needs agents.read)."""
        self._plugin._need("agents.read")
        from ..runtime import lifecycle
        from ..universe import agents
        with _acting(self._plugin._loaded, "observe", self, "reading an agent"):
            identity = _call(agents.directory, self.path, reference).name
            # Just this agent's row, built by the listing's own code: no other agent is observed. A record whose
            # own ID is not its directory's is not this agent, so it is never returned as one.
            rows = _call(lifecycle.listing, self.path, only=identity)
        for row in rows:
            if row.get("id") == identity:
                return Agent(row, self._plugin)
        raise Error(f"No agent named {reference} here")

    def spawn(self, name, *, harness="codex", task=None, model=None, wait=False):
        """Start a new agent (needs agents.control); returns it."""
        self._plugin._need("agents.control")
        from ..tools import spawn
        with _acting(self._plugin._loaded, "act", self, "spawn"):
            _call(spawn.run, self.path, name=name, task=task, harness=harness, model=model, wait=wait)
        return self.agent(name)

    def catalog(self):
        """Skills, commands, workflows and harnesses here: [{kind, name, description, path}]."""
        from ..universe import catalog
        with _acting(self._plugin._loaded, "observe", self, "reading the catalog"):
            return _call(catalog.catalog, self.path)

    def command(self, name, arguments=""):
        """A command, workflow or skill's prompt text with `arguments` appended, as `mv2 command` prints it."""
        from ..universe import catalog
        with _acting(self._plugin._loaded, "observe", self, "reading a command"):
            return _call(catalog.command, self.path, name, arguments)

    def refresh(self, apply=False):
        """The template refresh plan, applied when `apply` (needs universe.write to apply)."""
        if apply:
            self._plugin._need("universe.write")
        from ..tools import refresh
        with _acting(self._plugin._loaded, "act" if apply else "observe", self, "applying a refresh" if apply else "a refresh plan"):
            return _call(refresh.run, self.path, apply=apply, json=True)

    def doctor(self):
        """mv2 doctor's result: {ok, problems} (needs agents.read)."""
        self._plugin._need("agents.read")
        from ..tools import doctor
        with _acting(self._plugin._loaded, "observe", self, "doctor"):
            return _call(doctor.run, self.path)


class TranscriptChanged(Error):
    """The transcript changed under a cursor (1.13). `reason`: "replaced" (the agent is on another generation
    now, after a /clear; `resume` is its start), "rewritten" (the bytes before the cursor changed; `resume` is the
    same place found again by the nearest ref, or None), or "diverged" (a history read's cursor row is no longer
    on the history; `resume` is after the last row both histories share). `generation` is the current one. Later
    1.x minors may add reasons; treat an unknown one as "replaced"."""

    def __init__(self, message, *, reason, resume=None, generation=None):
        super().__init__(message)
        self.reason, self.resume, self.generation = reason, resume, generation


class TranscriptUnavailable(Error):
    """The transcript can't be read now (1.13). `retry`: True when the agent's identity was busy or changing, so
    trying again soon may work; False when it stays unavailable until something is fixed (the message says what)."""

    def __init__(self, message, *, retry=False):
        super().__init__(message)
        self.retry = retry


@dataclass(frozen=True, kw_only=True)
class Generation:
    """An earlier session this agent was on (a /clear, a fork, a fresh start), from its session lineage (1.13)."""
    generation: str                # token, accepted as generation= while readable
    native_id: str
    reason: str                    # the lineage's reason: "clear", "detected", "missing-conversation", ...
    ended_at: str                  # when the agent left it
    readable: bool | None          # its transcript can still be found; None: can't tell


@dataclass(frozen=True, kw_only=True)
class TranscriptInfo:
    """What `agent.transcript()` says about one generation of an agent's transcript (1.13). Cheap: a stat and the
    index's counts. Keep `end` and `history_version` with what you parsed, and skip work while both are
    unchanged."""
    state: str                     # "supported", "unavailable" or "error"; the rest is empty unless supported
    reason: str = ""
    format: str | None = None      # "claude-jsonl"
    version: str | None = None     # the harness version that wrote the newest row, where stored
    native_id: str | None = None
    generation: str | None = None  # opaque token: this native session in its recorded storage
    start: str | None = None       # cursor before the first row
    end: str | None = None         # cursor after the last complete row: unchanged means nothing new
    rows: int | None = None
    bytes: int | None = None
    updated_at: str | None = None
    tip: str | None = None         # the tip the counts below used (None: the newest leaf)
    gap: str | None = None         # ref of a parent the history needs and the harness never stored
    turns: int | None = None
    context_rows: int | None = None
    context_turns: int | None = None
    context_start: str | None = None   # cursor before the first row in context (== start without a compaction)
    history_version: int | None = None  # rises when the history changes other than by growing
    stable_rows: bool = True       # a complete row never changes in place (it may be removed)
    earlier: tuple = ()            # Generation, oldest first


@dataclass(frozen=True, kw_only=True)
class Row:
    """One stored row of a transcript (1.13)."""
    cursor: str                    # the position after this row: after=row.cursor continues from it
    ref: str | None                # the harness's own ID for it (Claude's uuid); some rows have none
    kind: str                      # the stored type: "user", "assistant", "system/compact_boundary", ...
    label: str                     # prompt, mid_turn, tool_result, command, command_output, injected, compaction,
                                   # response or meta; treat a label a later minor adds as meta
    source: str | None = None      # prompt and mid_turn: who (human, plugin, peer, task-notification, ...)
    call_id: str | None = None     # tool_result and a skill's injected instructions: the tool call it answers
    abandoned: bool = False        # off the history for this tip
    in_context: bool = True        # kept by the latest compaction on the history
    stop: str | None = None        # an assistant row's stop reason, where stored
    ends_turn: bool = False        # a native turn-end row (Claude's system/turn_duration, or its
                                   # "[Request interrupted by user…" row)
    at: str | None = None
    size: int = 0                  # stored bytes
    data: dict | None = None       # the row as stored, a fresh copy per call; None over 16 MiB or unreadable
    format: str = ""


@dataclass(frozen=True, kw_only=True)
class RowPage:
    """A bounded page of rows, oldest first, or in history order for a history read (1.13)."""
    rows: tuple                    # Row
    next: str                      # cursor to continue after the last row (== `after` when nothing new)
    previous: str                  # cursor before the first row
    at_end: bool                   # nothing complete after `next` right now
    generation: str
    remaining: int | None = None
    tip: str | None = None         # history reads: the tip used
    gap: str | None = None         # history reads: as TranscriptInfo.gap


@dataclass(frozen=True, kw_only=True)
class Turn:
    """Core's grouping of the history into prompt turns and command turns (1.13); it never spans generations."""
    id: str                        # its first row's ref ("@" + that row's cursor when it has none, stable while
                                   # its position survives); "preamble" for rows before the first turn
    kind: str                      # "prompt", "command" or "preamble"
    command: str | None = None     # a command turn's name, "/compact"
    args: str | None = None
    source: str | None = None
    at: str | None = None
    start: str = ""                # cursor before its first row
    end: str = ""                  # cursor after its last row
    rows: int = 0
    in_context: bool = True        # some of its rows are in context
    finished: bool | None = None   # None: can't be told
    preview: str = ""              # the prompt's or command's first 200 characters


@dataclass(frozen=True, kw_only=True)
class TurnPage:
    turns: tuple                   # Turn, oldest first
    next: str
    previous: str
    at_end: bool
    generation: str
    remaining: int | None = None
    tip: str | None = None
    gap: str | None = None


class ModelsUnavailable(Error):
    """A harness that doesn't provide its models, or whose models can't be read now (1.13). A settings screen
    catches it and offers a free-form model field instead."""


@dataclass(frozen=True, kw_only=True)
class Model:
    """A model a harness takes, for the provider its launches use (1.13)."""
    id: str                          # the ID the harness sends for this provider
    name: str                        # display name
    family: str | None = None        # "opus", "sonnet", "haiku", "fable"
    aliases: tuple = ()              # names that resolve to it for this provider ("sonnet")
    efforts: tuple = ()              # the efforts it takes, low to high; () when it takes none
    default_effort: str | None = None
    inputs: tuple = ("text",)        # "text", plus "image" where it takes images
    context_window: int | None = None
    long_context_window: int | None = None   # with the "[1m]" suffix, where the model supports it
    max_output_tokens: int | None = None
    default: bool = False            # the model the harness uses when none is given, where known
    hidden: bool = False


@dataclass(frozen=True, kw_only=True)
class ModelCatalog:
    """A harness's models (1.13). Treat a source, provider, input or effort you don't know as an opaque name:
    later minors may add them."""
    models: tuple                    # Model; only those offered on `provider`
    free_form: bool                  # the harness also takes model IDs it doesn't list
    source: str                      # "table" (kept by mv2) or "native" (asked of the harness)
    version: str | None = None       # "table": the harness version it was taken from ("2.1.310")
    provider: str | None = None      # Claude: "first-party", "bedrock", "vertex", "foundry", "mantle", ...


@dataclass(frozen=True, kw_only=True)
class ModelCheck:
    """Whether a harness takes a model and effort, without spending anything (1.13)."""
    ok: bool
    model: str | None = None         # the resolved ID, when it resolves
    effort: str | None = None        # the effort a run would use: the given one when the model takes it, the
                                     # model's default_effort when none was given, None when it takes none
    verified: bool = False           # True when the model is in the catalog; False when ok only because the
                                     # harness also takes models it doesn't list (reason says so)
    reason: str | None = None        # why not ok, or why ok is unverified


class Harness:
    """A harness configured in a universe (1.13): mv.universe(root).harness(NAME)."""

    def __init__(self, universe, name, info):
        self._universe, self.name, self.info = universe, name, info

    def __repr__(self):
        return f"Harness({self.name!r} in {str(self._universe.path)!r})"

    def models(self):
        """The models this harness takes, for a launch from this process (observe)."""
        return _from_core(ModelCatalog, self._ask("models"))

    def check(self, model, *, effort=None):
        """Whether this harness takes `model` (an ID or alias) at `effort`, for a launch from this process,
        without spending anything (observe). An effort the model doesn't take is not ok: Claude Code would
        silently use another."""
        if not isinstance(model, str) or not model.strip():
            raise Error("model must be a model ID or alias")
        if effort is not None and (not isinstance(effort, str) or not effort):
            raise Error("effort must be an effort name or None")
        return _from_core(ModelCheck, self._ask("check_model", model, effort=effort))

    def _ask(self, method, *args, **kwargs):
        from .. import harnesses
        from ..harnesses import types
        from ..runtime import effects
        plugin, root = self._universe._plugin, self._universe.path
        with _acting(plugin._loaded, "observe", self._universe, f"reading {self.name}'s models"):
            adapter = _call(harnesses.load, root, self.name)
            if not callable(getattr(adapter, method, None)):
                later = (": not implemented for this harness yet"
                         if getattr(adapter, "family", "") in ("codex", "pi") else "")
                raise ModelsUnavailable(f"{self.name} does not provide its models{later}")
            _call(effects.require, method, self.name)
            try:  # an adapter's failure never reaches the plugin as text with a file's content in it
                result = getattr(adapter, method)(*args, environment=_launch_environment(adapter), **kwargs)
            except Exception:
                raise ModelsUnavailable(f"{self.name}'s models could not be read") from None
        if not isinstance(result, types.ModelCatalog if method == "models" else types.ModelCheck):
            raise ModelsUnavailable(f"{self.name}'s models could not be read")
        return result


def _launch_environment(adapter):
    """What a launch of `adapter` from this process gets: this process's environment without mv2's own MV2_*
    variables, with what the harness removes mapped to None and its settings env on top."""
    spec = getattr(adapter, "spec", {}) or {}
    environment = {key: value for key, value in os.environ.items() if not key.startswith("MV2_")}
    environment.update(dict.fromkeys(spec.get("unset_env") or (), None))
    environment.update(spec.get("env") or {})
    return environment

def _from_core(kind, value):
    """An API result from core's result of the same shape: core imports no API types, so a field core adds later
    is one line here."""
    known = {item.name for item in fields(kind)}
    values = {name: getattr(value, name) for name in known if hasattr(value, name)}
    if kind is TranscriptInfo:
        values["earlier"] = tuple(_from_core(Generation, item) for item in values.get("earlier") or ())
    elif kind is RowPage:
        values["rows"] = tuple(_from_core(Row, item) for item in values.get("rows") or ())
    elif kind is TurnPage:
        values["turns"] = tuple(_from_core(Turn, item) for item in values.get("turns") or ())
    elif kind is ModelCatalog:
        values["models"] = tuple(_from_core(Model, item) for item in values.get("models") or ())
    return kind(**values)  # a row's data is already a fresh copy: core re-reads each body per call


def _choices(value, what):
    if value is None:
        return None
    if isinstance(value, str) or not all(isinstance(item, str) for item in value):
        raise Error(f"{what} takes a set of names")
    return frozenset(value)


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
        """This agent's session as the harness side sees it: native ID, launch, state, native mode, plugins and
        its recorded workspace."""
        family = ""
        try:
            family = self._plugin.harness_info(self.universe, self.harness).family
        except Error:
            pass
        runtime = self.record.get("runtime") or {}
        return SessionInfo(self.universe.path, self.id, self.harness or "", family,
                           self.record.get("native_session_id"), runtime.get("launch_id"),
                           runtime.get("state", "unknown"), runtime.get("native_mode"), runtime.get("plugins"),
                           self.record.get("workspace") or None)

    def terminal(self, lines=30):
        """The last `lines` lines of the agent's terminal, or why there is none (needs agents.read)."""
        self._plugin._need("agents.read")
        from ..tools import inspect
        with _acting(self._plugin._loaded, "observe", self.universe, "reading the terminal"):
            capture = _call(inspect.run, self.universe.path, agent=self.id)
        return "\n".join(inspect.tail(capture, lines)) if isinstance(capture, str) else str(capture)

    def usage(self):
        self._plugin._need("agents.read")
        from ..tools import get_usage
        with _acting(self._plugin._loaded, "observe", self.universe, "reading usage"):
            return _call(get_usage.run, self.universe.path, agent=self.id)

    def capabilities(self):
        self._plugin._need("agents.read")
        from ..tools import capabilities
        with _acting(self._plugin._loaded, "observe", self.universe, "reading capabilities"):
            return _call(capabilities.run, self.universe.path, agent=self.id)

    def resume(self, *, wait=False):
        self._plugin._need("agents.control")
        from ..tools import resume
        with _acting(self._plugin._loaded, "act", self.universe, "resume"):
            return _call(resume.run, self.universe.path, agent=self.id, wait=wait, json=True)

    def kill(self):
        self._plugin._need("agents.control")
        from ..tools import kill
        with _acting(self._plugin._loaded, "act", self.universe, "kill"):
            return _call(kill.run, self.universe.path, agent=self.id)

    def send(self, text, delivery="steer"):
        """Send as this plugin: the agent reads "Message from plugin NAME" (needs messages.send).

        steer and follow-up wake a stopped or dormant agent; inbox-only never does."""
        self._plugin._need("messages.send")
        from ..tools import send
        sender = {"name": f"plugin {self._plugin.name}", "plugin": self._plugin.name, "agent_id": None}
        with _acting(self._plugin._loaded, "act", self.universe, "send"):
            record = _call(send.run, self.universe.path, agent=self.id, text=text, sender=sender, delivery=delivery)
        native = record.get("native_delivery") or {}
        return SendResult(bool(record.get("ok", True)), record.get("delivery", delivery), str(record.get("state", "")),
                          str(record.get("reason") or native.get("reason") or ""), record)

    def ask(self, question):
        """A side question answered from the agent's conversation, never added to it (needs messages.ask)."""
        self._plugin._need("messages.ask")
        from ..tools import ask
        with _acting(self._plugin._loaded, "spend", self.universe, "ask"):
            result = _call(ask.run, self.universe.path, agent=self.id, question=question)
        if result.get("state") != "ok":
            raise Error(result.get("reason") or "The agent did not answer")
        return (result.get("value") or {}).get("answer", "")

    def inbox(self):
        """Unread inbox-only messages, left unread (needs messages.read): [{at, from, text, delivery}]."""
        self._plugin._need("messages.read")
        from ..universe import mailbox
        with _acting(self._plugin._loaded, "observe", self.universe, "reading the inbox"):
            found = _call(mailbox.inbox, self.directory, limit=None, peek=True)
        return [{key: message[key] for key in ("at", "from", "text", "delivery") if key in message}
                for message in found["messages"]]

    def _transcript(self, call, what, **arguments):
        """A transcript read (observe, transcripts.read) through core, for this agent's own universe."""
        self._plugin._need("transcripts.read")
        from ..runtime import transcripts

        def read():
            try:
                return getattr(transcripts, call)(self.universe.path, self.directory, **arguments)
            except transcripts.Changed as error:
                raise TranscriptChanged(str(error), reason=error.reason, resume=error.resume,
                                        generation=getattr(error, "generation", None)) from None
            except transcripts.Unavailable as error:
                raise TranscriptUnavailable(str(error), retry=bool(getattr(error, "retry", False))) from None
        with _acting(self._plugin._loaded, "observe", self.universe, what):
            return _call(read)

    def transcript(self, *, generation=None, tip=None):
        """What this agent's transcript holds now (1.13; needs transcripts.read): counts, cursors and the earlier
        generations. Cheap. `tip`: a row ref the history and its counts start from.

        Without `generation`, every read is of the current generation, and a cursor from another raises
        TranscriptChanged("replaced") with `resume` at the current start. With `generation` (a readable token
        from `earlier`), a read is of that generation only, and another's cursor is an api.Error; an unknown or
        unreadable token raises TranscriptUnavailable (retry false)."""
        return _from_core(TranscriptInfo, self._transcript("info", "reading the transcript", generation=generation,
                                                           tip=tip))

    def transcript_rows(self, *, after=None, before=None, limit=200, max_bytes=1 << 20, history=False, labels=None,
                        kinds=None, tip=None, generation=None, bodies=True):
        """A bounded page of rows (1.13; needs transcripts.read), oldest first, or in history order with
        `history=True`. `after`/`before`: a cursor or a row ref; `before` alone gives the newest rows. `labels`
        and `kinds` keep only those rows, and the rest are neither counted nor read. The history read is
        history=True with `tip`: the page says the tip used and the gap, and its rows' refs are the walked
        history. `bodies=False` leaves `data` out and reads no bodies (max_bytes doesn't apply), to walk a whole
        history's refs cheaply. Raises TranscriptChanged when the transcript changed under a cursor; see
        transcript() for `generation`."""
        return _from_core(RowPage, self._transcript(
            "rows", "reading transcript rows", after=after, before=before, limit=limit, max_bytes=max_bytes,
            history=bool(history), labels=_choices(labels, "labels"), kinds=_choices(kinds, "kinds"), tip=tip,
            generation=generation, bodies=bool(bodies)))

    def iter_transcript_rows(self, *, after=None, limit=200, max_bytes=1 << 20, history=False, labels=None,
                             kinds=None, tip=None, generation=None, bodies=True):
        """Every row from `after` on, a bounded page at a time (1.13): each page is read, and access checked,
        as transcript_rows reads it, and it raises what a page raises."""
        cursor = after
        while True:
            page = self.transcript_rows(after=cursor, limit=limit, max_bytes=max_bytes, history=history,
                                        labels=labels, kinds=kinds, tip=tip, generation=generation, bodies=bodies)
            yield from page.rows
            if page.at_end or not page.rows:
                return
            cursor = page.next

    def transcript_turns(self, *, after=None, before=None, limit=50, tip=None, generation=None):
        """Core's turns over the history (1.13; needs transcripts.read), oldest first: prompt turns, command turns
        and a preamble. `after`/`before`: a turn ID or a cursor."""
        return _from_core(TurnPage, self._transcript("turns", "reading transcript turns", after=after,
                                                     before=before, limit=limit, tip=tip, generation=generation))

    def transcript_turn(self, turn_id, *, after=None, limit=200, max_bytes=1 << 20, tip=None, generation=None):
        """A bounded page of one turn's rows (1.13; needs transcripts.read)."""
        if not isinstance(turn_id, str) or not turn_id:
            raise Error("transcript_turn takes a turn's id, from transcript_turns")
        return _from_core(RowPage, self._transcript("turn", "reading a transcript turn", turn_id=turn_id,
                                                    after=after, limit=limit, max_bytes=max_bytes, tip=tip,
                                                    generation=generation))

    def history(self, last=20):
        """The last messages, read or not, oldest first (needs messages.read)."""
        self._plugin._need("messages.read")
        from ..universe import mailbox
        with _acting(self._plugin._loaded, "observe", self.universe, "reading message history"):
            found = _call(mailbox.inbox, self.directory, limit=None, all_messages=True, peek=True)
        return [{key: message[key] for key in ("at", "from", "text", "delivery") if key in message}
                for message in found["messages"][-last:]]
