"""Inter-agent inbox + ask-record data layer.

Two on-disk surfaces:

  ~/.multiverse/asks/<ask-id>.json        — persistent ask metadata. Created
                                             by ask_agent, consulted by
                                             respond_to_agent for routing,
                                             deleted after a successful
                                             response (or kept around for
                                             timed-out asks).

  ~/.multiverse/inbox/<encoded-cwd>/<recipient-name>/<ask-id>.json
                                          — pending message file. Written
                                             by ask_agent (questions) and
                                             respond_to_agent (responses).
                                             Consumed (deleted) by the
                                             deliver-inbox hook after a
                                             single delivery as system-
                                             reminder content.

Files are written via tmp+rename so the deliver-inbox hook never sees a
partial file mid-write.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


MULTIVERSE_DIR = Path(os.path.expanduser("~/.multiverse"))
INBOX_DIR = MULTIVERSE_DIR / "inbox"
ASKS_DIR = MULTIVERSE_DIR / "asks"


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def agent_inbox_dir(cwd: str, name: str) -> Path:
    return INBOX_DIR / _encode_cwd(cwd) / name


def new_ask_id() -> str:
    return str(uuid.uuid4())


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.rename(path)


@dataclass
class AskRecord:
    """Persistent ask metadata. Used by respond_to_agent for routing."""

    ask_id: str
    from_name: str
    from_cwd: str
    to_name: str
    to_cwd: str
    question: str
    ts: float
    wait_for_response: bool = False


def save_ask(record: AskRecord) -> None:
    path = ASKS_DIR / f"{record.ask_id}.json"
    _atomic_write_json(path, asdict(record))


def load_ask(ask_id: str) -> Optional[AskRecord]:
    path = ASKS_DIR / f"{ask_id}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return AskRecord(**data)
    except TypeError:
        return None


def delete_ask(ask_id: str) -> None:
    path = ASKS_DIR / f"{ask_id}.json"
    try:
        path.unlink()
    except OSError:
        pass


def write_question_to_inbox(record: AskRecord) -> Path:
    """Drop the question into the recipient's inbox."""
    inbox_file = agent_inbox_dir(record.to_cwd, record.to_name) / f"{record.ask_id}.json"
    _atomic_write_json(
        inbox_file,
        {
            "ask_id": record.ask_id,
            "kind": "question",
            "from": record.from_name,
            "body": record.question,
            "ts": record.ts,
        },
    )
    return inbox_file


def write_response_to_inbox(
    asker_name: str,
    asker_cwd: str,
    responder_name: str,
    ask_id: str,
    original_question: str,
    response_body: str,
) -> Path:
    """Drop a response into the original asker's inbox."""
    inbox_file = agent_inbox_dir(asker_cwd, asker_name) / f"{ask_id}.json"
    _atomic_write_json(
        inbox_file,
        {
            "ask_id": ask_id,
            "kind": "response",
            "from": responder_name,
            "in_reply_to_question": original_question,
            "body": response_body,
            "ts": time.time(),
        },
    )
    return inbox_file


def list_pending_messages(cwd: str, name: str) -> list[dict]:
    """List pending inbox messages for an agent, sorted by ts ascending.

    Each dict is the message JSON augmented with ``__path`` (string) so
    callers can pass it to ``consume_messages``.
    """
    d = agent_inbox_dir(cwd, name)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for path in d.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["__path"] = str(path)
        out.append(data)
    out.sort(key=lambda d: d.get("ts", 0))
    return out


def consume_messages(messages: list[dict]) -> None:
    """Delete the underlying files of the given pending messages."""
    for m in messages:
        path = m.get("__path")
        if not path:
            continue
        try:
            Path(path).unlink()
        except OSError:
            pass


def pending_inbox_count(cwd: str, name: str) -> int:
    """Cheap count for TUI display. Doesn't parse files."""
    d = agent_inbox_dir(cwd, name)
    if not d.is_dir():
        return 0
    try:
        return sum(1 for _ in d.glob("*.json"))
    except OSError:
        return 0


def find_response_file(asker_cwd: str, asker_name: str, ask_id: str) -> Optional[Path]:
    """Used by blocking ask_agent to poll for a response."""
    path = agent_inbox_dir(asker_cwd, asker_name) / f"{ask_id}.json"
    return path if path.exists() else None
