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


def append_message(agent_dir, text, sender, delivery="inbox-only", *, target_session=None):
    if not text.strip():
        raise ValueError("Message cannot be empty")
    if delivery not in ("steer", "follow-up", "inbox-only"):
        raise ValueError("Unknown native delivery mode")
    message = {"schema": 2, "target_session": target_session, "delivery": delivery, "id": uid(), "at": now(), "from": sender, "text": text}
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


def _messages(agent_dir):
    path = agent_dir / 'inbox.jsonl'
    data = path.read_bytes() if path.exists() else b''
    cursor = _cursor(agent_dir)
    _validate_cursor(data, cursor)
    offset = 0
    messages = []
    for line in data.splitlines(keepends=True):
        if not line.endswith(b'\n'):
            break
        offset += len(line)
        messages.append((offset, json.loads(line)))
    return data, cursor, messages


def _states(agent_dir):
    path = agent_dir / 'inbox-state.json'
    states = read(path) if path.exists() else {}
    # Retired per-message read markers have no effect on delivery or read position.
    states = {identifier: {key: value for key, value in state.items() if key != 'acknowledged_at'}
              for identifier, state in states.items()}
    states = {identifier: state for identifier, state in states.items() if state}
    legacy = agent_dir / 'native-delivery.json'
    if legacy.exists():
        for identifier, attempts in read(legacy).items():
            if identifier not in states and attempts:
                last = attempts[-1]
                states[identifier] = {'attempts': attempts, 'target_session': last.get('native_session_id'),
                    'submission_state': {'native_queued': 'accepted', 'error': 'unknown'}.get(last['state'], 'failed')}
    return states


def _manual(message):
    # Existing logs retain their old mailbox semantics; no live migration.
    return message.get('schema') != 2 or message['delivery'] == 'inbox-only'


def inbox(agent_dir, limit=50, *, after=None, all_messages=False, peek=False):
    """Read unread manual messages; peek and full history do not consume them."""
    if peek and (all_messages or after is not None):
        raise ValueError('--peek cannot be combined with --all or --after')
    if limit is not None and limit <= 0:
        raise ValueError('Inbox limit must be positive')
    agent_dir = Path(agent_dir)
    with lock(agent_dir / '.inbox.lock'):
        data, read_cursor, records = _messages(agent_dir)
        states = _states(agent_dir)
        if after is not None:
            _validate_cursor(data, after)
            if not all_messages and after > read_cursor:
                raise ValueError('Ordinary inbox reads advance automatically; use --all to browse ahead')
        start = (after or 0) if all_messages else read_cursor
        cursor = start
        messages, cursors = [], []
        for offset, message in records:
            if offset <= start:
                continue
            if limit is not None and len(messages) >= limit:
                break
            cursor = offset
            state = states.get(message['id'], {})
            if all_messages:
                messages.append({**message, 'tracking': {'submission_state': 'manual' if _manual(message) else 'pending', **state}})
            elif _manual(message):
                messages.append(message)
            else:
                continue
            cursors.append(offset)
        if not all_messages and not peek and cursor > read_cursor:
            write(agent_dir / 'inbox.cursor', cursor)
        result = {'messages': messages, 'cursor': cursor}
        if after is not None:
            result.update(start_cursor=start, read_cursor=read_cursor, message_cursors=cursors,
                          has_more=b'\n' in data[cursor:])
        return result


def message_record(agent_dir, message_id):
    agent_dir = Path(agent_dir)
    with lock(agent_dir / '.inbox.lock'):
        _, _, records = _messages(agent_dir)
        message = next((m for _, m in records if m['id'] == message_id), None)
        if message is None:
            raise ValueError('Message does not belong to this recipient inbox')
        return message, _states(agent_dir).get(message_id, {})


def update_message(agent_dir, message_id, **changes):
    """Merge delivery evidence under the recipient inbox lock."""
    agent_dir = Path(agent_dir)
    with lock(agent_dir / '.inbox.lock'):
        _, _, records = _messages(agent_dir)
        if not any(m['id'] == message_id for _, m in records):
            raise ValueError('Message does not belong to this recipient inbox')
        states = _states(agent_dir)
        states[message_id] = {**states.get(message_id, {}), **changes}
        write(agent_dir / 'inbox-state.json', states)
        return states[message_id]
