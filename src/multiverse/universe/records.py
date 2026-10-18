"""Reading an agent's records so that a damaged file is reported by name and reason, never raised through its neighbours.

An agent's directory holds `properties.json` (identity and what it reported) and `runtime.json` (the current
launch). Either can be damaged: truncated by a crash, emptied, replaced by something that is not a JSON object,
unreadable, or holding a value of the wrong type. `properties` and `runtime` read one file and raise a
RecordError, which says which file and why in one line, for the caller to report instead of crashing. They check
only what a reader depends on, not a schema: a record that merely lacks a field is still readable.
"""
from pathlib import Path
import json
import re

MAX_BYTES = 8 * 1024 * 1024  # a record is a few kilobytes; a file this large is not one

# Fields the dashboard and listings show as text. When one is present it is a string. The first three identify
# the agent, so they are never null; the others may be, and a listing shows a null as empty text.
IDENTITY_FIELDS = ("id", "name", "harness")
TEXT_FIELDS = (*IDENTITY_FIELDS, "phase", "summary", "task")


class RecordError(ValueError):
    """An agent record that cannot be used: `.file` is its path, `.reason` one line, `.data` what was parsed (or None)."""

    def __init__(self, file, reason, data=None):
        self.file, self.reason, self.data = Path(file), reason, data
        super().__init__(f"{self.file.name}: {reason}")


def _kind(value):
    return {dict: "an object", list: "a list", str: "a string", bool: "a boolean", int: "a number",
            float: "a number", type(None): "null"}.get(type(value), type(value).__name__)


def _object(path):
    """The JSON object in `path`; RecordError (never another exception) for anything else."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_BYTES:
            raise RecordError(path, f"larger than {MAX_BYTES // (1024 * 1024)} MiB")
        raw = path.read_bytes()
    except RecordError:
        raise
    except OSError as error:
        raise RecordError(path, f"cannot be read: {error.strerror or type(error).__name__}") from None
    if not raw.strip():
        raise RecordError(path, "is empty")
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise RecordError(path, "is not text") from None
    except RecursionError:
        raise RecordError(path, "is nested too deeply") from None
    except ValueError as error:
        raise RecordError(path, f"is not valid JSON ({error})") from None
    if not isinstance(data, dict):
        raise RecordError(path, f"must be a JSON object, not {_kind(data)}")
    return data


def properties(path):
    """An agent's `properties.json` as a dict; RecordError when it cannot be used."""
    data = _object(path)
    for key in TEXT_FIELDS:
        if key in data and not isinstance(data[key], str) and (data[key] is not None or key in IDENTITY_FIELDS):
            raise RecordError(path, f"{key} must be a string, not {_kind(data[key])}", data)
    return data


def runtime(path):
    """An agent's `runtime.json` as a dict, {} when the agent never launched; RecordError when it cannot be used."""
    if not Path(path).exists():
        return {}
    return _object(path)


def mentions(path, value):
    """Bounded raw evidence scan; absence is useful only when the file was readable."""
    needle, tail, scanned = value.encode().lower(), b'', 0
    with Path(path).open('rb') as stream:
        while chunk := stream.read(65536):
            scanned += len(chunk)
            if scanned > MAX_BYTES:
                raise ValueError('file is too large to inspect safely')
            combined = tail + chunk.lower()
            # A truncated JSON string can still reserve an ASCII name/UUID
            # through Unicode escapes. Decode conservatively outside strings
            # too: a false positive refuses; it must never release a claim.
            decoded = re.sub(rb'\\u00([0-9a-f]{2})',
                             lambda match: bytes([int(match[1], 16)]), combined).lower()
            if needle in decoded:
                return True
            tail = combined[-6 * len(needle):]
    return False
