"""Print an exact matching prompt; never execute it."""
import argparse
from .. import universe
from ..universe import catalog
from .catalog import warning

def configure(parser):
    parser.add_argument("name")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)


def run(root=None, *, name, arguments=()):
    return catalog.command(universe.resolve(root), name, " ".join(arguments), on_error=warning)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
