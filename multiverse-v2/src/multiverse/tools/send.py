"""Append a durable message; delivery is adapter-specific."""
import os
from pathlib import Path
import sys
from .. import agents, universe
from ..store import append_message

def configure(parser):
    parser.add_argument("agent")
    message = parser.add_mutually_exclusive_group(required=True)
    message.add_argument("--text")
    message.add_argument("--file", type=Path)
    message.add_argument("--stdin", action="store_true")
    parser.add_argument("--reply-to")


def run(root=None, *, agent, text=None, file=None, stdin=False, reply_to=None, sender=None):
    root = universe.resolve(root)
    text = Path(file).read_text() if file is not None else sys.stdin.read() if stdin else text
    if sender is None:
        sender = {"agent_id": os.environ.get("MV2_AGENT_ID"), "universe": os.environ.get("MV2_UNIVERSE"),
                  "name": "agent" if os.environ.get("MV2_AGENT_ID") else "user"}
    result = append_message(agents.directory(root, agent), text, sender, reply_to)
    return {**result, "state": "queued"}


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
