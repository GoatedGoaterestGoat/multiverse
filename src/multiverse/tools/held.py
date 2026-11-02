"""Read the steer and follow-up messages held during your last focus, and mark only those read."""
import json as json_module

from .. import universe
from ..plugins import access
from ..runtime.agent_identity import sender_actor
from ..store import existing_lock
from ..universe import focus, mailbox
from .inbox import FIELDS, readable

EMPTY = 'No unread messages held during your last focus.'


def configure(parser):
    parser.add_argument('--json', action='store_true', help='Return machine-readable JSON')


def run(root=None, *, json=False, _present=None):
    """Only the unread messages the latest focus held, however it ended; other unread messages stay unread."""
    access.require('act', 'mark held messages read')
    caller = sender_actor(acting='read held messages')
    if caller is None:
        raise ValueError('Run held inside an agent session')
    if root is not None and universe.resolve(root).resolve() != caller.root.resolve():
        raise ValueError('An agent can read only its own held messages')
    directory = caller.directory
    def present(result):
        if _present:
            _present(caller.root, public(result))
    # The focus lock too: no focus begins between the check and marking the messages read.
    with existing_lock(directory / '.launch.lock'), focus.locked(directory):
        sender_actor(acting='read held messages')
        if focus.current(directory):
            raise ValueError('You are focused: run mv2 held after your focus ends')
        latest = focus.last(directory)
        result = mailbox.held(directory, latest['id'], present=present) if latest else {'messages': []}
        if not latest:
            present(result)
    return public(result)


def public(result):
    return {'messages': [{key: message[key] for key in FIELDS if key in message} for message in result['messages']]}


def cli_run(root=None, *, json=False):
    def present(root, result):
        # Printed while the inbox is locked: output that fails leaves the messages unread.
        print(json_module.dumps(result, indent=2, ensure_ascii=False) if json else readable(root, result, EMPTY),
              flush=True)
    run(root, json=json, _present=present)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
