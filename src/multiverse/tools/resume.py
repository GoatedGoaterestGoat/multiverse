"""Resume an agent using its recorded native session."""
from .. import agents, universe
from ._common import add_wait
from .spawn import cause, outcome

def configure(parser):
    parser.add_argument("agent")
    parser.add_argument("--model")
    add_wait(parser)
    parser.add_argument("--json", action="store_true", help="Print the full launch record as JSON")


def run(root=None, *, agent, model=None, wait=False, wait_timeout=60, json=False):  # `json`: presentation only
    return agents.start(universe.resolve(root), resume=agent, model=model, wait=wait, wait_timeout=wait_timeout)


def cli_run(root=None, *, agent, model=None, wait=False, wait_timeout=60, json=False):
    try:
        record = run(root, agent=agent, model=model, wait=wait, wait_timeout=wait_timeout)
    except RuntimeError as error:
        if json:
            raise
        raise cause(error, agent, "did not resume") from error
    return record if json else outcome(record, "Resumed", "did not resume")


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
