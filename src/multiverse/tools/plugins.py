"""List, turn on or off, configure, trust and check plugins."""
import argparse
import json
import sys
import time
from pathlib import Path

from .. import plugins, universe
from ..plugins import access as levels, loader, manifest as manifests, settings as settings_files, trust

ACTIONS = ("list", "info", "enable", "disable", "grant", "settings", "trust", "untrust", "check", "host", "events",
           "install", "migrate", "new")
HOST_ACTIONS = ("status", "start", "stop")
NAMED = ("info", "enable", "disable", "grant", "settings", "trust", "untrust")


def configure(parser):
    parser.add_argument("plugin_action", nargs="?", default="list", choices=ACTIONS, metavar="ACTION",
                        help="list (default), info, enable, disable, grant, settings, trust, untrust, check, host, "
                             "events, install, migrate or new")
    parser.add_argument("name", nargs="?", help="The plugin (for check: its directory; for host: start, stop or status; "
                                               "for new: the new plugin's name)")
    parser.add_argument("level", nargs="?", metavar="LEVEL", help="grant: observe, spend or act")
    parser.add_argument("--grant", choices=levels.LEVELS, dest="grant",
                        help="enable: the access level to grant it here (otherwise a terminal asks; without one, "
                             "its minimum)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", dest="assignments",
                        help="settings: change one setting (VALUE is JSON, or else text); repeatable")
    parser.add_argument("--unset", action="append", default=[], metavar="KEY", dest="removals",
                        help="settings: remove one setting, back to its default; repeatable")
    parser.add_argument("--prune", action="store_true", help="events: delete journal segments past retention now")
    # An optional value, so `check --load DIR` works on every Python (3.11's argparse gives DIR no positional).
    parser.add_argument("--load", nargs="?", const=True, default=False, metavar="DIR",
                        help="check: import the plugin even when it is a universe plugin that is not trusted "
                             "(check DIR --load, or check --load DIR)")
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
        recursive=False, directory=None, load=False, level=None, grant=None, json=False):
    if plugin_action not in ACTIONS:
        raise ValueError(f"Unknown plugins action {plugin_action!r}; use one of {', '.join(ACTIONS)}")
    if level is not None and plugin_action != "grant":
        raise ValueError(f"mv2 plugins {plugin_action} takes one name")
    if grant is not None and plugin_action != "enable":
        raise ValueError("--grant belongs to mv2 plugins enable; to change a grant: mv2 plugins grant NAME LEVEL")
    if (apply or recursive) and plugin_action != "migrate":
        raise ValueError("--apply and --recursive belong to mv2 plugins migrate")
    if directory and plugin_action != "new":
        raise ValueError("--dir belongs to mv2 plugins new")
    if plugin_action == "check":
        if isinstance(load, str):  # check --load DIR
            name, load = name or load, True
        if not name:
            raise ValueError("mv2 plugins check takes the plugin's directory")
        return check(Path(name), load=bool(load))
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
        planned, done = [], []
        for scope in scopes:
            if not apply:
                planned.extend({**item, "universe": str(scope)} for item in plugins.migrations(scope))
                continue
            # The lock a first-use conversion holds: planned again under it, so nothing is converted twice.
            from ..store import lock
            with lock(scope / "state" / "plugins" / plugins.MIGRATE_LOCK):
                here = [{**item, "universe": str(scope)} for item in plugins.migrations(scope)]
                planned.extend(here)
                for item in here:
                    if item.get("problem"):
                        continue
                    try:
                        done.append(plugins.migrate(scope, item))
                    except (OSError, ValueError) as error:
                        item["problem"] = str(error)
                        item["unchanged"] = getattr(error, "unchanged", True)
        if apply:
            # Already-shipped updaters invoke this command from the newly updated checkout.
            # Receipt migration is independent: no changes to plugin JSON or exit status.
            try:
                from ..runtime.receipt_migration import after_update, SECONDS
                deadline = time.monotonic() + SECONDS
                for scope in scopes:
                    after_update(scope, deadline=deadline)
            except Exception as error:
                print(f"Receipt migration did not run: {error}", file=sys.stderr)
        result = {"universe": str(root), "planned": planned, "applied": done, "apply": apply,
                  "not_installed": [{"plugin": name, "file": legacy, "universe": str(scope)}
                                    for scope in scopes for name, legacy in plugins.not_installed(scope)]}
        if apply and any(item.get("problem") for item in planned):
            result["ok"] = False  # something was not converted: exit 1
        return result
    if plugin_action == "events":
        from ..plugins import events
        removed = [str(path) for path in events.prune(root)] if prune else []
        return {**events.summary(root), "pruned": removed}
    if plugin_action == "list":
        return {"universe": str(root), "plugins": plugins.listing(root, wait=None),
                "problems": plugins.problems(root, reserved())}
    if plugin_action == "info":
        from ..plugins import run_records
        for item in plugins.listing(root, wait=None):
            if item["name"] == name:
                return {"universe": str(root), **item, "runs": run_records.summary(root, name)}
        plugins.entry(root, name)  # raises with the reason
    if plugin_action == "grant":
        if level is None:
            raise ValueError("mv2 plugins grant takes a plugin's name and a level: observe, spend or act")
        return {"universe": str(root), **plugins.set_grant(root, name, level),
                "access": plugins.entry_access(root, name)}
    if plugin_action in ("enable", "disable"):
        defaulted = False
        if plugin_action == "enable" and grant is None:
            # No terminal asked (cli_run asks on one): its minimum, raised later with mv2 plugins grant.
            wanted = plugins.entry(root, name).manifest
            if wanted is not None and wanted.access.minimum != wanted.access.request and not plugins.from_agent():
                grant, defaulted = wanted.access.minimum, True
        result = plugins.set_enabled(root, name, plugin_action == "enable", grant=grant)
        found = plugins.entry(root, name)
        from ..plugins import host
        top = root
        while universe.parent(top) is not None:
            top = universe.parent(top)
        needs_host = bool(found.manifest and host.NEEDS & found.manifest.extends)
        return {"universe": str(root), **result, "state": found.state, "problem": found.problem,
                "access": plugins.entry_access(root, name), "defaulted": defaulted,
                "permissions": found.manifest.permission_texts() if found.manifest else [],
                "needs_host": needs_host, "host_running": host.running(top) if needs_host else None}
    if plugin_action == "settings":
        changes = _changes(assignments, removals)
        unchecked = plugins.settings_checked(root, name)  # its own check, run before anything is written
        if changes:
            plugins.edit_settings(root, name, changes)
        values, problem, path, notes = plugins.settings_of(root, name)
        return {"universe": str(root), "name": name, "path": str(path), "values": values, "problem": problem,
                "notes": list(notes),
                "changed": sorted(changes), "unchecked": unchecked}
    # This universe's own copy, even when another source has a plugin of that name: while untrusted it hides
    # nothing, so looking the name up would find the other one.
    copy = plugins.universe_copy(root, name)
    if copy is None:
        found = plugins.entry(root, name)
        raise ValueError(f"{name} is a {found.source} plugin; only plugins in a universe's plugins/ need trust")
    if plugin_action == "trust":
        value = trust.trust(copy)
        after = plugins.entry(root, name)
        overrides = ([source for source, _ in after.shadowed if source != "universe"]
                     if after.source == "universe" and Path(after.directory).resolve() == copy else [])
        return {"name": name, "directory": str(copy), "fingerprint": value, "unreadable": trust.compiled(copy),
                "overrides": overrides}
    return {"name": name, "directory": str(copy), "forgotten": trust.forget(copy)}


TEMPLATE = '''"""{name}: what it does, in a sentence. See docs/develop/plugins.md for the API."""


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
    if name in manifests.RESERVED_NAMES or name in reserved():
        raise ValueError(f"{name!r} is reserved: mv2 uses it itself; choose another name")
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


def _universe_plugin(directory):
    """Whether `directory` is a plugin in some universe's own plugins/ (which runs only once trusted)."""
    return directory.parent.name == "plugins" and (directory.parent.parent / "universe.json").is_file()


def check(directory, load=False):
    """Everything about a plugin directory that can be told without turning it on anywhere.

    A universe's own plugin that is not trusted is checked without importing
    it (manifest, schema and default settings only), unless `load` asks for
    its code; a trusted one is checked from its trusted copy."""
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
    registered, state = None, None
    running = directory
    if not problems and _universe_plugin(directory):
        state, value = trust.verify(directory)
        if state != "trusted" and not load:
            notes.append(f"its code was not imported: it is not trusted (run mv2 plugins trust {found.name} after "
                         f"reviewing it, or check it with --load to import it now)")
            return {"directory": str(directory), "ok": True, "problems": [], "notes": notes, "name": found.name}
    if not problems and _universe_plugin(directory) and state == "trusted":
        try:  # trusted: checked from its trusted copy, so nothing is written into the universe's directory
            running = trust.cached(directory, value)
            found = manifests.load(running)
        except (OSError, ValueError, manifests.ManifestError) as error:
            problems.append(f"its trusted copy could not be made: {error}")
    if not problems:
        entry = loader.Entry(found.name, "on", True, "path", running, found)
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
                "dashboard.screen": registered.screens,
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
            recursive=False, directory=None, load=False, level=None, grant=None, json=False):
    if plugin_action == "enable" and grant is None and not json and name:
        grant = _ask_grant(root, name)
    result = run(root, plugin_action=plugin_action, name=name, assignments=assignments, removals=removals, prune=prune,
                 apply=apply, recursive=recursive, directory=directory, load=load, level=level, grant=grant)
    if json:
        return result
    if plugin_action in ("check", "migrate") and result.get("ok") is False:
        print(text(plugin_action, result))
        raise SystemExit(1)  # a failed check, or a migration that did not apply
    return text(plugin_action, result)


def _ask_grant(root, name):
    """On a terminal, for a person: the level to grant, asked from the plugin's request down to its minimum.
    None (no terminal, an agent, or nothing to choose): run() decides."""
    if plugins.from_agent() or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    found = plugins.entry(universe.resolve(root), name)
    if found.manifest is None:
        return None
    wanted = found.manifest.access
    choices = list(reversed(levels.LEVELS[levels.rank(wanted.minimum):levels.rank(wanted.request) + 1]))
    if len(choices) < 2:
        return None
    print(f"{name} {levels.ASKS[wanted.request]} (it asks for {wanted.request}; it can work with {wanted.minimum})."
          + (f" {wanted.why}" if wanted.why else ""))
    for number, choice in enumerate(choices, 1):
        print(f"  {number}. {choice}: {levels.MEANING[choice]}")
    while True:
        answer = input(f"Grant which level here? [1-{len(choices)}, Enter for {choices[0]}] ").strip().lower()
        if not answer:
            return choices[0]
        if answer in choices:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        print(f"Type a number from 1 to {len(choices)}, or a level: {', '.join(choices)}.")


def _permission_lines(result):
    """What a plugin may do here: what its access allows, then what that level holds back; what it asks for while
    it is off, where it has no access."""
    access = result.get("access") or {}
    if access.get("allowed") is None:
        return ["It asks to:", *(f"- {text}" for text in result["permissions"])]
    lines = ["It can:", *(f"- {text}" for text in access["allowed"])] if access["allowed"] else []
    if access.get("held_back"):
        lines.append(f'Held back by its {access["level"]} access ({access["raise"]}):')
        lines.extend(f'- {item["text"]} (needs {item["needs"]})' for item in access["held_back"])
    return lines


def _access_line(access):
    level, request, minimum = access.get("level"), access["request"], access["minimum"]
    asks = f"it asks for {request}" + (f", at least {minimum}" if minimum != request else ", and needs it")
    if level is None:
        return f"Access: {asks}; it has none while it is off."
    line = f"Access here: {level} ({asks})"
    caps = {item["by"]: item["level"] for item in access.get("limited_by") or []}
    if "MV2_EFFECTS" in caps:
        line += f"; MV2_EFFECTS caps every plugin in this process at {caps['MV2_EFFECTS']}"
    if "access_ceiling" in caps:
        line += f"; this universe's access_ceiling allows at most {caps['access_ceiling']}"
    return line + "."


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
            return "No plugins found. See docs/use/plugins.md to install one, or docs/develop/plugins.md to write one."
        width = max(len(item["name"]) for item in rows)
        lines = []
        for item in rows:
            line = f'{item["name"]:{width}}  {_state(item):11}  {item["source"] or "-":8}  {item["version"] or "-":8}  {item["description"]}'
            lines.append(line.rstrip())
            for override in item.get("overrides", []):
                lines.append(f'  overrides the {override["source"]} {item["name"]} ({override["directory"]})')
            if item["problem"]:
                lines.append(f"  ! {item['problem']}")
            for error in item["errors"][-3:]:
                lines.append(f"  ! {error}")
        reported = {item["problem"] for item in rows} | {error for item in rows for error in item["errors"][-3:]}
        lines.extend(f"! {problem}" for problem in result["problems"] if problem not in reported)
        return "\n".join(lines)
    if action == "info":
        lines = [f'{result["name"]} {result["version"] or ""}'.rstrip() + (f': {result["description"]}' if result["description"] else ""),
                 f'{_state(result)} here; {result["source"]} plugin at {result["directory"]}']
        for override in result.get("overrides", []):
            lines.append(f'It overrides the {override["source"]} {result["name"]} at {override["directory"]}.')
        if result["problem"]:
            lines.append(f'! {result["problem"]}')
        if result.get("access"):
            lines.append(_access_line(result["access"]))
            if result["access"].get("why"):
                lines.append(f'Why: {result["access"]["why"]}')
        if result["permissions"]:
            lines.extend(_permission_lines(result))
            lines.append("Plugins run with your own permissions: these limits guard against mistakes, not a sandbox.")
        runs = result.get("runs")
        if runs is not None:
            failed = ", ".join(f"{kind} {count}" for kind, count in sorted(runs["outcomes"].items()) if kind != "ok")
            lines.append(f'Runs in the last day: {runs["runs"]}' + (f' ({runs["failed"]} failed: {failed})' if failed else "")
                         + f', ${runs["cost_usd"]:.4f}.')
        for command, help_text in result["commands"].items():
            lines.append(f"Command: mv2 {command}: {help_text}")
        if result["settings"]:
            settings = result["settings"]
            lines.append(f'Settings: {settings["path"]} ({settings["source"]})'
                         + (f'; cannot be used: {settings["problem"]}' if settings["problem"] else ""))
            lines.extend(f"  Note: {note}" for note in settings.get("notes") or ())
        for shadow in result["shadowed"]:
            lines.append(f'Shadows its {shadow["source"]} copy at {shadow["directory"]}')
        lines.extend(f"Recent error: {error}" for error in result["errors"])
        return "\n".join(lines)
    if action in ("enable", "disable"):
        if not result["on"]:
            return f'Turned {result["name"]} off in {result["universe"]}.'
        access = result.get("access") or {}
        lines = [f'Turned {result["name"]} on in {result["universe"]}.']
        if access.get("level"):
            lines[0] = (f'Turned {result["name"]} on in {result["universe"]}, with {access["level"]} access: '
                        f'it {levels.MEANING[access["level"]]}.')
            if access["level"] != access["request"]:
                hint = access.get("raise") or f'a person can grant more with: mv2 plugins grant {result["name"]} {access["request"]}'
                lines.append(f'It asks for {access["request"]}. {hint[0].upper()}{hint[1:]}'
                             + (" (no terminal asked, so it has its minimum)" if result.get("defaulted") else ""))
        if result["permissions"]:
            lines.extend(_permission_lines(result))
        lines.extend(f"Installed its default settings: {path}" for path in result["installed"])
        if result["problem"]:
            lines.append(f'! {result["problem"]}')
        if result.get("needs_host") and not result.get("host_running"):
            lines.append("It runs in the plugin host, which is not running: start it with mv2 plugins host start "
                         "(the dashboard starts it too).")
        lines.append("Agents launched from now on use it; restart running agents to use it there.")
        return "\n".join(lines)
    if action == "grant":
        level = result.get("level")
        return (f'Granted {result["name"]} {result["granted"]} access in {result["universe"]}'
                + (f'; it has {level} here now.' if level and level != result["granted"]
                   else "." if level else "; it has none while it is off.")
                + " Agents launched from now on use it; restart running agents to use it there.")
    if action == "settings":
        lines = [f'{result["name"]} settings: {result["path"]}']
        if result["changed"]:
            lines.append(f'Changed: {", ".join(result["changed"])}')
        if result["problem"]:
            lines.append(f'! Cannot be used, so the plugin is off here until fixed: {result["problem"]}')
        lines.extend(f"Note: {note}" for note in result.get("notes") or ())
        if result.get("unchecked"):
            lines.append(f'! {result["name"]}\'s own settings check did not run ({result["unchecked"]}): '
                         'only its schema was checked.')
        lines.append(json.dumps(result["values"], indent=2, ensure_ascii=False))
        return "\n".join(lines)
    if action == "trust":
        text = (f'Trusted this universe\'s copy of {result["name"]} ({result["directory"]}) at its current content '
                f'({result["fingerprint"][:19]}…). Any change needs trust again.'
                + "".join(f' It now overrides the {source} {result["name"]}.' for source in result.get("overrides", [])))
        if result.get("unreadable"):
            text += ("\n! It ships code you cannot read as source, which runs with your permissions: "
                     + ", ".join(result["unreadable"]) + ". Trust it only if you trust where these came from.")
        return text
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
        waiting = [f'! {plugins.not_installed_message(item["plugin"], item["file"])}'
                   + ("" if item["universe"] == result["universe"] else f' (in {item["universe"]})')
                   for item in result.get("not_installed", [])]
        if not result["planned"]:
            return "\n".join(waiting) if waiting else "Nothing to migrate."
        lines = list(waiting)
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
        if result.get("problem"):
            lines.append(f'! {result["problem"]}; the host rewrites it when it starts.')
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
        unchecked = any(note.startswith("its code was not imported") for note in result["notes"])
        lines = [f'{result.get("name", result["directory"])}: ' + (
            "manifest and settings look right; its code was not checked." if result["ok"] and unchecked
            else "ready." if result["ok"] else "not usable.")]
        lines.extend(f"! {problem}" for problem in result["problems"])
        lines.extend(f"Note: {note}" for note in result["notes"])
        return "\n".join(lines)
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
