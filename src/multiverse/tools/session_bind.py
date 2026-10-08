"""Record a native session UUID for exact resume."""
from ..universe import agents
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--session-id", required=True, help="Native harness session UUID for exact resume")


def run(root=None, *, agent=None, session_id):
    root, directory, launch_id = callback(root, agent)
    return agents.bind_session(root, agent, session_id, launch_id)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
