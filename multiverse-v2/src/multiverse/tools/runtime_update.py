"""Report native harness activity for the current launch."""
from .. import agents
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--activity", choices=("idle", "working", "unknown"), required=True)


def run(root=None, *, agent=None, activity):
    root, directory, launch_id = callback(root, agent)
    return agents.update_runtime(directory, launch_id, activity=activity)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
