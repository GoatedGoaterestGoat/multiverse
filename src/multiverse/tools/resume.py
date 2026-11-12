"""Resume an agent, or explicitly recover its missing native conversation."""
from ..runtime import lifecycle
from .. import universe
from ..universe import agents
from ._common import add_wait
from ..display import cause, outcome

def configure(parser):
    parser.add_argument("agent")
    parser.add_argument("--model", help="Allowed model ID or alias; overrides the recorded choice")
    parser.add_argument("--effort", help="Allowed effort for this agent, retained on later resumes; default clears it when permitted")
    parser.add_argument('--start-fresh', action='store_true',
                        help='Recover a missing conversation with a new one, only after every candidate storage proves it absent')
    add_wait(parser)
    parser.add_argument("--json", action="store_true", help="Print the full launch record as JSON")


def run(root=None, *, agent, model=None, effort=None, wait=False, wait_timeout=60, json=False, start_fresh=False):  # `json`: presentation only
    return lifecycle.start(universe.resolve(root), resume=agent, model=model, wait=wait, wait_timeout=wait_timeout,
                           start_fresh=start_fresh, effort=effort)


def cli_run(root=None, *, agent, model=None, effort=None, wait=False, wait_timeout=60, json=False, start_fresh=False):
    try:
        record = run(root, agent=agent, model=model, effort=effort, wait=wait, wait_timeout=wait_timeout, start_fresh=start_fresh)
    except agents.AgentNotFound as error:
        if not json:
            raise
        from ._launch import failure_record
        return failure_record(root, agent, error)
    except (RuntimeError, ValueError) as error:
        if json:
            from ._launch import failure_record
            return failure_record(root, agent, error)
        raise cause(error, agent, "did not resume") from error
    return record if json else outcome(record, "Resumed", "did not resume")


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
