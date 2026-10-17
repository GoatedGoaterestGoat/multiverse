"""Alias for kill: end native work, then remove the owned tmux wrapper."""
from ..runtime import lifecycle
from .. import universe

def configure(parser):
    parser.add_argument("agent")
    parser.add_argument('--attachment-only', action='store_true', help='Close mv2\'s terminal without requesting native cancellation; work owned by that process ends')


def run(root=None, *, agent, attachment_only=False):
    return lifecycle.kill(universe.resolve(root), agent, attachment_only=attachment_only)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
