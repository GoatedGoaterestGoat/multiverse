"""Alias for kill: end native work, then remove the owned tmux wrapper."""
from ..runtime import lifecycle
from .. import universe

def configure(parser):
    parser.add_argument("agent")


def run(root=None, *, agent):
    return lifecycle.terminal_action(universe.resolve(root), agent, "stop")


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
