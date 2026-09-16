"""Local file protocol. Unix only, like tmux. No background service."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid.uuid4())


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    """Readers see the old or new complete document, never a partial write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def append_message(agent_dir, text, sender, reply_to=None):
    if not text.strip():
        raise ValueError("Message cannot be empty")
    message = {"id": uid(), "at": now(), "from": sender, "text": text}
    if reply_to:
        message["reply_to"] = reply_to
    agent_dir = Path(agent_dir)
    with lock(agent_dir / ".inbox.lock"):
        path = agent_dir / "inbox.jsonl"
        with path.open("ab+") as out:
            if out.tell():
                out.seek(-1, os.SEEK_END)
                if out.read(1) != b"\n":
                    raise ValueError(f"Incomplete inbox tail; repair before appending: {path}")
            out.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            out.flush()
            os.fsync(out.fileno())
    return message


def _cursor(agent_dir):
    path = agent_dir / "inbox.cursor"
    return int(path.read_text()) if path.exists() else 0


def _validate_cursor(data, cursor):
    if cursor < 0 or cursor > len(data) or (cursor and data[cursor - 1:cursor] != b"\n"):
        raise ValueError("Invalid inbox cursor; refusing to skip messages")


def inbox(agent_dir, limit=50):
    agent_dir = Path(agent_dir)
    with lock(agent_dir / ".inbox.lock"):
        path = agent_dir / "inbox.jsonl"
        data = path.read_bytes() if path.exists() else b""
        cursor = _cursor(agent_dir)
        _validate_cursor(data, cursor)
        messages = []
        for line in data[cursor:].splitlines(keepends=True):
            if not line.endswith(b"\n") or len(messages) >= limit:
                break
            messages.append(json.loads(line))
            cursor += len(line)
        return {"messages": messages, "cursor": cursor}


def ack(agent_dir, cursor):
    agent_dir = Path(agent_dir)
    with lock(agent_dir / ".inbox.lock"):
        data = (agent_dir / "inbox.jsonl").read_bytes()
        _validate_cursor(data, cursor)
        current = _cursor(agent_dir)
        _validate_cursor(data, current)
        if cursor < current:
            raise ValueError("Cursor cannot move backwards")
        write(agent_dir / "inbox.cursor", cursor)
    return {"cursor": cursor}
