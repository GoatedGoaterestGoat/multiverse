"""Finding plugins, loading the ones that are on, and keeping each one's failures to itself.

Sources, first match by name wins:

  path       MV2_PLUGIN_PATH: directories (a plugin, or a folder of plugins) a
             developer points at; trusted, like any code they run
  universe   <universe>/plugins/NAME/, loaded only once trusted at its content
  personal   ~/.multiverse/plugins/NAME/
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
import traceback
import types

from ..store import now
from . import config, manifest as manifests, settings as settings_files, trust

API_VERSION = (1, 1)
BUNDLED = Path(__file__).parent / "builtin"
PACKAGE = "mv2_plugins"
SAFE_MODE = "MV2_PLUGINS"  # "off" loads no plugin at all
ERRORS_KEPT = 20
SOURCES = ("path", "universe", "personal", "bundled")

# The process plugins are loaded into: cli, dashboard, host or helper.
process = "cli"
_lock = threading.RLock()
_loaded = {}  # resolved plugin directory -> Loaded or Failed
_running = set()  # (id(Loaded), what) of guarded calls still running past their time budget
_entries = {}  # root -> (signature, entries): entries() is asked for on every dashboard refresh
_generation = 0  # rises whenever a plugin is loaded or fails to load
SYNCHRONOUS_BUDGET = 2.0  # seconds a guarded call may take where a person waits on it


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
class Key:
    key: str
    label: str
    action: object  # action(context) -> str | None, the status line


@dataclass
class Registrations:
    """What one plugin's register(mv) added, by extension point."""
    commands: dict = field(default_factory=dict)
    bootstrap: list = field(default_factory=list)
    doctor: list = field(default_factory=list)
    columns: dict = field(default_factory=dict)
    panels: dict = field(default_factory=dict)  # type -> "module:Class"
    inspector: list = field(default_factory=list)
    theme: dict | None = None
    keys: list = field(default_factory=list)
    screens: dict = field(default_factory=dict)  # name -> "module:Class"
    settings_check: object = None
    settings_screen: str | None = None  # "module:Class"
    launch: dict = field(default_factory=dict)  # family -> [fn(LaunchInfo) -> Contribution | None]
    capabilities: dict = field(default_factory=dict)  # name -> fn(SessionInfo) -> api.Capability
    usage: list = field(default_factory=list)  # fn(SessionInfo, usage) -> usage
    helpers: dict = field(default_factory=dict)  # action -> fn(arguments) -> exit code


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
    found.extend(("bundled", directory) for directory in _plugin_directories(BUNDLED))
    return found


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
    for source, directory in _source_directories(root):
        parts.append((source, str(directory), _stat(directory / manifests.FILE)))
        if source == "universe":  # trust covers every file
            parts.append(tuple((str(path), _stat(path)) for path in sorted(directory.rglob("*"))
                               if "__pycache__" not in path.parts))
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
    chosen, shadowed, invalid = {}, {}, {}
    for source, directory in _source_directories(root):
        try:
            found = manifests.load(directory)
        except manifests.ManifestError as error:
            invalid.setdefault(directory.name, (source, directory, str(error)))
            continue
        if found.name in chosen:
            shadowed.setdefault(found.name, []).append((source, directory))
        else:
            chosen[found.name] = (source, directory, found)
    result = {}
    for name, (source, directory, found) in chosen.items():
        on = settings.is_on(name, found.default if source == "bundled" else "off")
        result[name] = _entry(root, name, on, source, directory, found, shadowed.get(name, []))
    for name, (source, directory, problem) in invalid.items():
        if name not in result:
            result[name] = Entry(name, "invalid", settings.is_on(name), source, directory, problem=problem)
    for name, value in settings.enabled.items():
        if value and name not in result:
            result[name] = Entry(name, "missing", True, problem=(
                f"{name} is on in {config.path(root)} but is not installed"))
    return [result[name] for name in sorted(result)]


def _entry(root, name, on, source, directory, found, shadowed):
    entry = Entry(name, "on" if on else "off", on, source, directory, found, shadowed=shadowed)
    if source == "universe":
        trusted = trust.state(directory)
        if trusted != "trusted":
            entry.state = trusted
            entry.problem = (f"{name} is in this universe's plugins/ and is not trusted; review it, then run "
                             f"mv2 plugins trust {name}" if trusted == "untrusted" else
                             f"{name} changed since it was trusted; review it, then run mv2 plugins trust {name}")
            return entry
    problem = compatible(found)
    if problem:
        entry.state, entry.problem = "incompatible", f"{name} {problem}"
        return entry
    if not on:
        return entry
    if safe_mode():
        entry.state, entry.problem = "safe-mode", f"{SAFE_MODE}=off: no plugins are loaded"
        return entry
    with _lock:
        known = _loaded.get(Path(directory).resolve())
    if isinstance(known, Failed):
        entry.state, entry.problem = "failed", known.problem
        return entry
    if known is not None:
        entry.state, entry.loaded = "loaded", known
    if root is not None and found.settings is not None:
        # Unusable settings: the plugin acts as off in this universe, with the reason.
        problem = settings(root, entry).problem
        if problem:
            entry.state, entry.problem = "unusable", problem
    return entry


def entry(root, name):
    for found in entries(root):
        if found.name == name:
            return found
    raise PluginError(f"No plugin named {name} here. Run mv2 plugins to see the plugins this universe can use.")


def _package_name(found):
    digest = hashlib.sha256(str(Path(found.directory).resolve()).encode()).hexdigest()[:10]
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


def load(found):
    """The Loaded plugin for an Entry that is usable, importing and registering it once per process.

    Raises PluginError when it cannot be loaded; the failure is kept, so later
    callers get the same answer without importing again.
    """
    # A plugin whose settings here are unusable still loads, so its screens can fix them.
    if not (found.on and found.state in ("on", "loaded", "unusable")):
        raise PluginError(found.problem or f"{found.name} is off here")
    from . import api  # the API imports this module
    directory = Path(found.directory).resolve()
    with _lock:
        known = _loaded.get(directory)
        if isinstance(known, Loaded):
            return known
        if isinstance(known, Failed):
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
        except Exception as error:
            problem = f"{found.name} failed to load: {_describe(error)}"
            _loaded[directory] = Failed(found.manifest, found.source, problem)
            _changed()
            raise PluginError(problem) from error
        _loaded[directory] = loaded
        _changed()
        return loaded


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


def record(loaded, what, error, root=None):
    """Keep a plugin's failure: in memory for the dashboard, and in its errors.log in `root` when given."""
    text = f"{what}: {_describe(error)}"
    if loaded.errors and loaded.errors[-1] == text:
        return text  # the same failure again (a column on every refresh): kept once
    loaded.errors.append(text)
    del loaded.errors[:-ERRORS_KEPT]
    if root is not None:
        try:
            log = Path(root) / "state" / "plugins" / loaded.name / "errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as handle:
                handle.write(f"{now()} {text}\n")
                handle.write("".join(traceback.format_exception(error)).rstrip() + "\n")
        except OSError:
            pass
    return text


def guard(loaded, what, function, *args, root=None, default=None, timeout=None, **kwargs):
    """Call plugin code; an exception is recorded against the plugin and `default` returned.

    With `timeout` (seconds) the call runs in a thread of its own and the caller
    waits at most that long: past it, "took longer than N s" is recorded and
    `default` returned. Python cannot stop the thread, so until it ends, later
    calls of the same plugin and `what` return `default` at once rather than
    starting another.
    """
    if timeout is None:
        try:
            return function(*args, **kwargs)
        except Exception as error:
            record(loaded, what, error, root)
            return default
    key = (id(loaded), what)
    with _lock:
        if key in _running:
            return default
        _running.add(key)
    outcome = {}

    def call():
        try:
            outcome["value"] = function(*args, **kwargs)
        except Exception as error:
            outcome["error"] = error
        finally:
            with _lock:
                _running.discard(key)

    worker = threading.Thread(target=call, name=f"plugin {loaded.name}: {what}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        record(loaded, what, TimeoutError(f"took longer than {timeout:g} s"), root)
        return default
    if "error" in outcome:
        record(loaded, what, outcome["error"], root)
        return default
    return outcome.get("value")


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
