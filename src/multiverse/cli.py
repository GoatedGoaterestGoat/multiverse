"""Compatibility command line; each action lives in tools/<name>.py."""
import argparse
import os
from pathlib import Path
from importlib import import_module
import sys
from types import SimpleNamespace

from . import universe
from .universe import templates
from .tools._common import add_universe, output

# Built-ins. Plugins that are on in the selected universe add their own commands
# (see plugin_commands); these names, and the aliases, are never theirs.
# Harness integrations report status with `python -m multiverse.tools.runtime_update`;
# it is not a command, so agents report their phase but never set their status.
COMMANDS = ("init", "subverse", "list", "catalog", "command", "spawn", "resume",
            "stop", "kill", "move", "remove", "trash", "attach", "inspect", "send", "inbox", "report",
            "session-bind", "capabilities", "deliver", "native-status", "ask", "get-usage", "tui", "doctor",
            "refresh", "update", "plugins")


# Former names that keep working for agents launched with older prompts.
ALIASES = {"report": ["status"]}
RESERVED = frozenset(COMMANDS) | {alias for names in ALIASES.values() for alias in names}


def builtin(action):
    return action is None or action in RESERVED


def tool(action, root=None):
    """The module (or, for a plugin's command, an object shaped like one) that runs `action`."""
    if not builtin(action):
        from . import plugins
        command = plugins.command(universe.resolve(root), action, RESERVED, guarded=True)
        return SimpleNamespace(__doc__=None, configure=command.configure, run=command.run,
                               **({"cli_run": command.cli_run} if command.cli_run else {}))
    action = {"status": "report"}.get(action, action)
    name = "list_agents" if action == "list" else action.replace("-", "_")
    return import_module(f".tools.{name}", __package__)


def _selection(argv):
    """(-u value, the action, whether help was asked for before the action), read before the full parser exists.

    argparse reads the global options exactly as the full parser will (every
    spelling and abbreviation of -u/--universe and -h/--help, clustered short
    options), and everything from the action on is left to that command, even
    an argument spelled -u or --universe. Malformed arguments are left for the
    full parser to report.
    """
    early = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    add_universe(early)
    early.add_argument("-h", "--help", action="store_true", dest="wants_help")
    early.add_argument("rest", nargs=argparse.REMAINDER)
    try:
        known, _ = early.parse_known_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return None, None, False
    rest = [item for item in known.rest if item != "--"][:1]
    return known.root, (rest[0] if rest else None), known.wants_help


def plugin_commands(argv):
    """(universe, {command: (plugin, help)}) offered by plugins on in the universe `argv` selects.

    Only manifests are read: no plugin code runs to list commands. No universe,
    or anything going wrong while looking, means no plugin commands; the
    built-in commands never depend on plugins.
    """
    root = _selection(argv)[0]
    try:
        selected = universe.resolve(root)
        from . import plugins
        return selected, plugins.commands(selected, RESERVED)
    except Exception:
        return None, {}


def parser(argv=None):
    """The command line. Given `argv`, it also offers the commands of plugins on in the universe it selects."""
    result = argparse.ArgumentParser(description="Local prompt-led universes. With no command, open the dashboard.")
    add_universe(result)
    commands = result.add_subparsers(dest="action")
    for action in COMMANDS:
        module = tool(action)
        module.configure(commands.add_parser(action, help=module.__doc__, aliases=ALIASES.get(action, [])))
    if argv is not None:
        selected, offered = plugin_commands(argv)
        _, invoked, wants_help = _selection(argv)
        for name, (plugin, text) in sorted(offered.items()):
            command = commands.add_parser(name, help=f"{text} (plugin {plugin})")
            # Help asked for before the command is the top-level help: manifests only.
            if name == invoked and not wants_help:
                tool(name, selected).configure(command)
    return result


def run(args, *, cli=False):
    values = vars(args).copy()
    action = values.pop("action")
    if action is None:
        action = "tui"
        if values["root"] is None and not os.environ.get("MV2_UNIVERSE"):
            try:
                values["root"] = universe.resolve()
            except universe.UniverseNotFoundError:
                cwd = Path.cwd()
                values["root"] = templates.create(cwd / ".multiverse", cwd.name,
                                                 template=str(templates.TEMPLATES / "base"), workdir=cwd)
    if not builtin(action):
        values["root"] = universe.resolve(values["root"])
    module = tool(action, values["root"])
    handler = getattr(module, 'cli_run', module.run) if cli else module.run
    return handler(**values)


def main():
    argv = sys.argv[1:]
    try:
        args = parser(argv).parse_args(argv)
        module = tool(args.action or 'tui', args.root)
    except ValueError as error:  # a plugin's command that cannot load says why
        print(f"mv2: {error}", file=sys.stderr)
        sys.exit(1)
    render = None if hasattr(module, 'cli_run') else getattr(module, 'format_output', None)
    output(lambda: run(args, cli=True), render=render)


if __name__ == "__main__":
    main()
