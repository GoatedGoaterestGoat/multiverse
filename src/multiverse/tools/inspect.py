"""Capture the owned live terminal pane."""
from .. import agents, universe

def configure(parser):
    parser.add_argument("agent")


def run(root=None, *, agent):
    return agents.terminal_action(universe.resolve(root), agent, "inspect")


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
