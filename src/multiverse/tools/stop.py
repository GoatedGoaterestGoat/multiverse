"""Alias for kill: end native work, then remove the owned tmux wrapper."""
from ..runtime import lifecycle
from .. import universe

def configure(parser):
    parser.add_argument("agent")
    parser.add_argument('--attachment-only', action='store_true', help='Close mv2\'s terminal without requesting native cancellation; work owned by that process ends')
    parser.add_argument('--json', action='store_true', help='Print the full record as JSON')


def run(root=None, *, agent, attachment_only=False, json=False):  # `json`: presentation only
    return lifecycle.kill(universe.resolve(root), agent, attachment_only=attachment_only)


def cli_run(root=None, *, agent, attachment_only=False, json=False):
    from .kill import stopped
    record = run(root, agent=agent, attachment_only=attachment_only)
    return record if json else stopped(root, agent, record, attachment_only)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
