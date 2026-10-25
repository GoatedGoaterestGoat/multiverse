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
from . import access as levels, config, manifest as manifests, settings as settings_files, trust

API_VERSION = (1, 13)
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
_loading = {}  # resolved plugin directory -> (Event set when its load in the background ends, when it started)
_attempts = {}  # resolved plugin directory -> loads started for it in this process
_log_lock = threading.Lock()
_entries = {}  # root -> (signature, entries): entries() is asked for on every dashboard refresh
_generation = 0  # rises whenever a plugin is loaded or fails to load
SYNCHRONOUS_BUDGET = 2.0
REFRESH_WAIT = 0.5  # seconds a dashboard refresh waits, in all, for plugins still loading
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
    fallback: str | None = None  # a core column key: a row without a value draws that column's cell (1.13)
    replaces: str | None = None  # a core column key it stands in for wherever a view lists that column (1.13)


# Core column names a view may use for another (ui/views.py reads them from here), and the columns no plugin may
# stand in for: since focus mode they carry what core must always show (the focus, waiting and damaged markers).
CORE_COLUMN_ALIASES = {"activity": "status", "tokens": "context"}
ATTENTION_COLUMNS = ("name", "live", "status", "focus")


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
    problems: list = field(default_factory=list)  # what register(mv) asked for and could not have, said by doctor


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
    set_aside: list = field(default_factory=list)  # [(source, directory, state)]: universe copies not trusted now

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
        _installed = _installed_remembered()
    return list(_installed)


INSTALLED = "installed-plugins.json"  # in ~/.multiverse: the last scan, per Python environment


def _environment_signature():
    """The directories this Python finds packages in (not the current directory) and their modification times,
    which change whenever a package is installed or removed there."""
    parts = []
    for entry in sys.path[1:]:
        try:
            parts.append([entry, os.stat(entry).st_mtime_ns])
        except OSError:
            continue
    return parts


INSTALLED_KEPT = 4  # sys.path signatures remembered per Python: processes started differently (a runner, the CLI)
USED_EVERY = 3600  # seconds between noting that a remembered signature was used again (a write)


def _installed_remembered():
    """installed_directories, from an earlier scan when nothing was installed or removed since: importing
    importlib.metadata to scan costs every mv2 command more than the rest of plugin discovery together.

    Up to INSTALLED_KEPT signatures are kept per Python executable, the least recently used dropped, so processes
    whose sys.path differs do not make each other scan and rewrite in turn. The newest stays in the entry's own
    fields, which older mv2 reads; the others are in its `recent`, which it ignores."""
    from ..store import read
    path, key, signature = trust.user_directory() / INSTALLED, sys.executable, _environment_signature()
    try:
        data = read(path)
        entry = data.get("environments", {}).get(key)
    except (OSError, ValueError, AttributeError):
        data, entry = {}, None
    entries = _installed_entries(entry)
    for item in entries:
        directories = item.get("directories")
        if item.get("signature") == signature and isinstance(directories, list) and all(
                isinstance(found, str) and (Path(found) / manifests.FILE).is_file() for found in directories):
            if time.time() - item.get("used", 0) > USED_EVERY:  # noted now and then, not on every command
                item["used"] = time.time()
                _write_installed(path, data, key, entries)
            return [Path(found) for found in directories]
    found = _scan_installed()
    fresh = {"signature": signature, "directories": [str(item) for item in found], "used": time.time()}
    kept = sorted([item for item in entries if item.get("signature") != signature],
                  key=lambda item: item.get("used", 0), reverse=True)[:INSTALLED_KEPT - 1]
    _write_installed(path, data, key, [fresh, *kept])
    return found


def _installed_entries(entry):
    """The signatures remembered for one Python, newest first: the entry itself, then its `recent`."""
    if not isinstance(entry, dict):
        return []
    recent = entry.get("recent") if isinstance(entry.get("recent"), list) else []
    first = {name: entry[name] for name in ("signature", "directories", "used") if name in entry}
    return [item for item in [first, *recent] if isinstance(item, dict) and "signature" in item]


def _write_installed(path, data, key, entries):
    from ..store import write
    try:
        environments = data.get("environments") if isinstance(data, dict) else None
        environments = environments if isinstance(environments, dict) else {}
        entries = sorted(entries, key=lambda item: item.get("used", 0), reverse=True)
        environments[key] = {**entries[0], "recent": entries[1:]}
        write(path, {"schema": 1, "environments": environments})
    except (OSError, ValueError, IndexError):
        pass  # unremembered: scanned again next time


def _scan_installed():
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
    with _lock:
        failed = {directory: known.files for directory, known in _loaded.items() if isinstance(known, Failed)}
    for source, directory in _source_directories(root):
        parts.append((source, str(directory), _stat(directory / manifests.FILE)))
        if directory.resolve() in failed:  # a plugin that failed to load is tried again once its files change
            parts.append(_files(directory.resolve()) == failed[directory.resolve()])
        if source == "universe":  # trust covers every file but .git, compiled bytecode included
            try:
                parts.append(trust.signature(directory))
            except OSError:
                parts.append(None)
    return tuple(parts)


@dataclass(frozen=True)
class Declared:
    """One plugin manifest a universe can see, and whether plugins.json (or the plugin's default) turns it on."""
    name: str
    source: str
    directory: Path
    manifest: manifests.Manifest
    on: bool


_declared = {}  # root -> (signature, [Declared])


def declared(root):
    """Every plugin manifest this universe can see, every copy, whatever its trust or state, with whether it is on.

    What plugins say, from small JSON files only: no trust check (which hashes
    a universe plugin's files), no import. For the checks every mv2 command
    makes (an old settings file to convert, an event someone subscribes to, a
    usage adjuster), which must cost a built-in command nothing measurable: a
    caller uses it only to see that nothing needs doing, and goes through
    entries() (trust included) for anything it then does. Kept while no
    manifest, plugin directory or plugins.json changes."""
    root = Path(root) if root is not None else None
    found = _source_directories(root)
    signature = (os.environ.get("MV2_PLUGIN_PATH", ""), safe_mode(),
                 _stat(config.path(root)) if root is not None else None,
                 tuple((source, str(directory), _stat(directory / manifests.FILE)) for source, directory in found))
    with _lock:
        cached = _declared.get(root)
    if cached and cached[0] == signature:
        return list(cached[1])
    settings = config.load(root) if root is not None else config.Config()
    result = []
    for source, directory in found:
        try:
            manifest = manifests.load(directory)
        except manifests.ManifestError:
            continue
        default = manifest.default if source == "bundled" else "off"
        result.append(Declared(manifest.name, source, directory, manifest, settings.is_on(manifest.name, default)))
    with _lock:
        _declared[root] = (signature, result)
    return list(result)


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
    aside = {}
    for name, (source, directory, found, state) in untrusted.items():
        if name in chosen:
            shadowed.setdefault(name, []).append((source, directory))
            aside.setdefault(name, []).append((source, directory, state))
        else:
            chosen[name] = (source, directory, found)
            trusted[name] = None
    result = {}
    for name, (source, directory, found) in chosen.items():
        # A pre-plugin settings file turns nothing on: its first-use conversion writes the switch into plugins.json.
        default = found.default if source == "bundled" else "off"
        on = settings.is_on(name, default)
        try:
            result[name] = _entry(root, name, on, source, directory, found, shadowed.get(name, []), trusted.get(name))
        except Exception as error:  # whatever happens with one plugin, every other one is still listed
            text = str(error).strip().splitlines()
            result[name] = Entry(name, "failed", on, source, directory, found, problem=(
                f"{name} could not be read: {type(error).__name__}" + (f": {text[0]}" if text else "")),
                shadowed=shadowed.get(name, []))
        result[name].set_aside = aside.get(name, [])
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
    if isinstance(known, Loaded):  # a failure whose files changed since is tried again: the plugin is on, unloaded
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


def access_level(root, found):
    """The access level plugin `found` has in universe `root`: None where it is off; else the level granted there
    (none recorded: what it requests), never above its request, capped by the universe's access_ceiling and by
    MV2_EFFECTS for this process.

    Decided from one read of plugins.json, whether it is on included, not from what `found` was read with: a
    file torn or changed since then never hands back access (an unusable read is off, so no access, while a
    missing file means the defaults)."""
    if found is None or not found.on or found.manifest is None:
        return None
    settings = config.load(root) if root is not None else config.Config()
    if not settings.is_on(found.name, found.manifest.default if found.source == "bundled" else "off"):
        return None
    request = found.manifest.access.request
    granted = settings.access.get(found.name, request)
    return levels.lowest(*(level for level in (granted, request, settings.ceiling, levels.process_ceiling())
                           if level is not None))


def limits(root, found):
    """[(what, level)] of what caps plugin `found` below its request here, strongest first: ("MV2_EFFECTS", level)
    for this process, ("access_ceiling", level) for the universe's plugins.json, ("grant", level) for its own
    grant. Empty where nothing does, or it is off."""
    if found is None or not found.on or found.manifest is None:
        return []
    request = found.manifest.access.request
    settings = config.load(root) if root is not None else config.Config()
    found_limits = [("MV2_EFFECTS", levels.process_ceiling()), ("access_ceiling", settings.ceiling),
                    ("grant", settings.access.get(found.name))]
    return [(what, level) for what, level in found_limits
            if level is not None and levels.rank(level) < levels.rank(request)]


def raise_hint(root, name, needs):
    """How a person gives plugin `name` `needs` access here: the change that would actually do it, naming what
    holds it below (MV2_EFFECTS, the universe's access_ceiling, its grant), or that it never asks for that much."""
    try:
        found = entry(root, name)
    except (PluginError, OSError, ValueError):
        found = None
    request = found.manifest.access.request if found is not None and found.manifest is not None else None
    if request is not None and levels.rank(request) < levels.rank(needs):
        return f"it asks for at most {request}, so no grant gives it {needs}"
    caps = [(what, level) for what, level in (limits(root, found) if found is not None else [])
            if levels.rank(level) < levels.rank(needs)]
    if caps and caps[0][0] == "MV2_EFFECTS":
        return f"MV2_EFFECTS={caps[0][1]} caps every plugin in this process: run it without MV2_EFFECTS"
    if caps and caps[0][0] == "access_ceiling":
        return (f"this universe's plugins.json caps every plugin at {caps[0][1]} (access_ceiling): a person can "
                f"raise or remove it there")
    return f"a person can grant it with: mv2 plugins grant {name} {needs}"


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


LOAD_SECONDS = 10.0  # the longest a plugin's import and register() may take; past it, it is left out


class StillLoading(PluginError):
    """The plugin is being loaded by another thread and the caller chose not to wait (longer)."""


def load(found, off=False, wait=None):
    """The Loaded plugin for an Entry that is usable, importing and registering it once per process.

    A plugin whose settings here are unusable still loads, so its screens can fix
    them. `off` also loads a plugin that is off here (still trusted and
    compatible), for what acts before it is turned on, such as migrating its
    pre-plugin settings. Raises PluginError when it cannot be loaded; the failure
    is kept, so later callers get the same answer without importing again.

    The import and register() always run in a daemon thread and get
    LOAD_SECONDS: a plugin that takes longer is left out (failed, tried again in
    another process or once its files change), and never hangs mv2. A caller
    waits for that load until at most `wait` seconds after it started (default:
    until it ends), then StillLoading says it goes on in the background. So
    wait=0 never waits, and many callers with a short wait (a dashboard refresh)
    pay for a slow register() once, not once each: the dashboard's first screen
    is drawn while plugins load, and shows each one on the refresh after it is.
    Under MV2_PLUGINS=off nothing loads, `off` or not.
    """
    if safe_mode():  # an entry that is off here never reaches the safe-mode state, so check it here as well
        raise PluginError(f"{SAFE_MODE}=off: no plugins are loaded")
    if not (found.on and found.state in ("on", "loaded", "unusable")) and not (off and found.state == "off"):
        raise PluginError(found.problem or f"{found.name} is off here")
    directory = Path(found.manifest.directory).resolve()  # a trusted universe plugin runs from its copy
    with _lock:
        known = _loaded.get(directory)
    if isinstance(known, Loaded):
        return known
    files = _files(directory) if isinstance(known, Failed) else None
    if isinstance(known, Failed) and known.files == files:
        raise PluginError(known.problem)
    if wait is None and _known_slow(found):
        wait = KNOWN_SLOW_WAIT  # it hung before with these files: whoever needs it now is not held up again
    done, started = _start_loading(found, directory, files)
    if not done.wait(LOAD_SECONDS + 1 if wait is None else max(0.0, started + wait - time.monotonic())):
        raise StillLoading(f"{found.name} is still loading")
    with _lock:
        known = _loaded.get(directory)
    if isinstance(known, Loaded):
        return known
    raise PluginError(known.problem if isinstance(known, Failed) else f"{found.name} did not load")


def _start_loading(found, directory, files):
    """(Event set when this plugin's load ends, when it started): one load at a time per plugin, in a daemon
    thread, started here unless one runs or the plugin is already loaded (or failed with these same files)."""
    with _lock:
        running = _loading.get(directory)
        if running is not None:
            return running
        known = _loaded.get(directory)
        if isinstance(known, Loaded) or (isinstance(known, Failed) and files is not None and known.files == files):
            done = threading.Event()
            done.set()
            return done, time.monotonic()
        done, started = threading.Event(), time.monotonic()
        _loading[directory] = (done, started)
    home = trust.user_directory()  # read here, in the caller's thread: the load may start after HOME changed

    def run():
        try:
            _load(found, directory, home)
        except BaseException:
            pass  # kept as Failed: callers say why
        finally:
            with _lock:
                _loading.pop(directory, None)
            done.set()

    threading.Thread(target=run, name=f"plugin {found.name}: loading", daemon=True).start()
    return done, started


def _load(found, directory, home=None):
    """Import and register a plugin in a thread of its own, waiting LOAD_SECONDS for it.

    A load past the limit is failed for now, not for good: its thread goes on, and what it ends with lands when
    it ends (a slow cold import on a slow disk is loaded then; an error replaces the timeout). A hang is also
    remembered against the plugin's files (LOAD_TIMEOUTS), so the next process does not wait the limit again."""
    from . import api  # the API imports this module
    with _lock:
        attempt = _attempts.get(directory, 0)
        _attempts[directory] = attempt + 1
    # Every attempt after the first imports under a package of its own: one still running late (or what it left in
    # sys.modules) can never hand a later attempt, after an edit, the code it started with.
    package = _package_name(found) + (f"_{attempt}" if attempt else "")
    registrations = Registrations()
    _keep_bytecode_out(directory)
    outcome, done, gate, late, timed_out = {}, threading.Event(), threading.Lock(), [], []
    files_at_start = _files(directory)
    # What this load records goes to the home it was asked for under (the caller's), never a later one.
    home = trust.user_directory() if home is None else home

    def forget_modules():
        for name in [name for name in sys.modules if name == package or name.startswith(package + ".")]:
            del sys.modules[name]  # tried again from scratch once its files change

    def land(loaded=None, error=None):
        """Keep what a load ended with. One that ends late is kept only while it is still the plugin's latest
        attempt and its files are still those it started with."""
        if late:
            with _lock:
                superseded = not timed_out or _loaded.get(directory) is not timed_out[0]
            if superseded or _files(directory) != files_at_start:
                forget_modules()  # this attempt's own package: a later one has its own
                return None
        if error is not None:
            forget_modules()
            problem = f"{found.name} failed to load: {_describe(error)}"
            with _lock:
                _loaded[directory] = Failed(found.manifest, found.source, problem, _files(directory))
                _changed()
            return problem
        with _lock:
            _loaded[directory] = loaded
            _changed()
        _remember_slow(directory, None, home)
        return None

    def run():
        try:
            _make_package(package, directory)
            module = importlib.import_module(f"{package}.{found.manifest.module}") if found.manifest.module else None
            loaded = Loaded(found.manifest, found.source, package, module, registrations)
            register = getattr(module, "register", None) if module is not None else None
            if module is not None and not callable(register):
                raise PluginError(f"{found.manifest.module} has no register(mv) function")
            if register is not None:
                register(api.Plugin(loaded))
            outcome["loaded"] = loaded
        except BaseException as error:  # SystemExit included: a plugin never ends the process by failing to load
            outcome["error"] = error
        finally:
            with gate:
                done.set()
                if late:  # the caller went on without it: what it ended with lands now
                    land(outcome.get("loaded"), outcome.get("error"))

    threading.Thread(target=run, name=f"plugin {found.name}: load", daemon=True).start()
    finished = done.wait(LOAD_SECONDS)
    with gate:
        if not finished and not done.is_set():
            late.append(True)
            error = TimeoutError(f"took longer than {LOAD_SECONDS:g} s to load")
            problem = f"{found.name} failed to load: {_describe(error)}"
            timed_out.append(Failed(found.manifest, found.source, problem, files_at_start))
            with _lock:
                _loaded[directory] = timed_out[0]
                _changed()
            _remember_slow(directory, _files(directory), home)
            raise PluginError(problem) from error
    if "error" in outcome:
        raise PluginError(land(error=outcome["error"])) from outcome["error"]
    land(outcome["loaded"])
    return outcome["loaded"]


LOAD_TIMEOUTS = "plugin-load-timeouts.json"  # in ~/.multiverse: plugins whose load ran past LOAD_SECONDS
KNOWN_SLOW_WAIT = 0.5  # seconds a caller that would wait in full waits for a plugin that hung before
KNOWN_SLOW_FOR = 600  # seconds a hang is remembered: then one caller waits in full again (a short process such as
# `mv2 spawn` ends before a load it waited 0.5 s for could clear the mark)


def _digest(files):
    return hashlib.sha256(repr(files).encode()).hexdigest()


def _remember_slow(directory, files, home=None):
    """Record (files given) or forget (None) that this plugin's load ran past the limit, in `home` (the
    ~/.multiverse the load started under), and never in a home that is gone (a test's, removed). Never raises."""
    from ..store import lock, read, write
    home = Path(home) if home is not None else trust.user_directory()
    if not home.parent.is_dir():
        return
    path = home / LOAD_TIMEOUTS
    try:
        if files is None:  # forgetting: nothing is written (not even the lock) unless the plugin is there
            try:
                if str(directory) not in (read(path).get("plugins") or {}):
                    return
            except (OSError, ValueError, AttributeError):
                return
        with lock(path.with_name(f".{LOAD_TIMEOUTS}.lock")):
            try:
                plugins = read(path).get("plugins") or {}
            except (OSError, ValueError, AttributeError):
                plugins = {}
            if files is None:
                if str(directory) not in plugins:
                    return
                plugins.pop(str(directory), None)
            else:
                plugins[str(directory)] = {"files": _digest(files), "at": now(), "until": time.time() + KNOWN_SLOW_FOR}
            write(path, {"schema": 1, "plugins": plugins})
    except (OSError, ValueError):
        pass


def _known_slow(found):
    """Whether this plugin's load ran past the limit before, with these same files."""
    from ..store import read
    try:
        recorded = (read(trust.user_directory() / LOAD_TIMEOUTS).get("plugins") or {})
    except (OSError, ValueError, AttributeError):
        return False
    directory = Path(found.manifest.directory).resolve()
    item = recorded.get(str(directory))
    until = item.get("until") if isinstance(item, dict) else None
    return (isinstance(until, (int, float)) and not isinstance(until, bool) and until > time.time()
            and item.get("files") == _digest(_files(directory)))


def _keep_bytecode_out(directory):
    """Importing from a universe's own plugin directory (a developer's MV2_PLUGIN_PATH, check --load) never
    writes __pycache__ there, which would change what was trusted: this process's bytecode goes to
    ~/.multiverse/pycache instead. Python reads the prefix at every import, lazy ones included."""
    if sys.pycache_prefix is None and directory.parent.name == "plugins" \
            and (directory.parent.parent / "universe.json").is_file():
        sys.pycache_prefix = str(trust.user_directory() / "pycache")


def loaded_by_package(package):
    """The Loaded plugin whose package (`mv2_plugins.NAME`, as in a traceback's module names) is `package`, else
    None. Never loads anything."""
    with _lock:
        for known in _loaded.values():
            if isinstance(known, Loaded) and (package == known.package or package.startswith(known.package + ".")):
                return known
    return None


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


def loading(found):
    """Whether this plugin's load is running in the background now (an Entry with a manifest)."""
    if found.manifest is None:
        return False
    with _lock:
        return Path(found.manifest.directory).resolve() in _loading


def generation():
    """A number that rises whenever a plugin finishes loading or fails to: what keeps plugin registrations (the
    dashboard's palette and screens) reads them again when it changes, for plugins that loaded in the background."""
    return _generation


def active(root, wait=None):
    """The plugins on in this universe, loaded, sorted by name. One that fails to load is left out.

    Every plugin not loaded yet starts loading at once, side by side, and this waits for them at most `wait`
    seconds in all (default: until each load ends, within LOAD_SECONDS); one still loading then is left out of
    this answer only. What must include every plugin (a launch plan) waits; what shows a plugin on its next
    refresh (the dashboard) waits REFRESH_WAIT, so a register() that hangs costs its first screen that at most."""
    if safe_mode():
        return []
    usable = [found for found in entries(root) if found.usable]
    for found in usable:
        try:
            load(found, wait=0)  # starts its load in the background, unless it is loaded already
        except PluginError:
            pass
    deadline = None if wait is None else time.monotonic() + max(0.0, wait)
    result = []
    for found in usable:
        try:
            result.append(load(found, wait=None if deadline is None else max(0.0, deadline - time.monotonic())))
        except PluginError:
            continue
    return result


def _describe(error):
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


ERRORS_LOG_BYTES = 1024 * 1024  # errors.log rotates to errors.log.1 past this
LOG_BYTES = 1024 * 1024  # plugin.log, events.log and host.log rotate to NAME.1 past this


def log_lock_path(path):
    path = Path(path)
    return path.with_name(f".{path.name}.lock")


def append_log(path, line, limit=None, root=None):
    """Append text to a log, rotating it to NAME.1 (one old file kept) once it is past `limit` (LOG_BYTES).

    The size check, the rotation and the append happen under a file lock, so two processes writing one log never
    both rotate it (which would overwrite NAME.1, or rename a file that is already gone). A log in a universe's
    plugin state (or under `root`) that is gone is not written: a log line never brings a removed universe or
    agent back (agent_files)."""
    from . import agent_files
    path = Path(path)
    guarded = agent_files.required_folder(path, root)[0] is not None
    try:
        agent_files.ensure_dir(path.parent, root)
        with _log_lock, agent_files.lock(log_lock_path(path), root):
            if path.exists() and path.stat().st_size > (LOG_BYTES if limit is None else limit):
                path.replace(path.with_name(path.name + ".1"))
            with path.open("a") as handle:
                handle.write(line.rstrip("\n") + "\n")
    except FileNotFoundError:
        if not guarded:
            raise  # removed meanwhile, with the folder it must find
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
    entry = (f"{now()} {text}" + (f" ({quiet} more like it not logged)" if quiet else "") + "\n"
             + "".join(traceback.format_exception(error)).rstrip())
    try:
        append_log(Path(root) / "state" / "plugins" / loaded.name / "errors.log", entry, ERRORS_LOG_BYTES)
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


def still_running(loaded, key):
    """Whether a guarded call of `loaded` with this key has not returned yet."""
    with _running_lock:
        return (id(loaded), key) in _running


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
        _declared.clear()
        _loading.clear()
        # _attempts stays: an attempt still running late (a test's hung register()) must never share a package
        # name with a later one, whose modules it would remove when it is dropped.
        for name in [name for name in sys.modules if name == PACKAGE or name.startswith(PACKAGE + ".")]:
            del sys.modules[name]
