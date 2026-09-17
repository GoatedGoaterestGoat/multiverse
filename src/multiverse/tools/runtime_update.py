"""Report native harness activity for the current launch."""
from .. import agents
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--activity", choices=("idle", "working", "unknown"), required=True)

    parser.add_argument("--bridge", choices=("pi-inbox-v1", "none"))

def run(root=None, *, agent=None, activity, bridge=None):
    root, directory, launch_id = callback(root, agent)
    changes = {"activity": activity}
    if bridge:
        changes["native_bridge"] = None if bridge == "none" else bridge
    return agents.update_runtime(directory, launch_id, **changes)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
