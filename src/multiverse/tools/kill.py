"""End native work and its process, then remove the agent's tmux wrapper."""
from ..runtime import lifecycle
from .. import universe


def configure(parser):
    parser.add_argument('agent')


def run(root=None, *, agent):
    return lifecycle.kill(universe.resolve(root), agent)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
