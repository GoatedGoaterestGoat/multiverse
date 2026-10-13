"""List, turn on or off, configure, trust and check plugins."""
import argparse
import json
from pathlib import Path

from .. import plugins, universe
from ..plugins import loader, manifest as manifests, settings as settings_files, trust

ACTIONS = ("list", "info", "enable", "disable", "settings", "trust", "untrust", "check", "host", "events",
           "install", "migrate", "new")
HOST_ACTIONS = ("status", "start", "stop")
NAMED = ("info", "enable", "disable", "settings", "trust", "untrust")


def configure(parser):
    parser.add_argument("plugin_action", nargs="?", default="list", choices=ACTIONS, metavar="ACTION",
                        help="list (default), info, enable, disable, settings, trust, untrust, check, host, events, "
                             "install, migrate or new")
    parser.add_argument("name", nargs="?", help="The plugin (for check: its directory; for host: start, stop or status; "
                                               "for new: the new plugin's name)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", dest="assignments",
                        help="settings: change one setting (VALUE is JSON, or else text); repeatable")
    parser.add_argument("--unset", action="append", default=[], metavar="KEY", dest="removals",
                        help="settings: remove one setting, back to its default; repeatable")
    parser.add_argument("--prune", action="store_true", help="events: delete journal segments past retention now")
    parser.add_argument("--apply", action="store_true", help="migrate: make the changes (otherwise only show them)")
    parser.add_argument("--recursive", action="store_true", help="migrate: this universe and every subverse")
    parser.add_argument("--dir", dest="directory", help="new: where to create it (default: the current directory)")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")


def reserved():
    from ..cli import ALIASES, COMMANDS
    return {*COMMANDS, *(alias for aliases in ALIASES.values() for alias in aliases)}


def _value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _changes(assignments, removals):
    changes = {}
    for item in assignments:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"--set takes KEY=VALUE, not {item!r}")
        changes[key.strip()] = _value(value)
    for key in removals:
        changes[key.strip()] = settings_files.DELETE
    return changes


def run(root=None, *, plugin_action="list", name=None, assignments=(), removals=(), prune=False, apply=False,
        recursive=False, directory=None, json=False):
    if plugin_action not in ACTIONS:
        raise ValueError(f"Unknown plugins action {plugin_action!r}; use one of {', '.join(ACTIONS)}")
    if (apply or recursive) and plugin_action != "migrate":
        raise ValueError("--apply and --recursive belong to mv2 plugins migrate")
    if directory and plugin_action != "new":
        raise ValueError("--dir belongs to mv2 plugins new")
    if plugin_action == "check":
        if not name:
            raise ValueError("mv2 plugins check takes the plugin's directory")
        return check(Path(name))
    if plugin_action == "new":
        if not name:
            raise ValueError("mv2 plugins new takes the new plugin's name")
        return scaffold(name, Path(directory or ".").expanduser())
    if plugin_action == "install":
        if not name:
            raise ValueError("mv2 plugins install takes a plugin's name")
        return plugins.install(name)
    root = universe.resolve(root)
    if plugin_action in NAMED and not name:
        raise ValueError(f"mv2 plugins {plugin_action} takes a plugin's name")
    if (assignments or removals) and plugin_action != "settings":
        raise ValueError("--set and --unset belong to mv2 plugins settings")
    if prune and plugin_action != "events":
        raise ValueError("--prune belongs to mv2 plugins events")
    if plugin_action == "host":
        from ..plugins import host
        top = root
        while universe.parent(top) is not None:  # one host per top-level universe tree
            top = universe.parent(top)
        action = name or "status"
        if action not in HOST_ACTIONS:
            raise ValueError(f"mv2 plugins host takes {', '.join(HOST_ACTIONS)}")
        return {"action": action, "universe": str(top), **getattr(host, action)(top)}
    if plugin_action == "migrate":
        scopes = list(universe.tree(root)) if recursive else [root]
        planned = [{**item, "universe": str(scope)} for scope in scopes for item in plugins.migrations(scope)]
        done = []
        if apply:
            for item in planned:
                if item.get("problem"):
                    continue
                try:
                    done.append(plugins.migrate(Path(item["universe"]), item))
                except (OSError, ValueError) as error:
                    item["problem"] = str(error)
        return {"universe": str(root), "planned": planned, "applied": done, "apply": apply}
    if plugin_action == "events":
        from ..plugins import events
        removed = [str(path) for path in events.prune(root)] if prune else []
        return {**events.summary(root), "pruned": removed}
    if plugin_action == "list":
        return {"universe": str(root), "plugins": plugins.listing(root), "problems": plugins.problems(root, reserved())}
    if plugin_action == "info":
        for item in plugins.listing(root):
            if item["name"] == name:
                return {"universe": str(root), **item}
        plugins.entry(root, name)  # raises with the reason
    if plugin_action in ("enable", "disable"):
        result = plugins.set_enabled(root, name, plugin_action == "enable")
        found = plugins.entry(root, name)
        from ..plugins import host
        top = root
        while universe.parent(top) is not None:
            top = universe.parent(top)
        needs_host = bool(found.manifest and host.NEEDS & found.manifest.extends)
        return {"universe": str(root), **result, "state": found.state, "problem": found.problem,
                "permissions": found.manifest.permission_texts() if found.manifest else [],
                "needs_host": needs_host, "host_running": host.running(top) if needs_host else None}
    if plugin_action == "settings":
        changes = _changes(assignments, removals)
        if changes:
            plugins.edit_settings(root, name, changes)
        values, problem, path = plugins.settings_of(root, name)
        return {"universe": str(root), "name": name, "path": str(path), "values": values, "problem": problem,
                "changed": sorted(changes)}
    found = plugins.entry(root, name)
    if found.source != "universe":
        raise ValueError(f"{name} is a {found.source} plugin; only plugins in a universe's plugins/ need trust")
    if plugin_action == "trust":
        return {"name": name, "directory": str(found.directory), "fingerprint": trust.trust(found.directory)}
    return {"name": name, "directory": str(found.directory), "forgotten": trust.forget(found.directory)}


TEMPLATE = '''"""{name}: what it does, in a sentence. See docs/plugins.md for the API."""


def shout(row):
    """An example column value: the agent's name in capitals. Column functions must be quick."""
    return str(row.get("name", "")).upper()


def register(mv):
    # Each call below needs its extension point in plugin.json "extends".
    mv.dashboard.column("shout", "Shout", 12, shout)
'''

TEST = '''"""Run with: python -m unittest discover -s tests (from the plugin directory)"""
from pathlib import Path

from multiverse.plugins.testing import PluginTestCase


class {cls}Test(PluginTestCase):
    plugin = Path(__file__).resolve().parents[1]

    def test_column(self):
        column = self.loaded().registrations.columns["shout"]
        self.assertEqual(column.value({{"name": "worker"}}), "WORKER")
'''


def scaffold(name, parent):
    """A new plugin directory: plugin.json, plugin.py with one example, and a test."""
    manifests.check_name(name)
    directory = (parent / name).resolve()
    if directory.exists():
        raise ValueError(f"{directory} already exists")
    (directory / "tests").mkdir(parents=True)
    (directory / "plugin.json").write_text(json.dumps({
        "schema": 1, "name": name, "version": "0.1.0", "description": "",
        "api": ".".join(map(str, loader.API_VERSION)), "module": "plugin", "extends": ["dashboard.column"],
        "permissions": []}, indent=2) + "\n")
    (directory / "plugin.py").write_text(TEMPLATE.format(name=name))
    cls = "".join(part.capitalize() for part in name.split("-"))
    (directory / "tests" / f"test_{name.replace('-', '_')}.py").write_text(TEST.format(cls=cls))
    return {"name": name, "directory": str(directory)}


def check(directory):
    """Everything about a plugin directory that can be told without turning it on anywhere."""
    directory = directory.expanduser().resolve()
    problems, notes = [], []
    try:
        found = manifests.load(directory)
    except manifests.ManifestError as error:
        return {"directory": str(directory), "ok": False, "problems": [str(error)], "notes": []}
    problem = loader.compatible(found)
    if problem:
        problems.append(f"{found.name} {problem}")
    taken = sorted(set(found.commands) & reserved())
    if taken:
        problems.append(f"commands mv2 itself uses: {', '.join(taken)}")
    if found.settings is not None:
        try:
            settings_files.schema_of(found)
            defaults = settings_files.defaults_of(found)
            schema = settings_files.schema_of(found)
            if defaults.get("schema") != found.settings.version:
                problems.append(f"its default settings must have \"schema\": {found.settings.version}")
            elif schema is not None:
                settings_files.validate({k: v for k, v in defaults.items() if k != "schema"}, schema)
        except ValueError as error:
            problems.append(f"settings: {error}")
    registered = None
    if not problems:
        entry = loader.Entry(found.name, "on", True, "path", directory, found)
        try:
            registered = loader.load(entry).registrations
        except loader.PluginError as error:
            problems.append(str(error))
    if registered is not None:
        missing = sorted(set(found.commands) - set(registered.commands))
        if missing:
            problems.append(f"declares commands it does not register: {', '.join(missing)}")
        used = {"cli": registered.commands, "bootstrap": registered.bootstrap, "doctor": registered.doctor,
                "dashboard.column": registered.columns, "dashboard.panel": {**registered.panels, **registered.data_panels},
                "dashboard.inspector": registered.inspector, "dashboard.theme": registered.theme,
                "dashboard.key": registered.keys, "dashboard.screen": registered.screens,
                "dashboard.settings": registered.settings_screen or registered.settings_fields,
                "dashboard.action": registered.actions,
                "launch": registered.launch or found.harnesses, "capabilities": registered.capabilities,
                "usage": registered.usage, "statusline": found.statusline, "helper": registered.helpers,
                "events": registered.handlers, "service": registered.services}
        unused = sorted(point for point in found.extends if point in used and not used[point])
        if unused:
            notes.append(f"extends but registers nothing for: {', '.join(unused)}")
        for kind, references in (("panel", registered.panels), ("screen", registered.screens)):
            for key, reference in references.items():
                notes.append(f"{kind} {key} is {reference}, imported only by the dashboard")
    return {"directory": str(directory), "name": found.name, "version": found.version,
            "ok": not problems, "problems": problems, "notes": notes}


def cli_run(root=None, *, plugin_action="list", name=None, assignments=(), removals=(), prune=False, apply=False,
            recursive=False, directory=None, json=False):
    result = run(root, plugin_action=plugin_action, name=name, assignments=assignments, removals=removals, prune=prune,
                 apply=apply, recursive=recursive, directory=directory)
    if json:
        return result
    if plugin_action == "check" and not result["ok"]:
        print(text(plugin_action, result))
        raise SystemExit(1)
    return text(plugin_action, result)


def _state(item):
    if item["state"] in ("on", "loaded"):
        return "on"
    if item["state"] == "off":
        return "off"
    return item["state"]


def text(action, result):
    if action == "list":
        rows = result["plugins"]
        if not rows:
            return "No plugins found. See docs/plugins.md to write or install one."
        width = max(len(item["name"]) for item in rows)
        lines = []
        for item in rows:
            line = f'{item["name"]:{width}}  {_state(item):11}  {item["source"] or "-":8}  {item["version"] or "-":8}  {item["description"]}'
            lines.append(line.rstrip())
            if item["problem"]:
                lines.append(f"  ! {item['problem']}")
            for error in item["errors"][-3:]:
                lines.append(f"  ! {error}")
        reported = {item["problem"] for item in rows}
        lines.extend(f"! {problem}" for problem in result["problems"] if problem not in reported)
        return "\n".join(lines)
    if action == "info":
        lines = [f'{result["name"]} {result["version"] or ""}'.rstrip() + (f': {result["description"]}' if result["description"] else ""),
                 f'{_state(result)} here; {result["source"]} plugin at {result["directory"]}']
        if result["problem"]:
            lines.append(f'! {result["problem"]}')
        if result["permissions"]:
            lines.append("It can:")
            lines.extend(f"- {text}" for text in result["permissions"])
            lines.append("Plugins run with your own permissions; these are what it asked for, not a sandbox.")
        for command, help_text in result["commands"].items():
            lines.append(f"Command: mv2 {command}: {help_text}")
        if result["settings"]:
            settings = result["settings"]
            lines.append(f'Settings: {settings["path"]} ({settings["source"]})'
                         + (f'; cannot be used: {settings["problem"]}' if settings["problem"] else ""))
        for shadow in result["shadowed"]:
            lines.append(f'Shadows its {shadow["source"]} copy at {shadow["directory"]}')
        lines.extend(f"Recent error: {error}" for error in result["errors"])
        return "\n".join(lines)
    if action in ("enable", "disable"):
        if not result["on"]:
            return f'Turned {result["name"]} off in {result["universe"]}.'
        lines = [f'Turned {result["name"]} on in {result["universe"]}.']
        if result["permissions"]:
            lines.append("It can:")
            lines.extend(f"- {text}" for text in result["permissions"])
        lines.extend(f"Installed its default settings: {path}" for path in result["installed"])
        if result["problem"]:
            lines.append(f'! {result["problem"]}')
        if result.get("needs_host") and not result.get("host_running"):
            lines.append("It runs in the plugin host, which is not running: start it with mv2 plugins host start "
                         "(the dashboard starts it too).")
        lines.append("Agents launched from now on use it; restart running agents to use it there.")
        return "\n".join(lines)
    if action == "settings":
        lines = [f'{result["name"]} settings: {result["path"]}']
        if result["changed"]:
            lines.append(f'Changed: {", ".join(result["changed"])}')
        if result["problem"]:
            lines.append(f'! Cannot be used, so the plugin is off here until fixed: {result["problem"]}')
        lines.append(json.dumps(result["values"], indent=2, ensure_ascii=False))
        return "\n".join(lines)
    if action == "trust":
        return f'Trusted {result["name"]} at its current content ({result["fingerprint"][:19]}…). Any change needs trust again.'
    if action == "untrust":
        return f'{result["name"]} is no longer trusted.' if result["forgotten"] else f'{result["name"]} was not trusted.'
    if action == "install":
        return (f'Installed {result["name"]} at {result["path"]}. Turn it on with mv2 plugins enable {result["name"]}.'
                if result["installed"] else f'Fetched {result["path"]}, but it holds no plugin.json.')
    if action == "new":
        return "\n".join([f'Created {result["name"]} at {result["directory"]}.',
                          f'Use it here while you work on it: export MV2_PLUGIN_PATH={result["directory"]}',
                          f'Then: mv2 plugins check {result["directory"]}, mv2 plugins enable {result["name"]}, and its tests:',
                          f'  python -m unittest discover -s {Path(result["directory"]) / "tests"}'])
    if action == "migrate":
        if not result["planned"]:
            return "Nothing to migrate."
        lines = []
        applied = {(item["plugin"], item["universe"]) for item in result["applied"]}
        for item in result["planned"]:
            what = (f'{item["from"]} -> {item["to"]}' if item["kind"] == "legacy"
                    else f'{item["to"]} from settings version {item["version"]}')
            if (item["plugin"], item["universe"]) in applied:
                lines.append(f'Migrated {item["plugin"]}: {what}')
            elif item.get("problem"):
                lines.append(f'! {item["plugin"]}: {what}: {item["problem"]}')
            else:
                lines.append(f'Would migrate {item["plugin"]}: {what}')
        if not result["apply"] and any(not item.get("problem") for item in result["planned"]):
            lines.append("Run with --apply to make these changes; old files go to the universe's trash.")
        return "\n".join(lines)
    if action == "host":
        state = "running" if result["running"] else "not running"
        lines = [f'The plugin host for {result["universe"]} is {state}' + (f' (pid {result["pid"]}).' if result["running"] and result.get("pid") else ".")]
        if result.get("needed"):
            lines.append(f'Plugins that need it: {", ".join(result["needed"])}.')
        elif result["action"] in ("start", "status"):
            lines.append("No plugin that is on here needs it (none has event handlers or services).")
        for worker, info in (result.get("workers") or {}).items():
            lines.append(f'- {worker}: {info["state"]}' + (f', restarted {info["restarts"]} times' if info.get("restarts") else ""))
        for worker, reason in (result.get("stopped") or {}).items():
            lines.append(f"! {worker}: {reason}")
        return "\n".join(lines)
    if action == "events":
        if not result["segments"]:
            return f'No event journal in {result["universe"]}: no plugin on here subscribes to events.'
        lines = [f'{result["segments"]} segments, {result["bytes"] / 1024:.0f} KiB, kept {result["retain_days"]:g} days '
                 f'and at most {result["max_mb"]:g} MiB; newest event {result["newest_seq"]}.']
        if result["oldest"]:
            lines.append(f'Oldest kept: {result["oldest"]["seq"]} at {result["oldest"]["at"]}.')
        lines.extend(f"Cursor {name}: {seq}" for name, seq in result["cursors"].items())
        if result["pruned"]:
            lines.append(f'Pruned {len(result["pruned"])} segments.')
        return "\n".join(lines)
    if action == "check":
        lines = [f'{result.get("name", result["directory"])}: ' + ("ready." if result["ok"] else "not usable.")]
        lines.extend(f"! {problem}" for problem in result["problems"])
        lines.extend(f"Note: {note}" for note in result["notes"])
        return "\n".join(lines)
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
