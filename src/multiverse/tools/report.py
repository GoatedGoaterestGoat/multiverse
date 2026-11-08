"""Report agent phase and current work."""
import os

from ..universe import agents, blocked
from ..universe import mailbox
from ..store import lock
from .. import display
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--phase", choices=("starting", "working", "blocked", "done"))
    parser.add_argument("--summary")
    parser.add_argument("--blocked-on", help="With --phase blocked: agent name/ID, user, or self; agent:NAME disambiguates")
    parser.add_argument("--blocked-on-universe", help="Universe or subverse of the blocked-on agent (otherwise this universe)")


def run(root=None, *, agent=None, phase=None, summary=None, blocked_on=None, blocked_on_universe=None):
    if blocked_on is not None and phase != 'blocked':
        raise ValueError('--blocked-on requires --phase blocked')
    if blocked_on_universe is not None and blocked_on is None:
        raise ValueError('--blocked-on-universe requires --blocked-on')
    root, directory, launch_id = callback(root, agent, agent_scoped=True)
    dependency = blocked.select(root, directory, blocked_on, blocked_on_universe) if blocked_on is not None else None
    # Serialize the phase/dependency change with send's upgrade decision. Same order as focus and send.
    with lock(directory / '.send.lock', create_parent=False), lock(directory / '.launch.lock', create_parent=False):
        callback(root, agent, agent_scoped=True)
        return agents.report(root, agent, phase, summary, blocked_on=dependency)


def unread(directory):
    """Unread inbox-only messages: the count `mv2 inbox --peek` shows, taken in this process.

    Nothing is marked read, and an inbox that cannot be read never fails the report.
    """
    try:
        return len(mailbox.inbox(directory, limit=None, peek=True, blocking=False)['messages'])
    except (OSError, ValueError):
        return 0


def cli_run(root=None, *, agent=None, phase=None, summary=None, blocked_on=None, blocked_on_universe=None):
    """Report the phase, with a general unread count only when the phase is done."""
    record = run(root, agent=agent, phase=phase, summary=summary, blocked_on=blocked_on,
                 blocked_on_universe=blocked_on_universe)
    directory = callback(root, agent, agent_scoped=True)[1]
    dependency = blocked.current(record)
    target = blocked.display(directory.parent.parent.parent, record) if dependency else None
    label = display.blocked_label(target)
    line = f"Reported blocked on {label}." if label else f"Reported {record.get('phase') or 'unknown'}."
    if record.get('phase') == 'done':
        count = unread(directory)
        if count:
            noun = 'message' if count == 1 else 'messages'
            line += f" You have {count} unread inbox {noun}."
    if (os.environ.get('MV2_AGENT_ID') and dependency and dependency['kind'] == 'agent'
            and dependency['id'] in mailbox.unread_senders(directory)):
        line += f" Reminder: you have unread messages from {display.clean(target['name'])}."
    return line


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
