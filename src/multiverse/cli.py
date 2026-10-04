"""Compatibility command line; each action lives in tools/<name>.py."""
import argparse
import os
from pathlib import Path
from importlib import import_module

from . import universe
from .tools._common import add_universe, output

# Explicit built-ins only. No plugin discovery or registration machinery.
# Harness integrations report status with `python -m multiverse.tools.runtime_update`;
# it is not a command, so agents report their phase but never set their status.
COMMANDS = ("init", "subverse", "list", "catalog", "command", "spawn", "resume",
            "stop", "kill", "move", "remove", "trash", "attach", "inspect", "send", "inbox", "report",
            "session-bind", "capabilities", "deliver", "native-status", "ask", "get-usage", "tui", "doctor",
            "refresh", "compaction")


# Former names that keep working for agents launched with older prompts.
ALIASES = {"report": ["status"]}


def tool(action):
    action = {"status": "report"}.get(action, action)
    name = "list_agents" if action == "list" else action.replace("-", "_")
    return import_module(f".tools.{name}", __package__)


def parser():
    result = argparse.ArgumentParser(description="Local prompt-led universes. With no command, open the dashboard.")
    add_universe(result)
    commands = result.add_subparsers(dest="action")
    for action in COMMANDS:
        module = tool(action)
        module.configure(commands.add_parser(action, help=module.__doc__, aliases=ALIASES.get(action, [])))
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
                values["root"] = universe.create(cwd / ".multiverse", cwd.name,
                                                template=str(universe.TEMPLATES / "base"), workdir=cwd)
    module = tool(action)
    handler = getattr(module, 'cli_run', module.run) if cli else module.run
    return handler(**values)


def main():
    args = parser().parse_args()
    module = tool(args.action or 'tui')
    render = None if hasattr(module, 'cli_run') else getattr(module, 'format_output', None)
    output(lambda: run(args, cli=True), render=render)


if __name__ == "__main__":
    main()
