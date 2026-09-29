"""Compatibility command line; each action lives in tools/<name>.py."""
import argparse
import os
from pathlib import Path
from importlib import import_module

from . import universe
from .tools._common import add_universe, output

# Explicit built-ins only. No plugin discovery or registration machinery.
COMMANDS = ("init", "subverse", "list", "catalog", "command", "spawn", "resume",
            "stop", "move", "remove", "trash", "attach", "inspect", "send", "inbox", "ack", "status",
            "runtime-update", "session-bind", "capabilities", "deliver", "native-status", "ask", "get-usage", "tui", "doctor")


def tool(action):
    name = "list_agents" if action == "list" else action.replace("-", "_")
    return import_module(f".tools.{name}", __package__)


def parser():
    result = argparse.ArgumentParser(description="Local prompt-led universes. With no command, open the dashboard.")
    add_universe(result)
    commands = result.add_subparsers(dest="action")
    for action in COMMANDS:
        module = tool(action)
        module.configure(commands.add_parser(action, help=module.__doc__))
    return result


def run(args):
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
    return tool(action).run(**values)


def main():
    args = parser().parse_args()
    output(lambda: run(args))


if __name__ == "__main__":
    main()
