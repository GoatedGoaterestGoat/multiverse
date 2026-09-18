"""Acknowledge a previously read inbox byte cursor."""
from .. import agents, store
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--cursor", type=int, required=True)


def run(root=None, *, agent=None, cursor):
    root, directory, launch_id = callback(root, agent)
    # Serialize generation validation with launch replacement, then take the
    # inbox lock. A callback validated before resume must not ack after it.
    with store.lock(directory / ".launch.lock"):
        if launch_id and agents.runtime(directory).get("launch_id") != launch_id:
            raise ValueError("Outdated launch")
        return store.ack(directory, cursor)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
