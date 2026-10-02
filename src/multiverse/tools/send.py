"""Save and deliver a message to the exact native session."""
import os
from pathlib import Path
import sys
from .. import agents, native, universe
from ..store import append_message, read, lock, message_record, update_message
from ._common import delivery_summary

def configure(parser):
    parser.add_argument("agent")
    message = parser.add_mutually_exclusive_group(required=True)
    message.add_argument("--text")
    message.add_argument("--file", type=Path)
    message.add_argument("--stdin", action="store_true")
    parser.add_argument("--queue-only", action="store_true", help="Save for manual inbox reading; do not submit natively")
    parser.add_argument("--delivery", choices=("steer", "follow-up", "inbox-only"), default="steer",
                        help="Native timing: next safe boundary or the next turn")


def run(root=None, *, agent, text=None, file=None, stdin=False, sender=None, delivery="steer", queue_only=False):
    if delivery not in ('steer', 'follow-up', 'inbox-only'):
        raise ValueError('Delivery must be steer, follow-up, or inbox-only')
    root = universe.resolve(root)
    text = Path(file).read_text() if file is not None else sys.stdin.read() if stdin else text
    if sender is None:
        sender = {"agent_id": os.environ.get("MV2_AGENT_ID"), "universe": os.environ.get("MV2_UNIVERSE"),
                  "name": "agent" if os.environ.get("MV2_AGENT_ID") else "user"}
    directory = agents.directory(root, agent)
    with lock(directory / '.send.lock'):
        if queue_only:
            delivery = 'inbox-only'
        with lock(directory / '.launch.lock'):
            target = read(directory / 'properties.json').get('native_session_id')
            message = append_message(directory, text, sender, delivery, target_session=target)
        result = {key: message[key] for key in ('at', 'from', 'text', 'delivery')}
        if delivery == 'inbox-only':
            return {**result, "state": "queued", "saved": True,
                    "reason": "Saved for explicit inbox reading"}
        try:
            receipt = native.send_or_wake(root, directory.name, message['id'])
        except Exception as error:
            receipt = {"state": "error", "reason": str(error)}
            _, tracking = message_record(directory, message['id'])
            if tracking.get('submission_state', 'pending') == 'pending':
                update_message(directory, message['id'], submission_state='failed',
                               attempts=[*tracking.get('attempts', []), receipt])
        return {**result, "state": receipt['state'], "saved": True, "native_delivery": delivery_summary(receipt),
                "ok": receipt['state'] not in ('error', 'unavailable', 'unknown')}



if __name__ == "__main__":
    from ._common import main
    main(configure, run)
