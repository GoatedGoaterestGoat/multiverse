"""`python -m multiverse.plugins run PLUGIN ACTION [ARGUMENTS...]`: a plugin's helper action.

Harness extensions (a plugin's Claude or Pi code) reach mv2 this way, with the
agent's own Python ($MV2_PYTHON) and environment: the universe is -u, else
MV2_UNIVERSE, else the current directory. The plugin must be on there. The
action's return value is the exit code (None is 0); it prints its own output.
An action that fails unexpectedly prints `mv2 plugin PLUGIN ACTION: ...` and
exits 1, with the traceback in the plugin's errors.log. mv2's own failures (a
usage error, a universe or plugin that cannot be used, a plugin that fails to
load) exit 1, never 2: 2 is left to the action, as a deliberate refusal. Before the action, a
pre-plugin settings file in the universe is converted, once (stderr says so).
"""
import argparse
import os
import sys

from .. import universe
from . import PluginError, entry, loader, migrate_on_first_use
from .api import AccessDenied


class _Parser(argparse.ArgumentParser):
    """Its own usage errors exit 1: exit 2 is the action's alone, a deliberate refusal (compaction's guard)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def main(argv=None):
    parser = _Parser(prog="python -m multiverse.plugins", description=__doc__,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-u", "--universe", dest="root")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run a plugin's helper action")
    run.add_argument("plugin")
    run.add_argument("action")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    loader.process = "helper"
    try:
        root = universe.resolve(args.root)
        # The action's universe is this one, whatever the environment says: the API resolves from MV2_UNIVERSE.
        os.environ["MV2_UNIVERSE"] = str(root)
        # Any pre-plugin settings file here is converted (once) before the action runs.
        for line in migrate_on_first_use(root):
            print(line, file=sys.stderr)
        loaded = loader.load(entry(root, args.plugin))
        action = loaded.registrations.helpers.get(args.action)
        if action is None:
            raise PluginError(f"{args.plugin} has no helper action {args.action!r}")
    except (OSError, ValueError) as error:
        print(f"mv2 plugin {args.plugin} {args.action}: {error}", file=sys.stderr)
        return 1
    try:
        code = action(list(args.arguments))
    except AccessDenied as error:  # refused for access before it ran: 1, never the action's own 2
        print(f"mv2 plugin {args.plugin} {args.action}: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        text = loader.record(loaded, f"helper {args.action}", error, root)
        print(f"mv2 plugin {args.plugin} {args.action}: {text}", file=sys.stderr)
        return 1
    return code if isinstance(code, int) and not isinstance(code, bool) else 0


if __name__ == "__main__":
    sys.exit(main())
