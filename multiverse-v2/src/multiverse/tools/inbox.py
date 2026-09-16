"""Peek at unread complete inbox messages."""
from .. import store
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)


def run(root=None, *, agent=None):
    root, directory, launch_id = callback(root, agent)
    return store.inbox(directory)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
