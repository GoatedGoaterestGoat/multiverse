"""Acknowledge a previously read inbox byte cursor."""
from .. import store
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--cursor", type=int, required=True)


def run(root=None, *, agent=None, cursor):
    root, directory, launch_id = callback(root, agent)
    return store.ack(directory, cursor)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
