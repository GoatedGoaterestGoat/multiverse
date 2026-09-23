"""Resume an agent using its recorded native session."""
from .. import agents, universe

def configure(parser):
    parser.add_argument("agent")
    parser.add_argument("--model")
    parser.add_argument("--start-empty", action="store_true", help=
                        "Start empty conversation: assign a new Claude UUID; retain agent identity and old history")


def run(root=None, *, agent, model=None, start_empty=False):
    return agents.start(universe.resolve(root), resume=agent, model=model, start_empty=start_empty)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
