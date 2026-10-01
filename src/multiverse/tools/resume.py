"""Resume an agent using its recorded native session."""
from .. import agents, universe
from ._common import add_wait

def configure(parser):
    parser.add_argument("agent")
    parser.add_argument("--model")
    add_wait(parser)


def run(root=None, *, agent, model=None, wait=False, wait_timeout=60):
    return agents.start(universe.resolve(root), resume=agent, model=model, wait=wait, wait_timeout=wait_timeout)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
