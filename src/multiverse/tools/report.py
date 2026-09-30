"""Report agent phase and current work."""
from .. import agents
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--phase", choices=("starting", "working", "blocked", "done"))
    parser.add_argument("--summary")


def run(root=None, *, agent=None, phase=None, summary=None):
    root, directory, launch_id = callback(root, agent)
    return agents.report(root, agent, phase, summary)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
