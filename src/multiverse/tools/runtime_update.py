"""Report native harness status for the current launch."""
from .. import agents
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    # --activity is the former name.
    parser.add_argument("--status", "--activity", dest="status", choices=("idle", "working", "waiting", "unknown"),
                        required=True)

    parser.add_argument("--bridge", choices=("pi-inbox-v1", "pi-socket-v1", "claude-mod-v1", "none"))

def run(root=None, *, agent=None, status, bridge=None):
    root, directory, launch_id = callback(root, agent)
    changes = {"status": status}
    if bridge:
        changes["native_bridge"] = None if bridge == "none" else bridge
    return agents.update_runtime(directory, launch_id, **changes)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
