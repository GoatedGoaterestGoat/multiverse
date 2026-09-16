"""Compatibility command line; each action lives in tools/<name>.py."""
import argparse
from importlib import import_module

from .tools._common import add_universe, output

# Explicit built-ins only. No plugin discovery or registration machinery.
COMMANDS = ("init", "subverse", "list", "catalog", "command", "spawn", "resume",
            "stop", "attach", "inspect", "send", "inbox", "ack", "status",
            "runtime-update", "session-bind", "tui", "doctor")


def tool(action):
    name = "list_agents" if action == "list" else action.replace("-", "_")
    return import_module(f".tools.{name}", __package__)


def parser():
    result = argparse.ArgumentParser(description="Local prompt-led universes. No daemon or MCP required.")
    add_universe(result)
    commands = result.add_subparsers(dest="action", required=True)
    for action in COMMANDS:
        module = tool(action)
        module.configure(commands.add_parser(action, help=module.__doc__))
    return result


def run(args):
    values = vars(args).copy()
    action = values.pop("action")
    return tool(action).run(**values)


def main():
    args = parser().parse_args()
    output(lambda: run(args))


if __name__ == "__main__":
    main()
