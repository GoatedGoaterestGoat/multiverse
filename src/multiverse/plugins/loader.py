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
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import types

from ..store import now, open_regular, retry_missing, is_file_replaced
from . import access as levels, config, manifest as manifests, settings as settings_files, trust

API_VERSION = (1, 21)
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
_discovering = threading.local()  # the universes this thread is finding plugins for now
_loaded = {}  # resolved plugin directory -> Loaded or Failed
_running = set()  # (id(Loaded), what) of guarded calls still running past their time budget
_running_lock = threading.Lock()
_late = {}  # running key -> (late tag, "ok", value) or (tag, "error", error): a call that timed out, ended since
_loading = {}  # resolved plugin directory -> (Event set when its load in the background ends, when it started)
_attempts = {}  # resolved plugin directory -> loads started for it in this process
_log_lock = threading.Lock()
_entries = {}  # root -> (signature, entries): entries() is asked for on every dashboard refresh
_generation = 0  # rises whenever a plugin is loaded or fails to load
SYNCHRONOUS_BUDGET = 2.0
REFRESH_WAIT = 0.5  # seconds a dashboard refresh waits, in all, for plugins still loading
UNUSABLE = "its settings cannot be used: "  # how an `unusable` entry's problem begins, after the plugin's name  # seconds a guarded call may take where a person waits on it


# A child that runs a module of this mv2 as __main__, with this mv2 first on its path rather than whatever mv2 the
# interpreter finds itself (MV2_SOURCE's rule): the source folder is an argument, never part of the code.
_RUN_MODULE = ("import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
               "runpy.run_module(sys.argv.pop(1), run_name='__main__', alter_sys=True)")


def module_command(module, *arguments):
    """The argv that runs `module` of this running mv2, like `python -m module ARGUMENTS`."""
    import multiverse
    return [sys.executable, "-c", _RUN_MODULE, multiverse.source(), module, *map(str, arguments)]


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
    packages: str | None = None  # its packages' problem, when that is why: tried again once it changes (1.20)


def _still_failed(known, files):
    """Whether a failed plugin stays failed: its files unchanged, and, if its packages were why, their problem too
    (installed since, or another plugin's version gone, means trying again; until then it reads as failed)."""
    if not isinstance(known, Failed) or files is None or known.files != files:
        return False
    if known.packages is None:
        return True
    from . import packages
    return packages.problem(known.manifest) == known.packages


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
    if root is not None:
        found = _with_recorded(root, found)
    return found


def _recorded(root, directory, name=None, source=None, pinned=None):
    """Whether `directory` must run at its pinned content, however this process found it: a folder the universe's
    registry records. For a plugin found by a source another process may lack (MV2_PLUGIN_PATH, a package), also
    when the registry can't be read (nothing proves it isn't recorded), when the registry records a folder for its
    name (a folder put in place of the recorded one isn't it), or when its folder is pinned (`pinned`: the paths
    trusted-plugins.json pins, None when that can't be read): then a replacement reads as untrusted, never as an
    ordinary plugin."""
    if root is None:
        return False
    from . import registry
    try:
        folder = str(Path(directory).resolve())
    except (OSError, RuntimeError):
        return source is not None and source not in registry.SHARED
    known = registry.known(root)
    # The recorded entry itself, as well as where it leads now: an entry relinked to a plugin of another name is
    # still the recorded one, verified against what was pinned there, never an ordinary plugin of that name.
    from .installer import entry_path
    try:  # where it leads, the entry as given, and the entry under its parent's real folder (an alias of it)
        spellings = {folder, os.path.abspath(directory), str(entry_path(directory))}
    except (OSError, RuntimeError):
        spellings = {folder, os.path.abspath(directory)}
    if known is not None and any(value["directory"] in spellings for value in known.values()):
        return True
    if source is None or source in registry.SHARED:
        return False
    if known is None and os.path.lexists(registry.path(root)):  # a dangling link there is unreadable, not absent
        return True  # a registry there that can't be read proves nothing either way
    if name is not None and (known or {}).get(name, {}).get("directory") is not None:
        return True
    paths = pinned() if callable(pinned) else pinned
    return paths is None or folder in paths


def _pinned_paths():
    """A reader of the paths trusted-plugins.json pins: read the first time it's asked, once (no wait when it was
    never written), None when it can't be read (then every plugin of a source another process may lack is
    verified)."""
    found = []

    def paths():
        if not found:
            from . import installer
            try:
                found.append(set(installer._pins()))
            except installer.InstallError:
                found.append(None)
        return found[0]
    return paths


def _with_recorded(root, found):
    """`found` plus the folders the universe's registry records for plugins that are on there, each right after the
    source that found it in the person's process: a plugin in their MV2_PLUGIN_PATH, or a package of another
    Python, is then a candidate in every process here. A candidate only: it runs at the content pinned for it, as
    a universe plugin does (_entries_uncached)."""
    from . import registry
    recorded = registry.candidates(root)
    if not recorded:
        return found
    seen = set()
    for _, directory in found:
        try:
            seen.add(directory.resolve())
        except (OSError, RuntimeError):
            continue
    order = ("path", "universe", "personal", "installed", "checkout", "bundled")
    rank = lambda source: order.index(source) if source in order else 0  # noqa: E731
    result = [(source, directory, rank(source)) for source, directory in found]
    for source, directory in recorded:
        if directory in seen or not (directory / manifests.FILE).is_file():
            continue
        seen.add(directory)
        # Where its own source's plugins would be, whether or not this process finds any of them.
        position = sum(1 for _, _, other in result if other <= rank(source))
        result.insert(position, ("recorded", directory, rank(source)))
    return [(source, directory) for source, directory, _ in result]


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
        data = retry_missing(lambda: read(path))
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


def _signature(root, pins=None, sources=None):
    """Everything entries() depends on, by file times and sizes: cheap enough for every refresh. `pins`, the
    installer's classification entries() read for this call (_pinned)."""
    parts = [os.environ.get("MV2_PLUGIN_PATH", ""), safe_mode(), _generation, _stat(trust.user_directory() / trust.FILE)]
    if root is not None:
        parts.append(tuple((path.name, _stat(path)) for path in sorted(Path(root).glob("*.json"))))
        parts.append(tuple((path.name, _stat(path)) for path in sorted((Path(root) / settings_files.DIRECTORY).glob("*.json"))))
        # A harness's settings hold the person's model choices, which a plugin's settings check may judge by
        # (harness.check): a change there must run those checks again.
        harness_files = sorted((Path(root) / "harnesses").glob("*/harness.json"))
        parts.append(tuple((path.parent.name, _stat(path)) for path in harness_files))
        # Which secrets are set (1.20): a required one set or cleared, here or in a parent, by any process, changes
        # whether the plugin can be used. The index is rewritten on each, whatever store holds the value.
        from . import secrets as secret_store
        parts.append(_stat(secret_store.directory() / secret_store.INDEX))
    # What the installer placed (personal, and this universe's): pinned at its content, as trusted plugins are.
    from . import installer
    parts.append(_stat(trust.user_directory() / installer.RECORD))
    if root is not None:
        parts.append(_stat(Path(root) / "state" / installer.RECORD))
    # Each folder classed as installed adds its trust signature below, so the classification is part of the
    # signature. An uncertain read only ever widens it (every personal folder verified): a listing kept from one
    # differs from any certain one, and is read again.
    pins, _ = pins if pins is not None else _pinned(root)
    from . import registry
    if root is not None:
        parts.append(_stat(registry.path(root)))  # whether a plugin is recorded decides how it is verified
    with _lock:
        failed = {directory: known for directory, known in _loaded.items() if isinstance(known, Failed)}
    for source, directory in (_source_directories(root) if sources is None else sources):
        parts.append((source, str(directory), _stat(directory / manifests.FILE)))
        if directory.resolve() in failed:  # a plugin that failed to load is tried again once its files change
            parts.append(_still_failed(failed[directory.resolve()], _files(directory.resolve())))
        if (source in ("universe", "recorded") or _managed(directory, pins) or _recorded(root, directory)
                or source not in registry.SHARED):  # a plugin another process may not find: verified when pinned
            # trust covers every file but .git
            try:
                parts.append(trust.signature(directory))
            except OSError:
                parts.append(None)
    # A failure for its packages, wherever it runs from (a trusted copy too), is tried again once their problem
    # changes, not only its files.
    parts.append(tuple(sorted(str(directory) for directory, known in failed.items()
                              if known.packages is not None and not _still_failed(known, known.files))))
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


def manifest_records(root):
    """Readable manifests from every copy, without importing code or reading enablement/grants."""
    for _, directory in _source_directories(root):
        try:
            yield manifests.load(directory)
        except manifests.ManifestError:
            continue


def legacy_files(root):
    """Legacy filenames from manifests alone, regardless of enablement, grants or load state."""
    return {manifest.settings.legacy for manifest in manifest_records(root)
            if manifest.settings is not None and manifest.settings.legacy}


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
    settings = config.load(root, observed=True) if root is not None else config.Config()
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
    # One look at the plugin folders and the installer's maps, for this call: its signature and its entries use
    # the same answer, so a folder arriving meanwhile can't skip the checks.
    sources = _source_directories(root)
    pins = _pinned(root, sources)
    certain = pins[1]
    signature = _signature(root, pins, sources)
    with _lock:
        cached = _entries.get(root) if certain else None  # an uncertain read neither uses nor leaves a listing
    if cached and cached[0] == signature:
        return list(cached[1])
    discovering = _discovering.__dict__.setdefault("roots", set())
    if root in discovering:
        # Asked again while this thread finds this universe's plugins: a loaded plugin's settings check calling the
        # API, whose access check asks which plugins are on. Answered without running settings checks again, which
        # would ask again, without end; on/off, the source and the grant come out the same.
        return list(_entries_uncached(root, checks=False, pins=pins, sources=sources))
    discovering.add(root)
    try:
        found = _entries_uncached(root, pins=pins, sources=sources)
    finally:
        discovering.discard(root)
    if certain:
        with _lock:
            _entries[root] = (signature, found)
    return list(found)


def _pinned(root, sources=None):
    """(directories, certain): the personal plugin directories the installer placed (by its record, its pin or
    its marker), verified at their pinned content as universe plugins are at what was trusted. Read only when a
    listing finds a folder in the personal plugins; entries() reads it once and shares it with its signature and
    its construction."""
    from . import installer
    sources = _source_directories(root) if sources is None else sources
    return installer.managed_personal([directory for _, directory in sources])


def _managed(directory, pins):
    """Whether a plugin directory the loader just found, by whichever source (personal, or MV2_PLUGIN_PATH naming
    the same folder), is one the installer placed: in what was read of its records and pins, or a folder in the
    personal plugins carrying the marker that arrives with it. Decided by the folder itself, never its label."""
    from . import installer
    return (str(installer.entry_path(directory)) in pins or str(Path(directory).resolve()) in pins
            or (installer.in_personal(directory) and installer.marked(directory)))


def _entries_uncached(root, checks=True, pins=None, sources=None):
    settings = config.load(root, observed=True) if root is not None else config.Config()
    chosen, shadowed, invalid, untrusted, trusted = {}, {}, {}, {}, {}
    pins = (pins if pins is not None else _pinned(root))[0]
    pinned = _pinned_paths()
    for source, directory in (_source_directories(root) if sources is None else sources):
        try:
            found = manifests.load(directory)
        except manifests.ManifestError as error:
            invalid.setdefault(directory.name, (source, directory, str(error)))
            continue
        if (source in ("universe", "recorded") or _managed(directory, pins)
                or _recorded(root, directory, found.name, source, pinned)) and found.name not in chosen:
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
            result[name] = _entry(root, name, on, source, directory, found, shadowed.get(name, []), trusted.get(name),
                                  checks=checks, pinned=source != "universe" and _managed(directory, pins),
                                  recorded=_recorded(root, directory, name, source, pinned))
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


def _entry(root, name, on, source, directory, found, shadowed, fingerprint=None, checks=True, pinned=False,
           recorded=False):
    entry = Entry(name, "on" if on else "off", on, source, directory, found, shadowed=shadowed)
    recorded = recorded or source == "recorded"
    if source == "universe" or recorded or pinned:
        trusted = "trusted" if fingerprint else trust.state(directory)
        if trusted != "trusted" and pinned:
            entry.state = "changed" if trusted == "changed" else "untrusted"
            entry.problem = (f"{name}'s files changed since mv2 plugins install put them in {directory}; review "
                             f"them, then run mv2 plugins trust {name}, or put back what was installed with mv2 "
                             f"plugins update {name}" if trusted == "changed" else
                             f"{name} was installed by mv2 plugins install but is no longer trusted; review it, then "
                             f"run mv2 plugins trust {name}")
            return entry
        if trusted != "trusted" and recorded and not pinned:
            entry.state = trusted
            entry.problem = (f"{name} is on here from {directory}, which changed since it was trusted" if
                             trusted == "changed" else f"{name} is on here from {directory}, which isn't trusted") + (
                f"; review it, then run mv2 plugins trust {name}")
            return entry
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
    if _still_failed(known, _files(running)):
        entry.state, entry.problem = "failed", known.problem
        return entry
    if isinstance(known, Loaded):  # a failure whose files changed since is tried again: the plugin is on, unloaded
        entry.state, entry.loaded = "loaded", known
    if checks and root is not None and found.settings is not None:
        # Unusable settings: the plugin acts as off in this universe, with the reason, naming the plugin and
        # the universe's own paths.
        problem = settings(root, entry).problem
        if problem:
            detail = problem.replace(f"{Path(root)}/", "")
            if detail.startswith(f"{name}: "):
                detail = detail[len(name) + 2:]
            entry.state, entry.problem = "unusable", f"{name}: {UNUSABLE}{detail}"
    if checks and root is not None and found.secrets and entry.state != "unusable":
        missing = missing_secret(root, found)  # `found` is the plugin's manifest here
        if missing:
            entry.state, entry.problem = "unusable", missing
    return entry


def missing_secret(root, manifest):
    """Why the plugin can't work here for want of a required secret (1.20), naming how to set it; or None."""
    from . import secrets
    for secret in manifest.secrets:
        if not secret.required:
            continue
        try:
            found = secrets.where(root, manifest.name, secret.key)
        except secrets.SecretsError as error:
            return f"{manifest.name}: its {secret.label} can't be read: {error}"
        if found is None:
            return (f"{manifest.name} needs its {secret.label}: set it with mv2 plugins secret {manifest.name} "
                    f"{secret.key}, or with Enter on the Plugins screen")
    return None


def entry(root, name):
    for found in entries(root):
        if found.name == name:
            return found
    raise PluginError(f"No plugin named {name} here. Run mv2 plugins to see the plugins this universe can use.")


def access_level(root, found, *, observed=False):
    """The access level plugin `found` has in universe `root`: None where it is off; else the level granted there
    (none recorded: what it requests), never above its request, capped by the universe's access_ceiling and by
    MV2_EFFECTS for this process.

    Decided from one read of plugins.json, whether it is on included, not from what `found` was read with: a
    file torn or changed since then never hands back access (an unusable read is off, so no access, while a
    missing file means the defaults)."""
    if found is None or not found.on or found.manifest is None:
        return None
    settings = config.load(root, observed=observed) if root is not None else config.Config()
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
    settings = config.load(root, observed=True) if root is not None else config.Config()
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
    if _still_failed(known, files):
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


def ran_from(directory, loaded=False):
    """Whether this process loaded a plugin from `directory`, or is loading one from there (`loaded`: loaded it)."""
    directory = Path(directory).resolve()
    with _lock:
        if loaded:
            return isinstance(_loaded.get(directory), Loaded)
        return directory in _loaded or directory in _loading


def start(found):
    """Start loading `found` in the background, from where it runs, unless it is loaded or loading already."""
    directory = Path(found.manifest.directory).resolve()
    with _lock:
        known = _loaded.get(directory)
    _start_loading(found, directory, _files(directory) if isinstance(known, Failed) else None)


def _start_loading(found, directory, files):
    """(Event set when this plugin's load ends, when it started): one load at a time per plugin, in a daemon
    thread, started here unless one runs or the plugin is already loaded (or failed with these same files)."""
    with _lock:
        running = _loading.get(directory)
        if running is not None:
            return running
        known = _loaded.get(directory)
        if isinstance(known, Loaded) or _still_failed(known, files):
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
                # Its packages' problem is kept too: it reads as failed until that changes, then is tried again.
                _loaded[directory] = Failed(found.manifest, found.source, problem, _files(directory),
                                            str(error) if type(error).__name__ == "PackagesError" else None)
                _changed()
            return problem
        with _lock:
            _loaded[directory] = loaded
            _changed()
        _remember_slow(directory, None, home)
        return None

    def run():
        try:
            from . import packages  # its packages first, after mv2's own on the import path (1.20)
            packages.activate(found.manifest, home)
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
                if str(directory) not in (retry_missing(lambda: read(path)).get("plugins") or {}):
                    return
            except (OSError, ValueError, AttributeError):
                return
        with lock(path.with_name(f".{LOAD_TIMEOUTS}.lock")):
            try:
                plugins = retry_missing(lambda: read(path)).get("plugins") or {}
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
        recorded = (retry_missing(lambda: read(trust.user_directory() / LOAD_TIMEOUTS), observed=True).get("plugins") or {})
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
    in_universe = directory.parent.name == "plugins" and is_file_replaced(directory.parent.parent / "universe.json")
    # A trusted copy too: then only the prefix's bytecode is read, never a __pycache__ placed in the copy, which
    # Python would prefer to the trusted source.
    in_cache = Path(directory).resolve().parent == (trust.user_directory() / trust.CACHE).resolve()
    if sys.pycache_prefix is None and (in_universe or in_cache):
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
    """(path, size, mtime_ns) of a plugin's files, to tell when they changed. Leaves out what Python writes, and
    the trust cache's own marker in a trusted copy, which each use touches though the plugin is unchanged."""
    found = []
    directory = Path(directory)
    for path in sorted(directory.rglob("*")):
        if "__pycache__" in path.parts or ".git" in path.parts or not path.is_file():
            continue
        if path.name == trust.COMPLETE and path.parent == directory:
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
    if type(error).__name__ == "PackagesError":
        return str(error)  # it says what's missing and the command that fixes it
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


ERRORS_LOG_BYTES = 1024 * 1024  # errors.log rotates to errors.log.1 past this
LOG_BYTES = 1024 * 1024  # plugin.log, events.log and host.log rotate to NAME.1 past this


def log_lock_path(path):
    path = Path(path)
    return path.with_name(f".{path.name}.lock")


def append_log(path, line, limit=None, root=None, *, blocking=True, wait=0.0):
    """Append text to a log, rotating it to NAME.1 (one old file kept) once it is past `limit` (LOG_BYTES).

    The size check, the rotation and the append happen under a file lock, so two processes writing one log never
    both rotate it (which would overwrite NAME.1, or rename a file that is already gone). A log in a universe's
    plugin state (or under `root`) that is gone is not written: a log line never brings a removed universe or
    agent back (agent_files). With blocking=False, skip a line when either log lock is busy, after waiting at most
    `wait` seconds for them (none by default: recovery and the dashboard log this way and never wait)."""
    from . import agent_files
    path = Path(path)
    guarded = agent_files.required_folder(path, root)[0] is not None
    try:
        agent_files.ensure_dir(path.parent, root)
        deadline = None if blocking else time.monotonic() + wait
        if not (_log_lock.acquire() if deadline is None else
                _log_lock.acquire(timeout=max(deadline - time.monotonic(), 0.0))):
            return
        try:
            with ExitStack() as held:
                while True:  # only taking the lock is tried again, never the write
                    try:
                        held.enter_context(agent_files.lock(log_lock_path(path), root, blocking=deadline is None))
                        break
                    except BlockingIOError:
                        left = None if deadline is None else deadline - time.monotonic()
                        if left is None or left <= 0:
                            raise
                        time.sleep(min(0.005, left))
                        if time.monotonic() >= deadline:  # never a try past the deadline
                            raise
                if path.exists() and path.stat().st_size > (LOG_BYTES if limit is None else limit):
                    path.replace(path.with_name(path.name + ".1"))
                with open_regular(path, "a") as handle:
                    handle.write(line.rstrip("\n") + "\n")
        finally:
            _log_lock.release()
    except BlockingIOError:
        if blocking:
            raise
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


LATE_KEPT = 256  # late ends kept for a later call with the same late tag to answer with, oldest dropped


def guarded(loaded, what, function, *args, root=None, timeout=SYNCHRONOUS_BUDGET, key=None, late=None, **kwargs):
    """Call plugin code within a time budget; the one way core calls into a plugin where something waits on it.

    The call runs in a daemon thread of its own (never an executor, which a hung
    call would exhaust and which would hold up the process's exit), and the
    caller waits at most `timeout` seconds. Python cannot stop the thread: until
    it ends, a call with the same plugin and `key` (default: `what`) is `busy`.
    SystemExit and other errors from plugin code are recorded, never raised.
    With timeout=None the call runs in the calling thread.

    `late` is for a caller that calls again with the same arguments after a timeout (an event handler): a tag
    naming what the call is for (the event: its journal and sequence). A call that timed out and has ended
    since answers the next call with that key and the same tag, which then starts nothing, so work a slow call
    did finish is never done twice; a next call with another tag drops that end and runs. Calls with one key
    still never run at once, whatever their tags."""
    if timeout is None:
        try:
            return Outcome(function(*args, **kwargs))
        except (Exception, SystemExit) as error:
            return Outcome(None, "error", record(loaded, what, error, root))
    running_key = (id(loaded), what if key is None else key)
    with _running_lock:
        if running_key in _running:
            return Outcome(None, "busy")
        ended = _late.pop(running_key, None) if late is not None else None
        if ended is not None and ended[0] != late:
            ended = None  # the end of a call for something else (another event, another journal): not this one's
        if ended is None:
            _running.add(running_key)
    if ended is not None:  # the call that timed out has ended since: its answer, nothing called again
        _, kind, value = ended
        return Outcome(None, "error", record(loaded, what, value, root)) if kind == "error" else Outcome(value)
    outcome, finished, abandoned = {}, threading.Event(), []

    def call():
        try:
            outcome["value"] = function(*args, **kwargs)
        except BaseException as error:  # SystemExit included: a plugin never ends the process from here
            outcome["error"] = error
        finally:
            with _running_lock:
                _running.discard(running_key)
                if abandoned:  # its caller timed out: keep how it ended for the next call with this key
                    _late[running_key] = (late, "error", outcome["error"]) if "error" in outcome else \
                        (late, "ok", outcome.get("value"))
                    while len(_late) > LATE_KEPT:
                        _late.pop(next(iter(_late)))
            finished.set()

    threading.Thread(target=call, name=f"plugin {loaded.name}: {what}", daemon=True).start()
    if not finished.wait(timeout):
        with _running_lock:
            # Decided under the lock the call ends under, so an end just after the wait is never lost: either it
            # has ended (answered below), or it is marked abandoned and keeps its end for the next call.
            still = running_key in _running
            if still and late is not None:
                abandoned.append(True)
        if still:
            return Outcome(None, "timeout", record(loaded, what, TimeoutError(f"took longer than {timeout:g} s"),
                                                   root))
        finished.wait()
    if "error" in outcome:
        return Outcome(None, "error", record(loaded, what, outcome["error"], root))
    return Outcome(outcome.get("value"))


def running_calls(loaded):
    """How many guarded calls of `loaded` have not returned yet, those whose caller timed out too."""
    with _running_lock:
        return sum(1 for owner, _ in _running if owner == id(loaded))


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
