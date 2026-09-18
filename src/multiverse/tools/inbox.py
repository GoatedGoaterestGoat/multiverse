"""Peek at unread complete inbox messages."""
from .. import store
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--after", type=int, help="Read a later page without acknowledging skipped messages")


def run(root=None, *, agent=None, after=None):
    root, directory, launch_id = callback(root, agent)
    return store.inbox(directory, after=after)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
