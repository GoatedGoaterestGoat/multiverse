"""Resume an agent using its recorded native session."""
from .. import agents, universe

def configure(parser):
    parser.add_argument("agent")
    parser.add_argument("--model")


def run(root=None, *, agent, model=None):
    return agents.start(universe.resolve(root), resume=agent, model=model)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
