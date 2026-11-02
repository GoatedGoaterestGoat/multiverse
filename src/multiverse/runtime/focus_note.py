"""Tell an agent whose focus ended with a turn about the messages that focus held: one follow-up from mv2.

universe.focus.after_turn starts this in its own process once the turn end has
removed the focus. A listener that runs then reports the messages itself, so the
note first waits for its claim; a listener killed meanwhile releases its lock, and
the note goes as usual. Only one of the listener and the note takes the report,
and a focus that began since makes both stand down.
"""
from pathlib import Path
import sys
import time

from ..universe import focus, mailbox

# Shown as `Message from mv2:`; not an agent and not the person.
SENDER = {'name': 'mv2', 'agent_id': None, 'core': True}
POLL = 0.1


def run(directory, focus_id, *, wait=focus.LISTENER_WAIT):
    """Send the note unless a listener claimed the report or nothing held is unread; the send's result, or None.

    The send saves the note, then commits the report to it, under the focus lock: a send that fails before
    saving, or a process killed before it, leaves the report unclaimed.
    """
    directory = Path(directory)
    deadline = time.monotonic() + wait
    while (focus.listener(directory) and not (focus.last(directory) or {}).get('report')
           and time.monotonic() < deadline):
        time.sleep(POLL)
    count = len(mailbox.held(directory, focus_id, peek=True)['messages'])
    if not count or (focus.last(directory) or {}).get('report'):
        return None
    from ..tools import send
    return send.run(directory.parents[2], agent=directory.name, text=focus.note(count), sender=SENDER,
                    delivery='follow-up', _note_for=focus_id)


if __name__ == '__main__':
    try:
        run(sys.argv[1], sys.argv[2])
    except Exception:
        sys.exit(1)  # Nothing to tell: the held messages stay in the inbox, and mv2 held still reads them.
