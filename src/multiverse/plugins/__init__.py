"""mv2 plugins: what core asks of them, one function per extension point.

Plugins themselves import only `multiverse.plugins.api`. Core calls the
functions here, always with the universe the question is about, so a plugin
counts only where that universe's plugins.json turns it on. Every call into
plugin code is guarded: a failure is recorded against the plugin and the
answer goes on without it. See docs/develop/plugins.md.
"""
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
import sys
import tempfile
from pathlib import Path

from . import access as levels, config, loader, manifest as manifests, settings as settings_files
from .loader import PluginError, entries, entry

CATALOG_KINDS = ("skills", "commands", "workflows")


def _usable(root):
    return [] if loader.safe_mode() else [found for found in entries(root) if found.usable]


def _commanding(root):
    """Plugins whose commands are offered: usable ones, and those whose settings cannot be used here (their
    command is where a person sees why, and often how to fix it)."""
    return [] if loader.safe_mode() else [found for found in entries(root)
                                          if found.usable or (found.on and found.state == "unusable")]


def commands(root, reserved=()):
    """{command: (plugin, help)} that plugins on here declare, from their manifests alone.

    A name core uses, or one two plugins declare, is left out; `problems` reports it.
    """
    claimed = {}
    for found in _commanding(root):
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
    try:
        loaded = loader.load(entry(root, owner[0]))
    except PluginError as error:
        raise PluginError(_about(owner[0], error)) from error
    now = entry(root, owner[0])
    if now.state == "unusable" and (root, owner[0]) not in _warned:
        _warned.add((root, owner[0]))  # once a process: one mv2 command looks its command up more than once
        print(f"mv2: {_about(owner[0], now.problem)}", file=sys.stderr)  # it still runs: it may show or fix them
    found = loaded.registrations.commands.get(name)
    if found is None:
        raise PluginError(f"plugin {loaded.name}: it declares mv2 {name} but did not register it")
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


_warned = set()  # (universe, plugin) whose unusable settings this process has already said


def _about(name, problem):
    """`plugin NAME: …` for a problem of plugin NAME, as its command reports it ("NAME failed to load: X" becomes
    "plugin NAME: failed to load: X")."""
    text = str(problem)
    for prefix in (f"{name}: ", f"{name} "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return f"plugin {name}: {text}"


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
            # From the directory it runs from: a trusted universe plugin's copy, never the live folder.
            for path in sorted((Path(found.manifest.directory) / kind).glob("*/entry.json")):
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


def problems(root, reserved=(), wait=None):
    """Why plugins here are not working, as sentences: for `mv2 doctor` and the dashboard."""
    found_problems = []
    usable = []
    settings_problem = config.load(root).problem
    if settings_problem:
        found_problems.append(f"plugins.json cannot be used, so every plugin is off here until it is repaired: "
                              f"{settings_problem}")
    waiting_uninstalled = dict(not_installed(root))
    found_problems.extend(not_installed_message(name, legacy) for name, legacy in waiting_uninstalled.items())
    for name, failure in sorted(_failed_conversions(root).items()):
        legacy = Path(root) / str(failure.get("file", ""))
        if failure.get("file") and legacy.is_file() and _fingerprint(legacy) == failure.get("fingerprint"):
            found_problems.append(f"{name}: {failure['file']} could not be converted ({failure.get('reason')}); "
                                  f"it is unchanged: fix it and run mv2 plugins migrate --apply")
    from . import host
    for found in entries(root):
        if found.manifest is not None and found.manifest.events:
            found_problems.extend(host.gave_up(root, found.name))  # a handler given up on, in the last day
        for source, directory, state in found.set_aside:
            if state == "changed":  # a fork the person trusted, edited since: the other copy runs, and they hear so
                found_problems.append(f"{found.name}: this universe's copy changed since it was trusted, so the "
                                      f"{found.source} {found.name} runs instead; review it, then run "
                                      f"mv2 plugins trust {found.name}")
    pending = None
    for found in entries(root):
        legacy = (found.manifest.settings.legacy if found.manifest is not None and found.manifest.settings is not None
                  else None)
        if (legacy and (Path(root) / legacy).is_file() and not settings_files.path(root, found.name).exists()
                and found.name not in _failed_conversions(root)):
            # Waiting for its first-use conversion: until then the plugin has neither its switch nor its settings.
            then = (f"nothing is converted while {loader.SAFE_MODE}=off" if loader.safe_mode()
                    else f"review the plugin and run mv2 plugins trust {found.name} first; it is converted the next "
                         f"time mv2 runs here after that" if found.state in ("untrusted", "changed")
                    else "it is converted the next time mv2 runs here, or now with mv2 plugins migrate --apply")
            found_problems.append(f"{found.name}: {legacy} is still here and not converted yet: {then}.")
            continue
        if found.on and found.state == "missing" and (found.name in waiting_uninstalled or found.name in KNOWN):
            continue  # said above with what to do about its old file, or, with none waiting, a note (notes())
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
            if below_minimum(root, found):
                found_problems.append(below_minimum(root, found))
    claimed = {}
    for found in usable:
        for name in found.manifest.commands:
            claimed.setdefault(name, []).append(found.name)
    for name, owners in sorted(claimed.items()):
        if name in reserved:
            found_problems.append(f"{', '.join(owners)} declares mv2 {name}, which mv2 itself uses; it is ignored")
        elif len(owners) > 1:
            found_problems.append(f"{' and '.join(owners)} both declare mv2 {name}; neither is used")
    from . import host
    stopped = host.left_stopped(root)
    for found in usable:
        if found.name in stopped:
            found_problems.append(_left_stopped(found.name, stopped[found.name]))
    for found in usable:  # `wait`: how long a plugin still loading is waited for (the dashboard: a short wait)
        try:
            loader.load(found, wait=wait)
        except loader.StillLoading:
            found_problems.append(f"{found.name} is still loading")
        except PluginError as error:
            found_problems.append(str(error))
    # Loading runs each plugin's own settings check: settings it refuses make it unusable here, said once.
    now = {found.name: found for found in entries(root)}
    for found in usable:
        if found.name in now and now[found.name].state == "unusable":
            found_problems.append(now[found.name].problem)
    replacing = {}  # what register(mv) asked for and could not have, and core columns two plugins stand in for
    for found in usable:
        loaded = now[found.name].loaded if found.name in now else None
        if loaded is not None:
            found_problems.extend(loaded.registrations.problems)
            for column in loaded.registrations.columns.values():
                if column.replaces and found.name not in replacing.setdefault(column.replaces, []):
                    replacing[column.replaces].append(found.name)
    for key, owners in sorted(replacing.items()):
        if len(owners) > 1:
            found_problems.append(f"{' and '.join(owners)} both replace the {key} column, so neither does")
    try:
        from .harness import item_conflicts
        found_problems.extend(item_conflicts(root))
    except Exception:
        pass
    return found_problems


def notes(root):
    """What doctor says without counting it a problem: a KNOWN optional plugin that is on here but not installed,
    with no old settings file waiting (then nothing is lost: it only does not run). A template's plugins.json
    can turn compaction on, and a checkout without access to its repository cannot install it."""
    waiting = dict(not_installed(root))
    unknown = config.load(root).unknown
    newer = ([f"plugins.json has fields this mv2 does not know: {', '.join(unknown)}; update mv2 to use them"]
             if unknown else [])
    ignored = [f"{name}: {legacy} is here but ignored: its settings were converted to "
               f"{settings_files.path(root, name).relative_to(root).as_posix()} already; remove {legacy}"
               for name, legacy in reappeared(root)]
    return newer + ignored + [f"{found.name} is on here but not installed (an optional plugin: mv2 plugins install "
                    f"{found.name}, which needs access to its repository)"
                    for found in entries(root)
                    if found.on and found.state == "missing" and found.name in KNOWN and found.name not in waiting]


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


def listing(root, wait=loader.REFRESH_WAIT):
    """Every plugin this universe sees, for `mv2 plugins` and the dashboard's Plugins screen.

    [{name, on, state, source, version, description, problem, errors, permissions,
      commands, settings: {path, source, problem, schema} or None, shadowed}]

    It never waits out a slow register(): a plugin whose load is still running is `loading`. A plugin that is on
    and has settings is loaded first, waiting at most `wait` (None: until it has loaded), so what its own settings
    check refuses reads `unusable` here as it does in doctor. A plugin that is off is never imported here; its
    own settings screen or fields (settings `own_view`) come from settings_view(), on request."""
    from . import host
    current = entries(root)
    checks = [found for found in current if found.manifest is not None and found.manifest.settings is not None
              and found.on and found.state in ("on", "unusable") and found.loaded is None]
    for found in checks:
        try:
            loader.load(found, wait=wait)
        except PluginError:
            pass  # its entry says why
    if checks:
        current = entries(root)
    recent = loader.errors()
    stopped = host.left_stopped(root)
    result = []
    for found in current:
        manifest = found.manifest
        state = "loading" if found.usable and found.loaded is None and loader.loading(found) else found.state
        problem, waiting = found.problem, _waiting_old_file(root, found)
        if waiting and found.on and state in ("on", "loaded", "loading"):
            # The plugin's real settings wait in its old file: it is off here until that is converted (compaction
            # fails closed), and its defaults are never shown as its settings.
            state, problem = "unusable", f"{found.name}: off until {waiting} is converted: run mv2 plugins migrate --apply"
        elif problem is None and state in ("on", "loaded", "loading"):
            problem = below_minimum(root, found)  # on, but it can't work: said where the listing is read
        item = {"name": found.name, "on": found.on, "state": state, "source": found.source,
                "directory": str(found.directory) if found.directory else None,
                "version": manifest.version if manifest else None,
                "description": manifest.description if manifest else "",
                "default": manifest.default if manifest and found.source == "bundled" else "off",
                "problem": problem, "errors": recent.get(found.name, []) + (
                    host.gave_up(root, found.name) if manifest is not None and manifest.events else []) + (
                    [_left_stopped(found.name, stopped[found.name])] if found.name in stopped else []),
                "permissions": manifest.permission_texts() if manifest else [],
                "commands": dict(manifest.commands) if manifest else {},
                "provides": _provides(found),
                "access": _access_here(root, found),
                "settings": None,
                "shadowed": [{"source": source, "directory": str(directory)} for source, directory in found.shadowed],
                # A trusted universe copy that takes the place of another source's plugin says so: a cloned
                # repository's plugin never replaces a built-in one silently.
                "overrides": [{"source": source, "directory": str(directory)} for source, directory in found.shadowed
                              if found.source == "universe" and source != "universe"]}
        if manifest is not None and manifest.settings is not None:
            current = loader.settings(root, found)
            try:
                schema, schema_problem = settings_files.schema_of(manifest), None
            except ValueError as error:
                schema, schema_problem = None, str(error)
            registered = found.loaded.registrations if isinstance(found.loaded, loader.Loaded) else None
            item["settings"] = {"path": str(current.path), "source": current.source,
                                "problem": (_waiting_problem(waiting) if waiting else None) or current.problem
                                or schema_problem, "schema": schema,  # the settings file's problem, nothing else
                                "notes": [] if waiting else list(current.notes),  # its own check's, 1.15
                                "own_view": "dashboard.settings" in manifest.extends,
                                "values": {} if waiting else current.values,
                                "screen": registered.settings_screen if registered else None,
                                "fields": ([asdict(item) for item in registered.settings_fields]
                                           if registered and registered.settings_fields else None)}
        result.append(item)
    return result


def _left_stopped(name, why):
    return f"{name}: the plugin host left it stopped: {why}"


# A plugin whose settings may be converted: listed, trusted and compatible (on or off).
MIGRATABLE = ("off", "on", "loaded", "unusable")
MIGRATE_LOCK = ".migrate.lock"
MIGRATE_FAILED = "migrate-failed.json"
NOTIFIED = "not-installed-notified.json"  # which old files' "not installed" notice was said, kept apart from failures


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


# Plugins mv2 knows by name although they live outside it (optional checkout submodules): their pre-plugin
# settings file, and the harness families they took part in. With these, an old file is never silently ignored
# while its plugin is not installed (a checkout without the submodule, or without access to it).
def _known():
    """{name: {"families": (...), "legacy": FILE}} from known.json, shipped as package data: the optional plugins
    mv2's checkout ships as submodules that core must know of while one is missing here (the harness families it
    extends, its pre-plugin settings file), so mv2 refresh never puts a template's settings in front of that file
    and a launch says the plugin isn't installed, in an installed mv2 too. Data, so core code names no plugin."""
    try:
        data = json.loads(Path(__file__).with_name("known.json").read_text())
        listed = data["plugins"] if data.get("schema") == 1 else {}
        return {name: {"families": tuple(entry["families"]), "legacy": entry["legacy"]}
                for name, entry in listed.items()
                if isinstance(entry.get("legacy"), str) and all(isinstance(item, str) for item in entry["families"])}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


KNOWN = _known()


def waiting_settings(root):
    """{"plugin-settings/NAME.json": old file name} of every plugin whose pre-plugin settings file waits here to
    be converted, whatever the plugin's state (on, off, untrusted, missing, or a known plugin not installed at
    all). Manifests and KNOWN only, no plugin code: for `mv2 refresh`, which must never put a template's
    settings in front of a universe's real ones, and for settings edits."""
    root, found, names = Path(root), {}, set()
    for entry_ in entries(root):
        names.add(entry_.name)
        legacy = (entry_.manifest.settings.legacy if entry_.manifest is not None and entry_.manifest.settings
                  else KNOWN.get(entry_.name, {}).get("legacy"))
        if legacy and (root / legacy).is_file() and not settings_files.path(root, entry_.name).exists():
            found[settings_files.path(root, entry_.name).relative_to(root).as_posix()] = legacy
    for name, known in KNOWN.items():
        if name not in names and (root / known["legacy"]).is_file() \
                and not settings_files.path(root, name).exists():
            found[settings_files.path(root, name).relative_to(root).as_posix()] = known["legacy"]
    return found


awaiting_migration = waiting_settings  # the name refresh has used


def _old_file_here(root):
    """Whether any pre-plugin settings file a plugin could convert is here: KNOWN's and every manifest's (any
    copy, any state), from manifests alone, so the check before every command makes no trust check."""
    names = {known["legacy"] for known in KNOWN.values()}
    names.update(found.manifest.settings.legacy for found in loader.declared(root)
                 if found.manifest.settings is not None and found.manifest.settings.legacy)
    return any((Path(root) / name).is_file() for name in names)


def not_installed(root):
    """[(name, old file)] of KNOWN plugins whose pre-plugin settings file is here while no copy of the plugin is
    found: their settings are not used until it is installed."""
    if root is None:
        return []
    found = {entry_.name for entry_ in entries(root) if entry_.manifest is not None}
    return [(name, known["legacy"]) for name, known in KNOWN.items()
            if name not in found and (Path(root) / known["legacy"]).is_file()
            and not settings_files.path(root, name).exists()]


def not_installed_message(name, legacy):
    return (f"{legacy} is here but the {name} plugin isn't installed: run mv2 plugins install {name}, "
            f"or move {legacy} away to stop using its settings")


def _fingerprint(path):
    status = path.stat()
    return [status.st_mtime_ns, status.st_size]


def _failed_conversions(root):
    """{plugin: {"file", "fingerprint", "reason"}} of conversions that failed and were not retried since.

    A "not installed" notice is no failed conversion: an older mv2 kept it here, and such a record is ignored,
    so installing the plugin later converts its file."""
    try:
        value = json.loads((Path(root) / "state" / "plugins" / MIGRATE_FAILED).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {name: record for name, record in value.items()
            if isinstance(record, dict) and not record.get("not_installed")}


def _notified(root):
    try:
        value = json.loads((Path(root) / "state" / "plugins" / NOTIFIED).read_text())
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
                if not _old_file_here(scope):
                    continue  # the case for every command once a universe is converted: a few stats, no trust check
                lines.extend(_say_not_installed(scope, "" if scope == root else _where(root, scope)))
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


def _say_not_installed(scope, where):
    """A KNOWN plugin's old file waits while the plugin is not installed: said once per version of the file."""
    from ..store import write
    missing = not_installed(scope)
    if not missing:
        return []
    state = Path(scope) / "state" / "plugins"
    lines = []
    with lock_of(state / MIGRATE_LOCK):
        notified = _notified(scope)
        for name, legacy in missing:
            fingerprint = _fingerprint(Path(scope) / legacy)
            if notified.get(name, {}).get("fingerprint") == fingerprint:
                continue
            notified[name] = {"file": legacy, "fingerprint": fingerprint}
            lines.append(f"mv2: {not_installed_message(name, legacy)}" + (f" ({where.strip()})" if where else ""))
        if lines:
            try:
                write(state / NOTIFIED, notified)
            except OSError:
                pass
    return lines


def lock_of(path):
    from ..store import lock
    return lock(path)


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
    # A legacy file's own on or off always wins: a template's plugins.json may name the plugin before its old
    # file is converted, and the old file is what the person chose.
    switch = legacy and on is not None and switches.enabled.get(found.name) != on
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
            if on:
                anchor_events(root, found.name)
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
        from ..store import now, write as write_json
        write_json(backup.parent / trash.RECORD, {"kind": "plugins-migrate", "name": item["plugin"],
                                                  "origin": str(root), "files": [source.name], "removed_at": now()})
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
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
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
                             r"^submodule\..*\.path$"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    for record in result.stdout.split("\0"):
        key, _, value = record.partition("\n")
        if value == relative and key.endswith(".path"):
            url = subprocess.run(["git", "-C", str(repository), "config", "-f", ".gitmodules", "--get",
                                  key[:-len(".path")] + ".url"], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=30)
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
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode != 0:
        raise PluginError(f"git could not fetch {name}: {(result.stderr or result.stdout).strip()}")
    return {"name": name, "path": str(known[name]), "installed": (known[name] / "plugin.json").is_file()}


def _access_here(root, found):
    """listing()'s access: what the plugin asks for here and what it has."""
    wanted = found.manifest.access if found.manifest is not None else manifests.Access()
    settings = config.load(root)
    level = loader.access_level(root, found)
    # What that level lets it use of what it asked for, and what it holds back, each with the level it needs:
    # None while it is off here, where it has no level.
    allowed, held_back = found.manifest.permissions_at(level) if found.manifest is not None and level else (None, None)
    return {"request": wanted.request, "minimum": wanted.minimum, "why": wanted.why,
            "granted": settings.access.get(found.name), "level": level,
            "allowed": allowed, "held_back": held_back,
            # What holds it below its request, strongest first: MV2_EFFECTS, the universe's access_ceiling, its grant.
            "limited_by": [{"by": what, "level": cap} for what, cap in loader.limits(root, found)],
            # How a person gives it what it asks for, where it has less: the change that would actually do it.
            "raise": (loader.raise_hint(root, found.name, wanted.request)
                      if level is not None and levels.rank(level) < levels.rank(wanted.request) else None),
            "ceiling": levels.lowest(*(level for level in (settings.ceiling, levels.process_ceiling())
                                       if level is not None)) if (settings.ceiling or levels.process_ceiling())
            else None}


def below_minimum(root, found):
    """The problem of a plugin that is on here but held below the least access it can work with, or None."""
    if found is None or not found.on or found.manifest is None:
        return None
    level, minimum = loader.access_level(root, found), found.manifest.access.minimum
    if level is None or levels.rank(level) >= levels.rank(minimum):
        return None
    return (f"{found.name} is on but held back below its minimum here: it has {level} access and needs at least "
            f"{minimum}; {loader.raise_hint(root, found.name, minimum)}")


def entry_access(root, name):
    """listing()'s access for one plugin, without listing the others."""
    return _access_here(Path(root), entry(Path(root), name))


def from_agent():
    """Whether this process runs for an agent: its launches and their children carry MV2_AGENT_ID or MV2_AGENT_DIR.
    A guard against mistakes, not against an agent that unsets them."""
    return bool(os.environ.get("MV2_AGENT_ID") or os.environ.get("MV2_AGENT_DIR"))


def _grant_for(root, found, grant):
    """The grant to record when `found` is turned on here: `grant` checked against what the plugin asks for, or,
    from an agent, at most its minimum. None records nothing (it has what it requests)."""
    if found.manifest is None:
        return grant
    wanted = found.manifest.access
    if grant is not None:
        levels.check(grant, "grant")
        if levels.rank(grant) > levels.rank(wanted.request):
            raise PluginError(f"{found.name} asks for at most {wanted.request} access")
        if levels.rank(grant) < levels.rank(wanted.minimum):
            raise PluginError(f"{found.name} needs at least {wanted.minimum} access"
                              + (f": {wanted.why}" if wanted.why else ""))
    if from_agent():
        if grant is not None and levels.rank(grant) > levels.rank(wanted.minimum):
            raise PluginError(f"An agent can turn {found.name} on with at most {wanted.minimum} access. "
                              f"For more, a person runs: mv2 plugins grant {found.name} {grant}")
        if grant is None and wanted.minimum != wanted.request:
            return wanted.minimum
    return grant


def set_grant(root, name, level):
    """Record the access level `name` has here: between its minimum and its request. An agent may lower a
    grant, never raise it. Returns {"name", "granted", "level"} (level: in effect now, None while it is off)."""
    root = Path(root)
    found = entry(root, name)
    levels.check(level, "grant")
    wanted = found.manifest.access if found.manifest is not None else manifests.Access()
    if levels.rank(level) > levels.rank(wanted.request):
        raise PluginError(f"{name} asks for at most {wanted.request} access")
    if levels.rank(level) < levels.rank(wanted.minimum):
        raise PluginError(f"{name} needs at least {wanted.minimum} access" + (f": {wanted.why}" if wanted.why else ""))
    try:
        # From an agent, compared with the grant recorded now and written in one locked step: a person who lowers
        # it meanwhile is never overwritten.
        config.set_grant(root, name, level, no_raise_from=wanted.request if from_agent() else None)
    except config.WouldRaise as error:
        raise PluginError(f"An agent can't raise a plugin's access ({error}). A person runs: "
                          f"mv2 plugins grant {name} {level}") from None
    return {"name": name, "granted": level, "level": loader.access_level(root, entry(root, name))}


def _provides(found):
    """What a plugin adds: from its manifest, and once it is loaded in this process, from what it registered."""
    manifest = found.manifest
    if manifest is None:
        return None
    known = {"commands": sorted(manifest.commands), "statusline": [item.name for item in manifest.statusline],
             "events": list(manifest.events)}
    if found.loaded is None:
        return {**known, "columns": None, "replaces": None, "actions": None, "panels": None, "screens": None,
                "services": None}
    registered = found.loaded.registrations
    return {**known, "columns": sorted(registered.columns),
            "replaces": sorted({column.replaces for column in registered.columns.values() if column.replaces}),
            "actions": sorted(registered.actions),
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
    for loaded in loader.active(root, wait=loader.REFRESH_WAIT):
        for kind, spec in sorted(loaded.registrations.data_panels.items()):
            found.append((loaded, kind, spec))
        for kind, reference in sorted(loaded.registrations.panels.items()):
            found.append((loaded, kind, reference))
    return found


def universe_copy(root, name):
    """The directory of this universe's own copy of plugin NAME (in its plugins/), or None: found whatever another
    source also provides, which entries() would choose while that copy is not trusted."""
    for directory in loader._plugin_directories(Path(root) / "plugins"):
        try:
            if manifests.load(directory).name == name:
                return directory.resolve()
        except manifests.ManifestError:
            if directory.name == name:
                return directory.resolve()
    return None


def set_enabled(root, name, on, grant=None):
    """Turn a plugin on or off in this universe. Turning it on installs its default settings when there are none,
    and records `grant` (an access level between its minimum and its request) when given.

    Returns {"name", "on", "installed": [paths], "granted"}. A plugin must be found to be
    turned on, and a universe plugin must be trusted first. From an agent, it is
    turned on with at most its minimum access (recorded when that is below its
    request). Turning a plugin off leaves its recorded grant alone.
    """
    root = Path(root)
    installed = []
    was_on = entry(root, name).on
    if on:
        found = entry(root, name)
        if found.state in ("invalid", "untrusted", "changed", "incompatible"):
            raise PluginError(found.problem)
        grant = _grant_for(root, found, grant)
        if found.manifest is not None and settings_files.install_defaults(root, found.manifest):
            installed.append(str(settings_files.path(root, name)))
    elif grant is not None:
        raise PluginError("A grant is recorded when a plugin is turned on, or with mv2 plugins grant")
    try:
        config.set_enabled(root, name, bool(on), grant=grant if on else None,
                           no_raise_from=found.manifest.access.request if on and from_agent() and found.manifest
                           else None)
    except config.WouldRaise as error:
        raise PluginError(f"An agent can't raise a plugin's access ({error}). A person runs: "
                          f"mv2 plugins grant {name} {grant}") from None
    if on and not was_on:
        anchor_events(root, name, again=True)  # what happened while it was off is not delivered later
    return {"name": name, "on": bool(on), "installed": installed, "granted": grant if on else None}


def anchor_events(root, name, again=False):
    """When a plugin with event handlers is turned on, its cursor starts at the journal's end now, so the events
    between this and its host worker's first run are delivered then, not skipped. `again` moves a cursor left from
    an earlier time it was on, so turning it on again does not replay what happened while it was off. Never fails
    the caller."""
    try:
        found = entry(root, name)
        if found.manifest is None or not found.manifest.events:
            return
        from . import events, host
        if host._cursor_path(root, name).exists() and not again:
            return
        segments = events.segments(root)
        host._write_cursor(root, name, events._last_sequence(segments[-1][1]) if segments else 0,
                           events.journal_id(root))
    except Exception:
        pass


def settings_view(root, name, wait=loader.REFRESH_WAIT):
    """A plugin's own settings view, for the Plugins screen's Enter: {"screen": "module:Class" or None,
    "fields": [field dicts] or None, "problem": why it can't be had, or None, "loading": whether that is only
    because its load is still running (try again), not because it failed}.

    It loads the plugin, also where it is off, as `mv2 plugins settings` edits there: its screen or fields are
    where settings are edited and unusable ones fixed. The only caller that imports a plugin for this, and only
    when a person asks; listing() never does. "problem" is the load's (still loading, untrusted, safe mode, a
    failed import), never the settings file's."""
    found = entry(Path(root), name)
    if found.manifest is None or "dashboard.settings" not in found.manifest.extends:
        return {"screen": None, "fields": None, "problem": f"{name} has no settings view of its own", "loading": False}
    loaded = found.loaded if isinstance(found.loaded, loader.Loaded) else None
    if loaded is None:
        try:
            loaded = loader.load(found, off=not found.on, wait=wait)
        except loader.StillLoading:
            return {"screen": None, "fields": None, "problem": f"{name} is still loading", "loading": True}
        except PluginError as error:
            return {"screen": None, "fields": None, "problem": str(error), "loading": False}
    registered = loaded.registrations
    return {"screen": registered.settings_screen,
            "fields": [asdict(item) for item in registered.settings_fields] if registered.settings_fields else None,
            "problem": None, "loading": False}


def settings_checked(root, name, wait=None):
    """Load plugin `name` so its own settings check runs here, also where it is off; None, or why it cannot run.

    The check is the plugin's code: a process that has not loaded the plugin (a fresh `mv2 plugins`) would
    otherwise judge its settings by their schema alone, write what the plugin refuses and call it usable. A
    plugin whose code cannot run here (untrusted, failed, safe mode) has only its schema checked, and is said so."""
    return _own_check(root, name, wait)[1]


def _own_check(root, name, wait=None):
    """(plugin `name`'s settings check or None, why it could not run or None), loading it, also where it is off."""
    found = entry(root, name)
    if found.manifest is None or found.manifest.settings is None:
        return None, None
    loaded = found.loaded if isinstance(found.loaded, loader.Loaded) else None
    if loaded is None:
        try:
            loaded = loader.load(found, off=not found.on, wait=wait)
        except PluginError as error:
            return None, str(error)
    return loaded.registrations.settings_check, None


def reappeared(root):
    """[(name, old file)] of pre-plugin settings files found here although the plugin's settings were converted
    already (plugin-settings/NAME.json is here too): nothing reads them; an old branch may have brought one back."""
    root, found = Path(root), []
    for entry_ in entries(root):
        legacy = (entry_.manifest.settings.legacy if entry_.manifest is not None and entry_.manifest.settings
                  else KNOWN.get(entry_.name, {}).get("legacy"))
        if legacy and (root / legacy).is_file() and settings_files.path(root, entry_.name).is_file():
            found.append((entry_.name, legacy))
    named = {name for name, _ in found} | {entry_.name for entry_ in entries(root)}
    found.extend((name, known["legacy"]) for name, known in KNOWN.items() if name not in named
                 and (root / known["legacy"]).is_file() and settings_files.path(root, name).is_file())
    return found


def settings_of(root, name):
    """(values, problem, path, notes) of a plugin's settings in this universe, judged by its own check too."""
    check, _ = _own_check(root, name)
    found = entry(root, name)
    if found.manifest is None:
        raise PluginError(found.problem or f"{name} cannot be read")
    current = settings_files.read(root, found.manifest, check)
    waiting = _waiting_old_file(root, found)
    if waiting:  # its real settings are in the old file: the defaults never stand in for them
        return {}, _waiting_problem(waiting), current.path, ()
    return current.values, current.problem, current.path, current.notes


def _waiting_old_file(root, found):
    """The pre-plugin settings file of `found` that waits here to be converted, or None."""
    legacy = (found.manifest.settings.legacy if found.manifest is not None and found.manifest.settings is not None
              else None)
    if root is not None and legacy and (Path(root) / legacy).is_file() \
            and not settings_files.path(root, found.name).exists():
        return legacy
    return None


def _waiting_problem(legacy):
    return f"{legacy} is not converted yet: run mv2 plugins migrate --apply"


def edit_settings(root, name, changes):
    """Change only the named settings of a plugin here; returns its new values.

    Keys are dotted paths or tuples; settings.DELETE removes one. Nothing is
    written unless the whole result passes the plugin's schema and own check.
    """
    check, _ = _own_check(root, name)
    found = entry(root, name)
    if found.manifest is None:
        raise PluginError(found.problem or f"{name} cannot be read")
    return settings_files.edit(root, found.manifest, changes, check).values


# The dashboard's extension points. Nothing here imports Textual; screens and
# panels are imported by the dashboard through Loaded.resolve. What the dashboard asks again on every refresh
# (columns, data panels, inspector lines) waits for plugins still loading at most loader.REFRESH_WAIT in all: one
# that takes longer, even a register() that never returns, shows on a later refresh. What it keeps (palette
# actions, a class-form panel, the settings screen) waits; the dashboard reads those off its event loop.

def on_here(root):
    """Names of the plugins on and usable in this universe."""
    return frozenset(found.name for found in _usable(root))


def dashboard_columns(root):
    """{"plugin:key": (Loaded, loader.Column)} of plugins on here, and each short key that only one of them uses."""
    result, short = {}, {}
    for loaded in loader.active(root, wait=loader.REFRESH_WAIT):
        for key, column in loaded.registrations.columns.items():
            result[f"{loaded.name}:{key}"] = (loaded, column)
            short.setdefault(key, []).append((loaded, column))
    result.update({key: owners[0] for key, owners in short.items() if len(owners) == 1})
    return result


def dashboard_panel(root, kind, wait=None):
    """(Loaded, "module:Class") for a view widget type of a plugin on here, or a PluginError saying what it needs.

    `wait` bounds how long a plugin still loading is waited for (loader.load); past it, loader.StillLoading (a
    PluginError) says the widget's plugin is still loading, which is not a failure to remember."""
    plugin, _, name = kind.rpartition(":")
    owners, pending = [], []
    for found in entries(root):
        if plugin and found.name != plugin:
            continue
        if found.usable:
            try:
                loaded = loader.load(found, wait=wait)
            except loader.StillLoading:
                if plugin:
                    raise loader.StillLoading(f"widget type {kind} needs plugin {plugin}, which is still loading") \
                        from None
                pending.append(found.name)
                continue
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
    if not owners and pending:
        raise loader.StillLoading(f"widget type {kind}: plugins still loading may provide it ({', '.join(pending)})")
    if plugin and not owners and not any(found.name == plugin for found in entries(root)):
        raise PluginError(f"widget type {kind} needs plugin {plugin}, which is not installed")
    raise PluginError(f"unknown widget type: {kind}" if not owners else
                      f"widget type {kind} is offered by {' and '.join(o.name for o, _ in owners)}; "
                      f"name it as PLUGIN:{kind}")


def theme_colors(root):
    """(Loaded, its palette) for the theme plugins.json names here, or None.

    Asked before the dashboard's first screen, so it loads that one plugin only, and waits for it at most
    loader.SYNCHRONOUS_BUDGET: a theme plugin that takes longer (or fails) leaves the dashboard its own colors,
    while its load goes on in the background (and the Plugins screen says if it fails)."""
    chosen = config.load(root).theme
    if chosen is None:
        return None
    found = entry(root, chosen)
    if found is None or not found.usable:
        return None
    try:
        loaded = loader.load(found, wait=loader.SYNCHRONOUS_BUDGET)
    except PluginError:
        return None
    return loaded, dict(loaded.registrations.theme or {})


def inspector_facts(root):
    """[(Loaded, fact)] of plugins on here, in plugin-name order; fact(row) gives a line or None."""
    return [(loaded, fact) for loaded in loader.active(root, wait=loader.REFRESH_WAIT) for fact in loaded.registrations.inspector]
