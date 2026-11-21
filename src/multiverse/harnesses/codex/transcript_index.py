"""An optional, bounded metadata index for one rollout in an agent folder."""
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from uuid import uuid4

NAME = 'codex-transcript-index.jsonl'
SCHEMA = 1  # Bump for changes to the format or how the reader derives indexed metadata.
MAX_BYTES = 64 << 20
MAX_LINE = 1 << 20
MAX_FIELD = 4096
MAX_ROWS = 200_000


class InvalidIndex(ValueError):
    pass


def _json(line):
    try:
        return json.loads(line)
    except (ValueError, RecursionError) as error:
        raise InvalidIndex('Invalid index JSON') from error


def location(session):
    if session.session_file:
        path = Path(session.session_file)
        if path.is_absolute() and path.name == 'session.jsonl':
            return path.parent
    return None


def _directory(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def load(directory, path, decode_entry, check):
    """Read a whole cache from a regular file, within the opened descriptor's bound."""
    try:
        parent = _directory(directory)
        try:
            fd = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, 'rb') as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_BYTES:
                return ()
            available = status.st_size

            def read_line():
                nonlocal available
                check()
                line = stream.readline(min(available, MAX_LINE + 1))
                if len(line) > MAX_LINE or not line.endswith(b'\n'):
                    raise InvalidIndex('Incomplete index row')
                available -= len(line)
                return line

            header = read_line()
            values = _json(header)
            if not isinstance(values, list) or len(values) != 3:
                return ()
            schema, source, count = values
            if (type(schema) is not int or schema != SCHEMA or source != str(path)
                    or type(count) is not int or not 0 < count <= MAX_ROWS):
                return ()
            digest = hashlib.sha256(header)
            result = []
            for _ in range(count):
                line = read_line()
                digest.update(line)
                result.append(decode_entry(_json(line), result[-1] if result else None))
            if _json(read_line()) == digest.hexdigest() and not available and not stream.read(1):
                check()
                return tuple(result)
    except (OSError, InvalidIndex):
        pass
    return ()


def save(directory, path, entries, encode_entry, *, deadline):
    """Replace the optional index atomically, with no writes through a link."""
    if not 0 < len(entries) <= MAX_ROWS or time.monotonic() >= deadline:
        return
    try:
        parent = _directory(directory)
    except OSError:
        return
    temporary = f'.codex-index-{uuid4().hex}'
    owned = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        owned = True
        with os.fdopen(fd, 'wb') as stream:
            remaining = MAX_BYTES
            digest = hashlib.sha256()

            def put(value):
                nonlocal remaining
                if time.monotonic() >= deadline:
                    raise InvalidIndex('Index write budget expired')
                line = (json.dumps(value, separators=(',', ':')) + '\n').encode()
                if len(line) > min(remaining, MAX_LINE):
                    raise InvalidIndex('Index exceeds its byte budget')
                stream.write(line)
                remaining -= len(line)
                digest.update(line)

            put([SCHEMA, str(path), len(entries)])
            for item in entries:
                put(encode_entry(item))
            put(digest.hexdigest())
        if time.monotonic() >= deadline:
            return
        os.replace(temporary, NAME, src_dir_fd=parent, dst_dir_fd=parent)
        owned = False
    except (OSError, InvalidIndex):
        pass
    finally:
        try:
            if owned:
                os.unlink(temporary, dir_fd=parent)
        except OSError:
            pass
        finally:
            os.close(parent)
