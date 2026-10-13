"""mv2 plugins: what core asks of them, one function per extension point.

Plugins themselves import only `multiverse.plugins.api`. Core calls the
functions here, always with the universe the question is about, so a plugin
counts only where that universe's plugins.json turns it on. Every call into
plugin code is guarded: a failure is recorded against the plugin and the
answer goes on without it. See docs/plugins.md.
"""
from dataclasses import asdict
import json
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
    pending = None
    for found in entries(root):
        legacy = (found.manifest.settings.legacy if found.manifest is not None and found.manifest.settings is not None
                  else None)
        if legacy and (Path(root) / legacy).is_file() and (not found.on or found.state == "safe-mode"):
            # Its pre-plugin file says to use it, but it is not used: say so rather than run quietly without it.
            why = (f"{loader.SAFE_MODE}=off loads no plugins" if found.state == "safe-mode"
                   else f"{config.path(root).name} turns it off")
            found_problems.append(f"{legacy} is still here, but the {found.name} plugin is not used: {why}. "
                                  f"Turn it on with mv2 plugins enable {found.name}, or move {legacy} away.")
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
    for loaded in loader.active(root):
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


def awaiting_migration(root):
    """{"plugin-settings/NAME.json": legacy file name} for each plugin whose pre-plugin settings file is still
    here and that has no settings file yet. Manifests only, no plugin code: for `mv2 refresh`, which must not
    add a template's default settings file in front of a universe's real, unmigrated settings."""
    found = {}
    for entry_ in entries(root):
        manifest = entry_.manifest
        if manifest is None or manifest.settings is None or not manifest.settings.legacy:
            continue
        target = settings_files.path(root, entry_.name)
        if (Path(root) / manifest.settings.legacy).is_file() and not target.exists():
            found[target.relative_to(root).as_posix()] = manifest.settings.legacy
    return found


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


def migrate(root, item):
    """Apply one planned migration: the new settings are checked, then written; the old file goes to trash."""
    from ..store import write
    from ..universe import trash
    found = entry(root, item["plugin"])
    loaded = loader.load(found, off=True)
    function = loaded.registrations.settings_migrate
    if function is None:
        raise PluginError(f"{found.name} offers no migration")
    source = Path(item["from"])
    old = json.loads(source.read_text())
    try:
        answer = function(old, "legacy" if item["kind"] == "legacy" else item["version"])
    except Exception as error:
        raise PluginError(f"{found.name}'s migration failed: {loader.record(loaded, 'migrate', error, root)}") from error
    data, on = answer if isinstance(answer, tuple) and len(answer) == 2 else (answer, None)
    if not isinstance(data, dict) or (on is not None and not isinstance(on, bool)):
        raise PluginError(f"{found.name}'s migration returned something other than settings")
    settings_files._accept(found.manifest, data, loaded.registrations.settings_check, item["to"], root)
    backup = trash.entry(Path(root), "plugins-migrate") / source.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(source.read_bytes())
    write(Path(item["to"]), data)
    if item["kind"] == "legacy":
        source.unlink()
        if on is not None and found.name not in config.load(root).enabled:
            config.set_enabled(root, found.name, on)
    return {**item, "on": on, "backup": str(backup)}


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
