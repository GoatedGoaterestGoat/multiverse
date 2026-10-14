"""mv2 plugins: what core asks of them, one function per extension point.

Plugins themselves import only `multiverse.plugins.api`. Core calls the
functions here, always with the universe the question is about, so a plugin
counts only where that universe's plugins.json turns it on. Every call into
plugin code is guarded: a failure is recorded against the plugin and the
answer goes on without it. See docs/plugins.md.
"""
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
import tempfile
from pathlib import Path

from . import config, loader, settings as settings_files
from .loader import PluginError, entries, entry

CATALOG_KINDS = ("skills", "commands", "workflows")


def _usable(root):
    return [] if loader.safe_mode() else [found for found in entries(root) if found.usable]


def commands(root, reserved=()):
    """{command: (plugin, help)} that plugins on here declare, from their manifests alone.

    A name core uses, or one two plugins declare, is left out; `problems` reports it.
    """
    claimed = {}
    for found in _usable(root):
        for name, text in found.manifest.commands.items():
            claimed.setdefault(name, []).append((found.name, text))
    return {name: owners[0] for name, owners in claimed.items() if name not in reserved and len(owners) == 1}


def command(root, name, reserved=(), guarded=False):
    """The loader.Command a plugin registered for `mv2 NAME`; PluginError when there is none.

    `guarded`: its functions record an unexpected exception against the plugin
    and raise PluginError with it, for a plain `mv2: ...` and exit 1. The
    errors a command means to give (ValueError, OSError, KeyError,
    RuntimeError) pass through as they do for built-in commands.
    """
    owner = commands(root, reserved).get(name)
    if owner is None:
        raise PluginError(f"No plugin here provides mv2 {name}")
    loaded = loader.load(entry(root, owner[0]))
    found = loaded.registrations.commands.get(name)
    if found is None:
        raise PluginError(f"{loaded.name} declares mv2 {name} but did not register it")
    if not guarded:
        return found

    def guard(function, what, passing):
        def call(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except passing:
                raise
            except Exception as error:
                text = loader.record(loaded, f"mv2 {name} {what}", error, root)
                raise PluginError(f"plugin {loaded.name}: {text}") from error
        return call
    expected = (OSError, ValueError, KeyError, RuntimeError)
    return loader.Command(name, guard(found.configure, "options", ()), guard(found.run, "failed", expected),
                          guard(found.cli_run, "failed", expected) if found.cli_run else None)


def catalog_entries(root):
    """Skills, commands and workflows of plugins on here that extend `catalog`, from their directories alone.

    Each entry is {kind, name, description, path, plugin}; the caller decides shadowing.
    """
    from ..store import read
    result = []
    for found in _usable(root):
        if "catalog" not in found.manifest.extends:
            continue
        for kind in CATALOG_KINDS:
            for path in sorted((Path(found.directory) / kind).glob("*/entry.json")):
                try:
                    meta = read(path)
                except (OSError, ValueError):
                    continue
                result.append({"kind": kind, "name": path.parent.name,
                               "description": meta.get("description", "") if isinstance(meta, dict) else "",
                               "path": str(path.parent), "plugin": found.name})
    return result


def bootstrap_lines(root):
    """Lines plugins on here add to an agent's bootstrap, in plugin-name order."""
    lines = []
    for loaded in loader.active(root):
        lines.extend(loaded.registrations.bootstrap)
    return lines


def problems(root, reserved=()):
    """Why plugins here are not working, as sentences: for `mv2 doctor` and the dashboard."""
    found_problems = []
    usable = []
    settings_problem = config.load(root).problem
    if settings_problem:
        found_problems.append(f"plugins.json cannot be used, so every plugin is at its default: {settings_problem}")
    for name, failure in sorted(_failed_conversions(root).items()):
        legacy = Path(root) / str(failure.get("file", ""))
        if failure.get("file") and legacy.is_file() and _fingerprint(legacy) == failure.get("fingerprint"):
            found_problems.append(f"{name}: {failure['file']} could not be converted ({failure.get('reason')}); "
                                  f"it is unchanged: fix it and run mv2 plugins migrate --apply")
    pending = None
    for found in entries(root):
        legacy = (found.manifest.settings.legacy if found.manifest is not None and found.manifest.settings is not None
                  else None)
        if (legacy and (Path(root) / legacy).is_file() and not settings_files.path(root, found.name).exists()
                and found.name not in _failed_conversions(root)):
            # Waiting for its first-use conversion: until then the plugin has neither its switch nor its settings.
            then = (f"nothing is converted while {loader.SAFE_MODE}=off" if loader.safe_mode()
                    else "it is converted the next time mv2 runs here, or now with mv2 plugins migrate --apply")
            found_problems.append(f"{found.name}: {legacy} is still here and not converted yet: {then}.")
            continue
        if found.on and found.state == "missing":
            if pending is None:
                try:
                    pending = submodules()
                except Exception:
                    pending = {}
            hint = f"; it is a submodule of this mv2 checkout: run mv2 plugins install {found.name}" \
                if found.name in pending else ""
            found_problems.append(found.problem + hint)
        elif found.on and found.state not in ("on", "loaded", "safe-mode"):
            found_problems.append(found.problem or f"{found.name} is {found.state}")
        elif found.on and found.state in ("on", "loaded"):
            usable.append(found)
    claimed = {}
    for found in usable:
        for name in found.manifest.commands:
            claimed.setdefault(name, []).append(found.name)
    for name, owners in sorted(claimed.items()):
        if name in reserved:
            found_problems.append(f"{', '.join(owners)} declares mv2 {name}, which mv2 itself uses; it is ignored")
        elif len(owners) > 1:
            found_problems.append(f"{' and '.join(owners)} both declare mv2 {name}; neither is used")
    for found in usable:
        try:
            loader.load(found)
        except PluginError as error:
            found_problems.append(str(error))
    # Loading runs each plugin's own settings check: settings it refuses make it unusable here, said once.
    now = {found.name: found for found in entries(root)}
    for found in usable:
        if found.name in now and now[found.name].state == "unusable":
            found_problems.append(now[found.name].problem)
    try:
        from .harness import item_conflicts
        found_problems.extend(item_conflicts(root))
    except Exception:
        pass
    return found_problems


def doctor(root, universe_for):
    """Problems the plugins' own doctor checks report here, each prefixed with the plugin's name.

    `universe_for(loaded)` gives the api.Universe a check receives.
    """
    found_problems = []
    before = {found.name: found.state for found in entries(root)}
    active = loader.active(root)
    # A plugin's own settings check runs only once its code is loaded, as this just did: report what that finds,
    # which problems() (manifests only) could not see.
    for found in entries(root):
        if found.on and found.state == "unusable" and before.get(found.name) != "unusable":
            found_problems.append(found.problem)
    for loaded in active:
        for check in loaded.registrations.doctor:
            try:
                answer = check(universe_for(loaded))
                if isinstance(answer, str) or not all(isinstance(item, str) for item in answer or []):
                    raise TypeError("a doctor check returns a list of sentences")
            except Exception as error:
                text = loader.record(loaded, "doctor check", error, root)
                found_problems.append(f"{loaded.name}: its {text.replace(': ', ' failed: ', 1)}")
                continue
            found_problems.extend(f"{loaded.name}: {problem}" for problem in answer or [])
    return found_problems


def listing(root):
    """Every plugin this universe sees, for `mv2 plugins` and the dashboard's Plugins screen.

    [{name, on, state, source, version, description, problem, errors, permissions,
      commands, settings: {path, source, problem, schema} or None, shadowed}]
    """
    recent = loader.errors()
    result = []
    for found in entries(root):
        manifest = found.manifest
        item = {"name": found.name, "on": found.on, "state": found.state, "source": found.source,
                "directory": str(found.directory) if found.directory else None,
                "version": manifest.version if manifest else None,
                "description": manifest.description if manifest else "",
                "default": manifest.default if manifest and found.source == "bundled" else "off",
                "problem": found.problem, "errors": recent.get(found.name, []),
                "permissions": manifest.permission_texts() if manifest else [],
                "commands": dict(manifest.commands) if manifest else {},
                "provides": _provides(found),
                "settings": None,
                "shadowed": [{"source": source, "directory": str(directory)} for source, directory in found.shadowed]}
        if manifest is not None and manifest.settings is not None:
            current = loader.settings(root, found)
            try:
                schema, schema_problem = settings_files.schema_of(manifest), None
            except ValueError as error:
                schema, schema_problem = None, str(error)
            item["settings"] = {"path": str(current.path), "source": current.source,
                                "problem": current.problem or schema_problem, "schema": schema,
                                "values": current.values,
                                "screen": (found.loaded.registrations.settings_screen if found.loaded else None),
                                "fields": ([asdict(item) for item in found.loaded.registrations.settings_fields]
                                           if found.loaded and found.loaded.registrations.settings_fields else None)}
        result.append(item)
    return result


# A plugin whose settings may be converted: listed, trusted and compatible (on or off).
MIGRATABLE = ("off", "on", "loaded", "unusable")
MIGRATE_LOCK = ".migrate.lock"
MIGRATE_FAILED = "migrate-failed.json"


def _pending_legacy(root):
    """[(entry, legacy path, target path)] of plugins whose pre-plugin settings file is still here with no settings
    file yet. Manifests and file checks only, no plugin code."""
    found = []
    for entry_ in entries(root):
        manifest = entry_.manifest
        if (manifest is None or manifest.settings is None or not manifest.settings.legacy
                or entry_.state not in MIGRATABLE):
            continue
        legacy, target = Path(root) / manifest.settings.legacy, settings_files.path(root, entry_.name)
        if legacy.is_file() and not target.exists():
            found.append((entry_, legacy, target))
    return found


def awaiting_migration(root):
    """{"plugin-settings/NAME.json": legacy file name} for each plugin whose pre-plugin settings file is still
    here and that has no settings file yet. Manifests only, no plugin code: for `mv2 refresh`, which must not
    add a template's default settings file in front of a universe's real, unmigrated settings."""
    return {target.relative_to(root).as_posix(): legacy.name for _, legacy, target in _pending_legacy(root)}


def _fingerprint(path):
    status = path.stat()
    return [status.st_mtime_ns, status.st_size]


def _failed_conversions(root):
    """{plugin: {"file", "fingerprint", "reason"}} of conversions that failed and were not retried since."""
    try:
        value = json.loads((Path(root) / "state" / "plugins" / MIGRATE_FAILED).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def migrate_on_first_use(root):
    """Convert the pre-plugin settings files of `root` and its subverses, once; returns lines for stderr.

    Runs where mv2 starts work in a universe (`mv2` commands and plugin
    helpers), before the command or helper action runs, never in runners or the
    status line. Nothing pending costs a manifest check and no plugin import.
    A pending file is converted under the universe's migrate lock, planned again
    there, so of two processes one converts and the other finds nothing to do.
    A conversion that fails leaves the file as it was and is said once; it is
    tried again when the file changes, and doctor reports it until then. Never
    raises."""
    from .. import universe
    lines = []
    try:
        if loader.safe_mode():
            return lines
        root = Path(root)
        for scope in universe.tree(root):
            try:
                pending = _pending_legacy(scope)
                failed = _failed_conversions(scope) if pending else {}
                due = [item for item in pending
                       if failed.get(item[0].name, {}).get("fingerprint") != _fingerprint(item[1])]
                if due:
                    lines.extend(_convert(scope, "" if scope == root else _where(root, scope)))
            except Exception as error:  # one universe that cannot be read never stops the rest
                lines.append(f"mv2: could not check {scope} for settings to convert: {error}")
    except Exception as error:
        lines.append(f"mv2: could not check for settings to convert: {error}")
    return lines


def _where(root, scope):
    try:
        return " in " + "/".join(part for part in Path(scope).relative_to(root).parts if part != "subverses")
    except ValueError:
        return f" in {scope}"


def _convert(scope, where):
    from ..store import lock, write
    lines = []
    state = Path(scope) / "state" / "plugins"
    with lock(state / MIGRATE_LOCK):
        failed = _failed_conversions(scope)
        for found, legacy, target in _pending_legacy(scope):  # planned again: another process may have done it
            fingerprint = _fingerprint(legacy)
            if failed.get(found.name, {}).get("fingerprint") == fingerprint:
                continue
            item = {"plugin": found.name, "kind": "legacy", "from": str(legacy), "to": str(target)}
            try:
                done = migrate(scope, item)
            except (PluginError, OSError, ValueError, KeyError) as error:
                reason = " ".join(str(error).split())
                failed[found.name] = {"file": legacy.name, "fingerprint": fingerprint, "reason": reason}
                after = ("it is unchanged: fix it and run mv2 plugins migrate --apply"
                         if getattr(error, "unchanged", True) else
                         f"check {target.relative_to(scope).as_posix()} and {config.path(scope).name} by hand")
                lines.append(f"mv2: {found.name}{where}: could not convert {legacy.name} ({reason}); {after}")
                continue
            failed.pop(found.name, None)
            switch = {True: ", turned it on in plugins.json", False: ", turned it off in plugins.json"}.get(done["on"], "")
            backup = Path(done["backup"]).parent
            lines.append(f"mv2: {found.name}{where}: converted {legacy.name} to "
                         f"{target.relative_to(scope).as_posix()}{switch} (the old file is in "
                         f"{backup.relative_to(scope).as_posix()}/)"
                         + (f"; but {done['warning']}" if done.get("warning") else ""))
        if failed or (state / MIGRATE_FAILED).exists():
            try:
                write(state / MIGRATE_FAILED, failed)
            except OSError:
                pass  # the record only spares repeating a failure: the lines still say what happened
    return lines


def migrations(root):
    """What `mv2 plugins migrate` would do here: [{plugin, kind, from, to, problem?}]. Imports a plugin only to ask."""
    planned = []
    for found in entries(root):
        manifest = found.manifest
        if manifest is None or manifest.settings is None or found.state not in ("off", "on", "loaded", "unusable"):
            continue
        target = settings_files.path(root, found.name)
        legacy = Path(root) / manifest.settings.legacy if manifest.settings.legacy else None
        if not target.is_file() and legacy is not None and legacy.is_file():
            planned.append({"plugin": found.name, "kind": "legacy", "from": str(legacy), "to": str(target)})
        elif target.is_file():
            try:
                version = json.loads(target.read_text()).get("schema")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(version, int) and not isinstance(version, bool) and version < manifest.settings.version:
                planned.append({"plugin": found.name, "kind": "version", "from": str(target), "to": str(target),
                                "version": version})
    for item in planned:
        try:
            loaded = loader.load(entry(root, item["plugin"]), off=True)
            if loaded.registrations.settings_migrate is None:
                item["problem"] = f"{item['plugin']} offers no migration; edit {item['to']} by hand"
        except PluginError as error:
            item["problem"] = str(error)
    return planned


class MigrationError(PluginError):
    """A migration that did not happen. `unchanged` says whether every file is as it was before it began."""

    def __init__(self, message, unchanged=True):
        super().__init__(message)
        self.unchanged = unchanged


@contextmanager
def _in_universe(root):
    """The API's default universe is `root` while a plugin's migration runs: never the caller's ambient one."""
    previous = os.environ.get("MV2_UNIVERSE")
    os.environ["MV2_UNIVERSE"] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MV2_UNIVERSE", None)
        else:
            os.environ["MV2_UNIVERSE"] = previous


def migrate(root, item):
    """Apply one planned migration, so that it either happens or leaves things as they were.

    The plugin's migration runs with `root` as the API's universe. Its result
    is checked, and so is plugins.json when the switch is to be set, before
    anything changes. Then, in this order: the new settings are published
    (never over a settings file that is there or appears meanwhile), the
    switch is set, and only then is the old file copied to trash and removed.
    A step that fails undoes the ones before it, unless the published file is
    no longer exactly the one published, and MigrationError.unchanged says
    which. A conversion whose old file could not be moved to trash is reported
    in the result's `warning`."""
    from ..universe import trash
    root = Path(root)
    found = entry(root, item["plugin"])
    loaded = loader.load(found, off=True)
    function = loaded.registrations.settings_migrate
    if function is None:
        raise MigrationError(f"{found.name} offers no migration")
    source, target, legacy = Path(item["from"]), Path(item["to"]), item["kind"] == "legacy"
    old = json.loads(source.read_text())
    try:
        with _in_universe(root):
            answer = function(old, "legacy" if legacy else item["version"])
    except Exception as error:
        loader.record(loaded, "migrate", error, root)
        raise MigrationError(f"{found.name}'s migration failed: {type(error).__name__}: {error}") from error
    data, on = answer if isinstance(answer, tuple) and len(answer) == 2 else (answer, None)
    if not isinstance(data, dict) or (on is not None and not isinstance(on, bool)):
        raise MigrationError(f"{found.name}'s migration returned something other than settings")
    with _in_universe(root):
        settings_files._accept(found.manifest, data, loaded.registrations.settings_check, item["to"], root)
    switches = config.load(root)
    switch = legacy and on is not None and found.name not in switches.enabled
    if switch and switches.problem:
        raise MigrationError(f"{config.path(root).name} cannot be used, so the switch for {found.name} cannot be "
                             f"set: {switches.problem}")
    previous = None if legacy else target.read_bytes()
    try:
        # A legacy file's settings never go over a settings file that is there or appears meanwhile.
        published = _publish(target, data, overwrite=not legacy)
    except FileExistsError:
        raise MigrationError(f"{target.relative_to(root).as_posix()} appeared meanwhile") from None
    except OSError as error:  # nothing was published: whatever is at the target is not ours
        raise MigrationError(f"{target.name} could not be written: {error}") from error
    if switch:
        try:
            config.set_enabled(root, found.name, on)
        except (OSError, ValueError) as error:
            raise MigrationError(f"the switch in {config.path(root).name} could not be set: {error}",
                                 unchanged=_undo(target, published, previous)) from error
    # Only now, with everything else done, is the old file moved to trash (a copy first, then removed); a version
    # upgrade keeps a copy of the file it replaced.
    backup = trash.entry(root, "plugins-migrate") / source.name
    result = {**item, "on": on if switch else None, "backup": str(backup)}
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(previous if previous is not None else source.read_bytes())
        if legacy:
            source.unlink()
    except OSError as error:
        result["warning"] = (f"{source.name} could not be moved to trash ({error}): remove it yourself" if legacy
                             else f"no copy of the old settings was kept ({error})")
    return result


def _publish(target, data, overwrite=False):
    """Write settings to `target` and return the published file's identity: (device, inode, size, mtime_ns).

    The file is written in full to a temporary file beside it first, and its
    identity taken then, so ownership is never judged from what happens to be at
    the target later. It is published by a hard link, which fails with
    FileExistsError when a file is there (or appears meanwhile), or, with
    `overwrite`, by an atomic replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".migrate-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as out:
            json.dump(data, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        status = os.stat(temporary)
        identity = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
        return identity
    finally:
        try:
            os.unlink(temporary)  # its other name, after a link; already gone after a replace
        except OSError:
            pass


def _ours(target, identity):
    status = target.stat()
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns) == identity


def _undo(target, identity, previous):
    """Put `target` back as it was before a migration published `identity` there; True when done. A file that
    is not exactly the one published (replaced, edited, or someone else's) is left alone."""
    try:
        if not target.exists():
            return previous is None
        if not _ours(target, identity):
            return False
        if previous is None:
            target.unlink()
        else:
            descriptor, temporary = tempfile.mkstemp(prefix=".migrate-", dir=target.parent)
            with os.fdopen(descriptor, "wb") as out:
                out.write(previous)
            os.replace(temporary, target)
        return True
    except OSError:
        return False


def submodules():
    """{plugin name: checkout path} of the mv2 checkout's plugin submodules (.gitmodules), initialized or not."""
    import subprocess
    if loader.CHECKOUT is None:
        return {}
    repository = loader.CHECKOUT.parent
    if not (repository / ".gitmodules").is_file():
        return {}
    result = subprocess.run(["git", "-C", str(repository), "config", "-f", ".gitmodules", "--get-regexp", r"\.path$"],
                            capture_output=True, text=True, timeout=30)
    found = {}
    for line in result.stdout.splitlines():
        _, _, path = line.partition(" ")
        parts = Path(path.strip()).parts
        if len(parts) == 2 and parts[0] == "plugins":
            found[parts[1]] = repository / path.strip()
    return found


def _submodule_url(repository, relative):
    """The URL .gitmodules gives the submodule at `relative`, or None."""
    import subprocess
    result = subprocess.run(["git", "-C", str(repository), "config", "-f", ".gitmodules", "-z", "--get-regexp",
                             r"^submodule\..*\.path$"], capture_output=True, text=True, timeout=30)
    for record in result.stdout.split("\0"):
        key, _, value = record.partition("\n")
        if value == relative and key.endswith(".path"):
            url = subprocess.run(["git", "-C", str(repository), "config", "-f", ".gitmodules", "--get",
                                  key[:-len(".path")] + ".url"], capture_output=True, text=True, timeout=30)
            return url.stdout.strip() or None
    return None


def _local_url(url):
    """Whether a submodule URL is a path on this machine rather than a remote."""
    return bool(url) and (url.startswith(("/", "./", "../", "~", "file:")))


def install(name):
    """Initialize the checkout's submodule for plugin `name` (git submodule update --init); never prompts."""
    import os
    import subprocess
    if loader.CHECKOUT is None:
        raise PluginError("This mv2 runs from an installed package, not a checkout: install plugins with pip")
    known = submodules()
    if name not in known:
        raise PluginError(f"No plugin named {name} among this checkout's submodules (.gitmodules)")
    repository = loader.CHECKOUT.parent
    relative = str(known[name].relative_to(repository))
    # A submodule whose URL is a path on this machine (a plugin repository not hosted yet) needs Git's file
    # protocol, which current Git refuses for submodules by default: allowed for this one command only.
    allow = ["-c", "protocol.file.allow=always"] if _local_url(_submodule_url(repository, relative)) else []
    result = subprocess.run(["git", "-C", str(repository), *allow, "submodule", "update", "--init", "--", relative],
                            capture_output=True, text=True, timeout=600, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode != 0:
        raise PluginError(f"git could not fetch {name}: {(result.stderr or result.stdout).strip()}")
    return {"name": name, "path": str(known[name]), "installed": (known[name] / "plugin.json").is_file()}


def _provides(found):
    """What a plugin adds: from its manifest, and once it is loaded in this process, from what it registered."""
    manifest = found.manifest
    if manifest is None:
        return None
    known = {"commands": sorted(manifest.commands), "statusline": [item.name for item in manifest.statusline],
             "events": list(manifest.events)}
    if found.loaded is None:
        return {**known, "columns": None, "actions": None, "panels": None, "screens": None, "services": None}
    registered = found.loaded.registrations
    return {**known, "columns": sorted(registered.columns), "actions": sorted(registered.actions),
            "panels": sorted({*registered.panels, *registered.data_panels}),
            "screens": sorted(registered.screens),
            "services": len(registered.services)}


def panel_context(loaded, root, widget, rows, plugins_on):
    """The api.PanelContext a data panel of `loaded` renders with in the scope at `root`."""
    from . import api
    return api._panel_context(loaded, root, widget, rows, plugins_on)


def panel_blocks(result, title):
    """A data panel's render result as the dashboard's widget objects; api.Error names what cannot be drawn."""
    from . import api
    return api._blocks(result, title)


def dashboard_actions(root):
    """[(Loaded, name, function)] of the command palette actions plugins on here register, in plugin-name order."""
    return [(loaded, name, function) for loaded in loader.active(root)
            for name, function in sorted(loaded.registrations.actions.items())]


def dashboard_panels(root):
    """[(Loaded, type, spec)] of the panels plugins on here register, in plugin-name order.

    spec is a loader.DataPanel (render, title, auto) or, for the class form, the "module:Class" string."""
    found = []
    for loaded in loader.active(root):
        for kind, spec in sorted(loaded.registrations.data_panels.items()):
            found.append((loaded, kind, spec))
        for kind, reference in sorted(loaded.registrations.panels.items()):
            found.append((loaded, kind, reference))
    return found


def set_enabled(root, name, on):
    """Turn a plugin on or off in this universe. Turning it on installs its default settings when there are none.

    Returns {"name", "on", "installed": [paths]}. A plugin must be found to be
    turned on, and a universe plugin must be trusted first.
    """
    root = Path(root)
    installed = []
    if on:
        found = entry(root, name)
        if found.state in ("invalid", "untrusted", "changed", "incompatible"):
            raise PluginError(found.problem)
        if found.manifest is not None and settings_files.install_defaults(root, found.manifest):
            installed.append(str(settings_files.path(root, name)))
    config.set_enabled(root, name, bool(on))
    return {"name": name, "on": bool(on), "installed": installed}


def settings_of(root, name):
    """(values, problem, path) of a plugin's settings in this universe."""
    found = entry(root, name)
    if found.manifest is None:
        raise PluginError(found.problem or f"{name} cannot be read")
    current = loader.settings(root, found)
    return current.values, current.problem, current.path


def edit_settings(root, name, changes):
    """Change only the named settings of a plugin here; returns its new values.

    Keys are dotted paths or tuples; settings.DELETE removes one. Nothing is
    written unless the whole result passes the plugin's schema and own check.
    """
    found = entry(root, name)
    if found.manifest is None:
        raise PluginError(found.problem or f"{name} cannot be read")
    check = found.loaded.registrations.settings_check if found.loaded else None
    return settings_files.edit(root, found.manifest, changes, check).values


# The dashboard's extension points. Nothing here imports Textual; screens and
# panels are imported by the dashboard through Loaded.resolve.

def on_here(root):
    """Names of the plugins on and usable in this universe."""
    return frozenset(found.name for found in _usable(root))


def dashboard_columns(root):
    """{"plugin:key": (Loaded, loader.Column)} of plugins on here, and each short key that only one of them uses."""
    result, short = {}, {}
    for loaded in loader.active(root):
        for key, column in loaded.registrations.columns.items():
            result[f"{loaded.name}:{key}"] = (loaded, column)
            short.setdefault(key, []).append((loaded, column))
    result.update({key: owners[0] for key, owners in short.items() if len(owners) == 1})
    return result


def dashboard_panel(root, kind):
    """(Loaded, "module:Class") for a view widget type of a plugin on here, or a PluginError saying what it needs."""
    plugin, _, name = kind.rpartition(":")
    owners = []
    for found in entries(root):
        if plugin and found.name != plugin:
            continue
        if found.usable:
            try:
                loaded = loader.load(found)
            except PluginError as error:
                if plugin:
                    raise PluginError(f"widget type {kind} needs plugin {plugin}, which failed to load: {error}") from None
                continue
            if name in loaded.registrations.panels:
                owners.append((loaded, loaded.registrations.panels[name]))
        elif plugin:
            raise PluginError(f"widget type {kind} needs plugin {plugin}"
                              + (f": {found.problem}" if found.problem else ", which is off here"))
    if len(owners) == 1:
        return owners[0]
    if plugin and not owners and not any(found.name == plugin for found in entries(root)):
        raise PluginError(f"widget type {kind} needs plugin {plugin}, which is not installed")
    raise PluginError(f"unknown widget type: {kind}" if not owners else
                      f"widget type {kind} is offered by {' and '.join(o.name for o, _ in owners)}; "
                      f"name it as PLUGIN:{kind}")


def theme_colors(root):
    """(plugin name, its palette) for the theme plugins.json names here, or None."""
    chosen = config.load(root).theme
    if chosen is None:
        return None
    for loaded in loader.active(root):
        if loaded.name == chosen:
            return loaded, dict(loaded.registrations.theme or {})
    return None


def inspector_facts(root):
    """[(Loaded, fact)] of plugins on here, in plugin-name order; fact(row) gives a line or None."""
    return [(loaded, fact) for loaded in loader.active(root) for fact in loaded.registrations.inspector]
