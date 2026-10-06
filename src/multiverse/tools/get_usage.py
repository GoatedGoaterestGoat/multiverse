"""Read measured usage for the exact native session."""
from ..runtime import native
from .. import universe


def configure(parser):
    parser.add_argument("agent")


def run(root=None, *, agent):
    return native.operation(universe.resolve(root), agent, "get_usage")


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
