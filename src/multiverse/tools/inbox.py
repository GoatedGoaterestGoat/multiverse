"""Peek at unread complete inbox messages."""
from .. import store
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--all", dest="all_messages", action="store_true", help="Include native delivery and acknowledged history")
    parser.add_argument("--after", type=int, help="Read a later page without acknowledging skipped messages")


def run(root=None, *, agent=None, after=None, all_messages=False):
    root, directory, launch_id = callback(root, agent)
    return store.inbox(directory, after=after, all_messages=all_messages)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
