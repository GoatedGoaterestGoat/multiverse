"""Report agent phase and current work."""
from ..universe import agents
from ..universe import mailbox
from ..store import lock
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--phase", choices=("starting", "working", "blocked", "done"))
    parser.add_argument("--summary")


def run(root=None, *, agent=None, phase=None, summary=None):
    root, directory, launch_id = callback(root, agent, agent_scoped=True)
    with lock(directory / '.launch.lock'):
        callback(root, agent, agent_scoped=True)
        return agents.report(root, agent, phase, summary)


def unread(directory):
    """Unread inbox-only messages: the count `mv2 inbox --peek` shows, taken in this process.

    Nothing is marked read, and an inbox that cannot be read never fails the report.
    """
    try:
        return len(mailbox.inbox(directory, limit=None, peek=True)['messages'])
    except (OSError, ValueError):
        return 0


def cli_run(root=None, *, agent=None, phase=None, summary=None):
    """`Reported working.`, and how many inbox-only messages wait when there are any.

    Done means the current task is finished, so what waits in the inbox is the agent's next work: the line says to
    read and address it before the turn ends.
    """
    record = run(root, agent=agent, phase=phase, summary=summary)
    line = f"Reported {record.get('phase') or 'unknown'}."
    count = unread(callback(root, agent, agent_scoped=True)[1])
    if count:
        noun = 'message' if count == 1 else 'messages'
        if record.get('phase') == 'done':
            line += (f" You have {count} unread inbox {noun}: read {'it' if count == 1 else 'them'} now with mv2 inbox "
                     f"and address {'it' if count == 1 else 'them'} before you finish.")
        else:
            line += f" You have {count} unread inbox {noun}."
    return line


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
