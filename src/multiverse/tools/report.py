"""Report agent phase and current work."""
from ..universe import agents
from ..universe import mailbox
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--phase", choices=("starting", "working", "blocked", "done"))
    parser.add_argument("--summary")


def run(root=None, *, agent=None, phase=None, summary=None):
    root, directory, launch_id = callback(root, agent)
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
    """`Reported working.`, and how many inbox-only messages wait when there are any."""
    record = run(root, agent=agent, phase=phase, summary=summary)
    line = f"Reported {record.get('phase') or 'unknown'}."
    count = unread(callback(root, agent)[1])
    if count:
        line += f" You have {count} unread inbox {'message' if count == 1 else 'messages'}."
    return line


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
