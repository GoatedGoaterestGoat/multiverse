"""Finding plugins, loading the ones that are on, and keeping each one's failures to itself.

Sources, first match by name wins:

  path       MV2_PLUGIN_PATH: directories (a plugin, or a folder of plugins) a
             developer points at; trusted, like any code they run
  universe   <universe>/plugins/NAME/, loaded only once trusted at its content
  personal   ~/.multiverse/plugins/NAME/
  installed  Python packages that name their plugin directory in the
             `multiverse.plugins` entry point group (pip install ...)
  checkout   plugins/NAME/ at the root of the mv2 checkout this code runs from:
             optional Git submodules, pinned by the mv2 repository
  bundled    multiverse/plugins/builtin/NAME/

A plugin's Python is imported only when something needs what it registers, into
a package of its own (`mv2_plugins.NAME_<digest>`), so two copies of one name
never share modules and nothing is added to sys.path. Each loaded plugin is kept
for the life of the process. Whether it counts in a universe is decided there,
from that universe's plugins.json, every time.
"""
from dataclasses import dataclass, field
import hashlib
import importlib
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import types

from ..store import now
from . import config, manifest as manifests, settings as settings_files, trust

API_VERSION = (1, 9)
BUNDLED = Path(__file__).parent / "builtin"
# plugins/ beside src/ when this code runs from a checkout; None when it runs from an installed package.
_REPOSITORY = Path(__file__).resolve().parents[3]
CHECKOUT = _REPOSITORY / "plugins" if (_REPOSITORY / "src" / "multiverse" / "plugins").is_dir() else None
ENTRY_POINTS = "multiverse.plugins"
PACKAGE = "mv2_plugins"
SAFE_MODE = "MV2_PLUGINS"  # "off" loads no plugin at all
ERRORS_KEPT = 20
SOURCES = ("path", "universe", "personal", "installed", "checkout", "bundled")

# The process plugins are loaded into: cli, dashboard, host or helper.
process = "cli"
_lock = threading.RLock()
_loaded = {}  # resolved plugin directory -> Loaded or Failed
_running = set()  # (id(Loaded), what) of guarded calls still running past their time budget
_running_lock = threading.Lock()
_plugin_locks = {}  # resolved plugin directory -> its import lock  # guards _running only: never held while plugin code runs
_log_lock = threading.Lock()
_entries = {}  # root -> (signature, entries): entries() is asked for on every dashboard refresh
_generation = 0  # rises whenever a plugin is loaded or fails to load
SYNCHRONOUS_BUDGET = 2.0
UNUSABLE = "its settings cannot be used: "  # how an `unusable` entry's problem begins, after the plugin's name  # seconds a guarded call may take where a person waits on it


class PluginError(ValueError):
    """A plugin that is on cannot be used; the message says why and what to do."""


@dataclass
class Command:
    name: str
    configure: object
    run: object
    cli_run: object = None


@dataclass
class Column:
    key: str
    title: str
    width: int
    value: object  # value(row) -> str
    compact: bool = False
    flexible: bool = False
    group: object = None  # group(row) -> str, for subverse rows


@dataclass
class DataPanel:
    """A local view panel a plugin renders from data: render(api.PanelContext) gives api blocks."""
    kind: str
    render: object
    title: str | None = None
    auto: bool = False  # shown in every scope where the plugin is on, unless a view opts out


@dataclass
class Registrations:
    """What one plugin's register(mv) added, by extension point."""
    commands: dict = field(default_factory=dict)
    bootstrap: list = field(default_factory=list)
    doctor: list = field(default_factory=list)
    columns: dict = field(default_factory=dict)
    panels: dict = field(default_factory=dict)  # type -> "module:Class" (a Panel class)
    data_panels: dict = field(default_factory=dict)  # type -> DataPanel: render(PanelContext) -> blocks
    inspector: list = field(default_factory=list)
    theme: dict | None = None
    screens: dict = field(default_factory=dict)  # name -> "module:Class"
    settings_check: object = None
    settings_migrate: object = None  # fn(data, source) -> data, or (data, on) for pre-plugin settings
    settings_screen: str | None = None  # "module:Class": the plugin's own settings screen
    settings_fields: list | None = None  # api.Field: settings shown as these fields rather than from the schema
    actions: dict = field(default_factory=dict)  # palette name -> fn(context) -> status line
    launch: dict = field(default_factory=dict)  # family -> [fn(LaunchInfo) -> Contribution | None]
    capabilities: dict = field(default_factory=dict)  # name -> fn(SessionInfo) -> api.Capability
    usage: list = field(default_factory=list)  # fn(SessionInfo, usage) -> usage
    helpers: dict = field(default_factory=dict)  # action -> fn(arguments) -> exit code
    handlers: list = field(default_factory=list)  # (event type pattern, fn(api.Event)), run in the host
    services: list = field(default_factory=list)  # (start(api.ServiceContext), stop or None), run in the host


@dataclass
class Loaded:
    manifest: manifests.Manifest
    source: str
    package: str
    module: object
    registrations: Registrations
    errors: list = field(default_factory=list)

    @property
    def name(self):
        return self.manifest.name

    def resolve(self, reference):
        """A "module:Attribute" name inside this plugin, imported now."""
        module, _, attribute = reference.partition(":")
        if not module or not attribute:
            raise ValueError(f"{reference!r} must be \"module:Attribute\"")
        value = importlib.import_module(f"{self.package}.{module}")
        for part in attribute.split("."):
            value = getattr(value, part)
        return value


@dataclass
class Failed:
    manifest: manifests.Manifest
    source: str
    problem: str
    files: tuple = ()  # the plugin's files when it failed: changed files are tried again


@dataclass
class Entry:
    """One plugin name as a universe sees it.

    state: `off`, `on` (on, not loaded in this process yet), `loaded`,
    `failed` (its code or registration failed), `unusable` (its settings
    here cannot be used), `untrusted`, `changed`
    (trusted at other content), `incompatible` (needs another plugin API),
    `invalid` (its manifest cannot be used), `missing` (on in plugins.json, not
    installed), or `safe-mode`.
    """
    name: str
    state: str
    on: bool
    source: str | None = None
    directory: Path | None = None
    manifest: manifests.Manifest | None = None
    problem: str | None = None
    shadowed: list = field(default_factory=list)  # [(source, directory)]
    loaded: Loaded | None = None

    @property
    def usable(self):
        """On, and nothing known stops it from loading here."""
        return self.on and self.state in ("on", "loaded")


def safe_mode():
    return os.environ.get(SAFE_MODE, "").strip().lower() in ("off", "0", "false", "no")


def _plugin_directories(base):
    """Plugin directories at `base`: itself when it is one, else its children that are."""
    base = Path(base).expanduser()
    if (base / manifests.FILE).is_file():
        return [base]
    try:
        return sorted(child for child in base.iterdir() if (child / manifests.FILE).is_file())
    except OSError:
        return []


def _source_directories(root):
    """(source, plugin directory) in precedence order."""
    found = []
    for entry in os.environ.get("MV2_PLUGIN_PATH", "").split(os.pathsep):
        if entry.strip():
            found.extend(("path", directory) for directory in _plugin_directories(entry.strip()))
    if root is not None:
        found.extend(("universe", directory) for directory in _plugin_directories(Path(root) / "plugins"))
    found.extend(("personal", directory) for directory in _plugin_directories(trust.user_directory() / "plugins"))
    found.extend(("installed", directory) for directory in installed_directories())
    if CHECKOUT is not None:
        # An uninitialized submodule is an empty directory: no plugin.json, so nothing is found there.
        found.extend(("checkout", directory) for directory in _plugin_directories(CHECKOUT))
    found.extend(("bundled", directory) for directory in _plugin_directories(BUNDLED))
    return found


_installed = None


def installed_directories():
    """Plugin directories that installed packages name in the multiverse.plugins entry point group.

    An entry point's value is a top-level package whose directory holds
    plugin.json (`compaction = "mv2_compaction"`). It is located, never
    imported. Read once per process: installing a package needs a restart.
    """
    global _installed
    if _installed is None:
        found = []
        try:
            from importlib.metadata import entry_points
            for point in sorted(entry_points(group=ENTRY_POINTS), key=lambda point: point.name):
                try:
                    spec = importlib.util.find_spec(point.module)
                except (ImportError, ValueError):
                    continue
                locations = list(spec.submodule_search_locations or []) if spec else []
                directory = Path(locations[0]) if locations else (Path(spec.origin).parent if spec and spec.origin else None)
                if directory is not None and (directory / manifests.FILE).is_file():
                    found.append(directory)
        except Exception:
            found = []
        _installed = found
    return list(_installed)


def compatible(manifest):
    """None, or why this mv2's plugin API cannot load the plugin."""
    major, minor = manifest.api
    if major != API_VERSION[0] or minor > API_VERSION[1]:
        have = ".".join(map(str, API_VERSION))
        return (f"needs plugin API {major}.{minor}; this mv2 has {have}"
                + ("; update mv2" if (major, minor) > API_VERSION else "; update the plugin"))
    return None


def _stat(path):
    try:
        status = path.stat()
        return status.st_mtime_ns, status.st_size
    except OSError:
        return None


def _signature(root):
    """Everything entries() depends on, by file times and sizes: cheap enough for every refresh."""
    parts = [os.environ.get("MV2_PLUGIN_PATH", ""), safe_mode(), _generation, _stat(trust.user_directory() / trust.FILE)]
    if root is not None:
        parts.append(tuple((path.name, _stat(path)) for path in sorted(Path(root).glob("*.json"))))
        parts.append(tuple((path.name, _stat(path)) for path in sorted((Path(root) / settings_files.DIRECTORY).glob("*.json"))))
    with _lock:
        failed = {directory: known.files for directory, known in _loaded.items() if isinstance(known, Failed)}
    for source, directory in _source_directories(root):
        parts.append((source, str(directory), _stat(directory / manifests.FILE)))
        if directory.resolve() in failed:  # a plugin that failed to load is tried again once its files change
            parts.append(_files(directory.resolve()) == failed[directory.resolve()])
        if source == "universe":  # trust covers every file but .git, compiled bytecode included
            parts.append(tuple((str(path), _stat(path)) for path in sorted(directory.rglob("*"))
                               if ".git" not in path.relative_to(directory).parts))
    return tuple(parts)


def entries(root):
    """Every plugin this universe can see or names, sorted by name. Reads files only; imports nothing.

    Kept while nothing it depends on changes (see _signature)."""
    root = Path(root) if root is not None else None
    signature = _signature(root)
    with _lock:
        cached = _entries.get(root)
    if cached and cached[0] == signature:
        return list(cached[1])
    found = _entries_uncached(root)
    with _lock:
        _entries[root] = (signature, found)
    return list(found)


def _entries_uncached(root):
    settings = config.load(root) if root is not None else config.Config()
    chosen, shadowed, invalid, untrusted, trusted = {}, {}, {}, {}, {}
    for source, directory in _source_directories(root):
        try:
            found = manifests.load(directory)
        except manifests.ManifestError as error:
            invalid.setdefault(directory.name, (source, directory, str(error)))
            continue
        if source == "universe" and found.name not in chosen:
            state, value = trust.verify(directory)
            if state != "trusted":
                # An untrusted copy in a universe never hides the plugin another source provides (a repository's
                # plugins/typeahead must not turn the bundled typeahead off); alone, it is listed as untrusted.
                untrusted.setdefault(found.name, (source, directory, found, state))
                continue
            trusted[found.name] = value
        if found.name in chosen:
            shadowed.setdefault(found.name, []).append((source, directory))
        else:
            chosen[found.name] = (source, directory, found)
    for name, (source, directory, found, state) in untrusted.items():
        if name in chosen:
            shadowed.setdefault(name, []).append((source, directory))
        else:
            chosen[name] = (source, directory, found)
            trusted[name] = None
    result = {}
    for name, (source, directory, found) in chosen.items():
        # A pre-plugin settings file turns nothing on: its first-use conversion writes the switch into plugins.json.
        default = found.default if source == "bundled" else "off"
        on = settings.is_on(name, default)
        result[name] = _entry(root, name, on, source, directory, found, shadowed.get(name, []), trusted.get(name))
    for name, (source, directory, problem) in invalid.items():
        if name not in result:
            result[name] = Entry(name, "invalid", settings.is_on(name), source, directory, problem=problem)
    for name, value in settings.enabled.items():
        if value and name not in result:
            result[name] = Entry(name, "missing", True, problem=(
                f"{name} is on in {config.path(root)} but is not installed"))
    return [result[name] for name in sorted(result)]


def _entry(root, name, on, source, directory, found, shadowed, fingerprint=None):
    entry = Entry(name, "on" if on else "off", on, source, directory, found, shadowed=shadowed)
    if source == "universe":
        trusted = "trusted" if fingerprint else trust.state(directory)
        if trusted != "trusted":
            entry.state = trusted
            entry.problem = (f"{name} is in this universe's plugins/ and is not trusted; review it, then run "
                             f"mv2 plugins trust {name}" if trusted == "untrusted" else
                             f"{name} changed since it was trusted; review it, then run mv2 plugins trust {name}")
            return entry
        try:
            # What runs is the copy of exactly the trusted bytes, never the universe's directory itself.
            copy = trust.cached(directory, fingerprint)
            entry.manifest = manifests.load(copy)
        except (OSError, ValueError, manifests.ManifestError) as error:
            entry.state, entry.problem = "changed", f"{name} could not be copied for running: {error}"
            return entry
        found = entry.manifest
    problem = compatible(found)
    if problem:
        entry.state, entry.problem = "incompatible", f"{name} {problem}"
        return entry
    if not on:
        return entry
    if safe_mode():
        entry.state, entry.problem = "safe-mode", f"{SAFE_MODE}=off: no plugins are loaded"
        return entry
    running = Path(found.directory).resolve()  # where it runs from: its own directory, or a trusted copy
    with _lock:
        known = _loaded.get(running)
    if isinstance(known, Failed) and known.files == _files(running):
        entry.state, entry.problem = "failed", known.problem
        return entry
    if known is not None:
        entry.state, entry.loaded = "loaded", known
    if root is not None and found.settings is not None:
        # Unusable settings: the plugin acts as off in this universe, with the reason, naming the plugin and
        # the universe's own paths.
        problem = settings(root, entry).problem
        if problem:
            detail = problem.replace(f"{Path(root)}/", "")
            if detail.startswith(f"{name}: "):
                detail = detail[len(name) + 2:]
            entry.state, entry.problem = "unusable", f"{name}: {UNUSABLE}{detail}"
    return entry


def entry(root, name):
    for found in entries(root):
        if found.name == name:
            return found
    raise PluginError(f"No plugin named {name} here. Run mv2 plugins to see the plugins this universe can use.")


def _package_name(found):
    digest = hashlib.sha256(str(Path(found.manifest.directory).resolve()).encode()).hexdigest()[:10]
    return f"{PACKAGE}.{found.name.replace('-', '_')}_{digest}"


def _make_package(name, directory):
    if PACKAGE not in sys.modules:
        parent = types.ModuleType(PACKAGE)
        parent.__path__ = []
        parent.__doc__ = "Loaded mv2 plugins, one subpackage per plugin directory."
        sys.modules[PACKAGE] = parent
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    spec.submodule_search_locations = [str(directory)]
    package = importlib.util.module_from_spec(spec)
    package.__path__ = [str(directory)]
    sys.modules[name] = package
    return package


def load(found, off=False):
    """The Loaded plugin for an Entry that is usable, importing and registering it once per process.

    A plugin whose settings here are unusable still loads, so its screens can fix
    them. `off` also loads a plugin that is off here (still trusted and
    compatible), for what acts before it is turned on, such as migrating its
    pre-plugin settings. Raises PluginError when it cannot be loaded; the failure
    is kept, so later callers get the same answer without importing again.
    """
    if not (found.on and found.state in ("on", "loaded", "unusable")) and not (off and found.state == "off"):
        raise PluginError(found.problem or f"{found.name} is off here")
    from . import api  # the API imports this module
    directory = Path(found.manifest.directory).resolve()  # a trusted universe plugin runs from its copy
    # One lock per plugin: a slow import or register() never holds up another plugin, or any guarded call.
    with _plugin_lock(directory):
        with _lock:
            known = _loaded.get(directory)
        if isinstance(known, Loaded):
            return known
        if isinstance(known, Failed) and known.files == _files(directory):
            raise PluginError(known.problem)
        package = _package_name(found)
        registrations = Registrations()
        try:
            _make_package(package, directory)
            module = importlib.import_module(f"{package}.{found.manifest.module}") if found.manifest.module else None
            loaded = Loaded(found.manifest, found.source, package, module, registrations)
            register = getattr(module, "register", None) if module is not None else None
            if module is not None and not callable(register):
                raise PluginError(f"{found.manifest.module} has no register(mv) function")
            if register is not None:
                register(api.Plugin(loaded))
        except (Exception, SystemExit) as error:  # a plugin never ends the process by failing to load
            problem = f"{found.name} failed to load: {_describe(error)}"
            for name in [name for name in sys.modules if name == package or name.startswith(package + ".")]:
                del sys.modules[name]  # tried again from scratch once its files change
            with _lock:
                _loaded[directory] = Failed(found.manifest, found.source, problem, _files(directory))
                _changed()
            raise PluginError(problem) from error
        with _lock:
            _loaded[directory] = loaded
            _changed()
        return loaded


def _plugin_lock(directory):
    with _lock:
        return _plugin_locks.setdefault(directory, threading.Lock())


def _files(directory):
    """(path, size, mtime_ns) of a plugin's files, to tell when they changed. Leaves out what Python writes."""
    found = []
    for path in sorted(Path(directory).rglob("*")):
        if "__pycache__" in path.parts or ".git" in path.parts or not path.is_file():
            continue
        try:
            status = path.stat()
        except OSError:
            continue
        found.append((str(path), status.st_size, status.st_mtime_ns))
    return tuple(found)


def _changed():
    global _generation
    _generation += 1


def active(root):
    """The plugins on in this universe, loaded, sorted by name. One that fails to load is left out."""
    if safe_mode():
        return []
    result = []
    for found in entries(root):
        if found.usable:
            try:
                result.append(load(found))
            except PluginError:
                continue
    return result


def _describe(error):
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


ERRORS_LOG_BYTES = 1024 * 1024  # errors.log rotates to errors.log.1 past this
LOG_BYTES = 1024 * 1024  # plugin.log, events.log and host.log rotate to NAME.1 past this


def append_log(path, line):
    """Append one line to a log, rotating it to NAME.1 (one old file kept) once it is past LOG_BYTES."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock:
        if path.exists() and path.stat().st_size > LOG_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a") as handle:
            handle.write(line.rstrip("\n") + "\n")
TRACEBACK_EVERY = 60.0  # seconds: at most one traceback per plugin and call site in this long
_logged = {}  # (id(Loaded), what) -> (when a traceback was last written, failures not written since)


def record(loaded, what, error, root=None):
    """Keep a plugin's failure: in memory for the dashboard, and in its errors.log in `root` when given.

    The log has one line per failure, but at most one traceback per call site
    (`what`) a minute, with the count of the others since, so a column that
    fails on every row and refresh cannot fill the disk; it rotates at 1 MiB."""
    text = f"{what}: {_describe(error)}"
    if not (loaded.errors and loaded.errors[-1] == text):
        loaded.errors.append(text)
        del loaded.errors[:-ERRORS_KEPT]
    if root is None:
        return text
    key, moment = (id(loaded), what), time.monotonic()
    with _log_lock:
        last, quiet = _logged.get(key, (None, 0))
        if last is not None and moment - last < TRACEBACK_EVERY:
            _logged[key] = (last, quiet + 1)
            return text
        _logged[key] = (moment, 0)
        try:
            log = Path(root) / "state" / "plugins" / loaded.name / "errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            if log.exists() and log.stat().st_size > ERRORS_LOG_BYTES:
                log.replace(log.with_name("errors.log.1"))
            with log.open("a") as handle:
                handle.write(f"{now()} {text}" + (f" ({quiet} more like it not logged)" if quiet else "") + "\n")
                handle.write("".join(traceback.format_exception(error)).rstrip() + "\n")
        except OSError:
            pass
    return text


@dataclass(frozen=True)
class Outcome:
    """How a guarded call into plugin code ended: ok, error (recorded), timeout (recorded) or busy (an earlier call
    with the same key is still running: nothing started, nothing recorded)."""
    value: object = None
    state: str = "ok"
    error: str | None = None


def guarded(loaded, what, function, *args, root=None, timeout=SYNCHRONOUS_BUDGET, key=None, **kwargs):
    """Call plugin code within a time budget; the one way core calls into a plugin where something waits on it.

    The call runs in a daemon thread of its own (never an executor, which a hung
    call would exhaust and which would hold up the process's exit), and the
    caller waits at most `timeout` seconds. Python cannot stop the thread: until
    it ends, a call with the same plugin and `key` (default: `what`) is `busy`.
    SystemExit and other errors from plugin code are recorded, never raised.
    With timeout=None the call runs in the calling thread."""
    if timeout is None:
        try:
            return Outcome(function(*args, **kwargs))
        except (Exception, SystemExit) as error:
            return Outcome(None, "error", record(loaded, what, error, root))
    running_key = (id(loaded), what if key is None else key)
    with _running_lock:
        if running_key in _running:
            return Outcome(None, "busy")
        _running.add(running_key)
    outcome, finished = {}, threading.Event()

    def call():
        try:
            outcome["value"] = function(*args, **kwargs)
        except BaseException as error:  # SystemExit included: a plugin never ends the process from here
            outcome["error"] = error
        finally:
            with _running_lock:
                _running.discard(running_key)
            finished.set()

    threading.Thread(target=call, name=f"plugin {loaded.name}: {what}", daemon=True).start()
    if not finished.wait(timeout):
        return Outcome(None, "timeout", record(loaded, what, TimeoutError(f"took longer than {timeout:g} s"), root))
    if "error" in outcome:
        return Outcome(None, "error", record(loaded, what, outcome["error"], root))
    return Outcome(outcome.get("value"))


def guard(loaded, what, function, *args, root=None, default=None, timeout=None, **kwargs):
    """guarded(), returning the call's value, or `default` when it failed, timed out or was busy."""
    outcome = guarded(loaded, what, function, *args, root=root, timeout=timeout, **kwargs)
    return outcome.value if outcome.state == "ok" else default


def errors():
    """Every loaded plugin's recent failures in this process: {name: [text]}."""
    with _lock:
        return {item.name: list(item.errors) for item in _loaded.values()
                if isinstance(item, Loaded) and item.errors}


def settings(root, found):
    """The Settings of a plugin in this universe, with its own check when it is loaded."""
    check = found.loaded.registrations.settings_check if found.loaded else None
    return settings_files.read(root, found.manifest, check)


def reset():
    """Forget every loaded plugin (tests only: Python cannot unload a module)."""
    with _lock:
        _loaded.clear()
        _entries.clear()
        for name in [name for name in sys.modules if name == PACKAGE or name.startswith(PACKAGE + ".")]:
            del sys.modules[name]
