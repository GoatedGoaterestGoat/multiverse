"""Observe the current native session and launch state."""
from .. import native, universe


def configure(parser):
    parser.add_argument("agent")


def run(root=None, *, agent):
    return native.operation(universe.resolve(root), agent, "observe")


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
