"""Save and deliver a message to the exact native session."""
import os
from pathlib import Path
import sys
from .. import agents, native, universe
from ..store import append_message

def configure(parser):
    parser.add_argument("agent")
    message = parser.add_mutually_exclusive_group(required=True)
    message.add_argument("--text")
    message.add_argument("--file", type=Path)
    message.add_argument("--stdin", action="store_true")
    parser.add_argument("--reply-to")
    parser.add_argument("--queue-only", action="store_true", help="Only append; existing inbox consumers may still read it")
    parser.add_argument("--delivery", choices=("steer", "follow-up"), default="steer",
                        help="Native timing: next safe boundary or the next turn")


def run(root=None, *, agent, text=None, file=None, stdin=False, reply_to=None, sender=None, delivery="steer", queue_only=False):
    if delivery not in ('steer', 'follow-up'):
        raise ValueError('Delivery must be steer or follow-up')
    root = universe.resolve(root)
    text = Path(file).read_text() if file is not None else sys.stdin.read() if stdin else text
    if sender is None:
        sender = {"agent_id": os.environ.get("MV2_AGENT_ID"), "universe": os.environ.get("MV2_UNIVERSE"),
                  "name": "agent" if os.environ.get("MV2_AGENT_ID") else "user"}
    directory = agents.directory(root, agent)
    result = append_message(directory, text, sender, reply_to, delivery)
    if queue_only:
        return {**result, "state": "queued", "saved": True,
                "reason": "No explicit native delivery attempt; existing inbox consumers may still read it"}
    try:
        receipt = native.deliver(root, directory.name, result['id'])
    except Exception as error:
        receipt = {"state": "error", "reason": str(error)}
    return {**result, "state": receipt['state'], "saved": True, "native_delivery": receipt,
            "ok": receipt['state'] not in ('error', 'unavailable')}



if __name__ == "__main__":
    from ._common import main
    main(configure, run)
