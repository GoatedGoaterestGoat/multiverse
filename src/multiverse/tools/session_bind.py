"""Record a native session UUID for exact resume."""
from ..universe import agents
from ._common import add_agent, callback
from ..runtime.agent_identity import actor
from ..harnesses.types import normalize_session_id
from ..store import lock, read
from .. import universe

def configure(parser):
    add_agent(parser)
    parser.add_argument("--session-id", required=True, help="Native harness session UUID for exact resume")
    parser.add_argument("--json", action="store_true", help="Print the agent's record as JSON")


def cli_run(root=None, *, agent=None, session_id, json=False):
    record = run(root, agent=agent, session_id=session_id)
    return record if json else f"Recorded {record.get('name') or 'the agent'}'s session."


def run(root=None, *, agent=None, session_id, json=False):  # `json`: presentation only
    caller = actor()
    if caller:
        root = universe.resolve(root)
        directory = agents.directory(root, agent)
        with lock(directory / '.launch.lock'):
            actor()
            data = read(directory / 'properties.json')
            if directory.resolve() != caller.directory.resolve() or normalize_session_id(session_id) != data.get('native_session_id'):
                raise ValueError('Agent identity can only verify its existing session binding; the runner owns binding changes')
            return data  # Cached bridge bootstrap: exact-existing verification, no writes.
    root, directory, launch_id = callback(root, agent)
    return agents.bind_session(root, agent, session_id, launch_id)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
