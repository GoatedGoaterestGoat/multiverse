"""Bounded, observation-only rollout state; transcript row indexes are independent."""
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import stat
from threading import RLock

from ..types import normalize_session_id
from . import transcripts
from .rollout import MAX_ROWS, open_rollout, preferred
from .storage import HEADER_BYTES, MAX_ENTRIES

CACHE_LIMIT = 64
TAIL_BYTES = 4096
_states = OrderedDict()
_copies = OrderedDict()
_lock = RLock()


class Unstable(ValueError):
    """A changed scan must not become a reusable negative observation."""


def remember(cache, key, value):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > CACHE_LIMIT:
        cache.popitem(last=False)


def paths(home, native_id):
    """Metadata-only equivalent of transcript discovery; headers are scanned once."""
    uuid = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
    name = re.compile(r'rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-' + re.escape(native_id)
                      + r'(?:_' + uuid + r')?\.jsonl(?:\.zst)?')
    roots = [Path(home) / part for part in ('sessions', 'archived_sessions')]
    pending, found, scanned = list(roots), {}, 0
    while pending:
        directory = pending.pop()
        try:
            if directory.is_symlink():
                raise ValueError('Linked rollout directory')
            with os.scandir(directory) as entries:
                for item in entries:
                    scanned += 1
                    if scanned > MAX_ENTRIES:
                        raise ValueError('Rollout discovery limit')
                    path = Path(item.path)
                    mode = path.lstat().st_mode
                    if name.fullmatch(item.name):
                        path = preferred(path)
                        status = path.lstat()
                        if not stat.S_ISREG(status.st_mode):
                            raise ValueError('Nonregular rollout')
                        found[path] = transcripts.signature(status)
                    elif item.name.startswith('rollout-') and native_id in item.name:
                        raise ValueError('Unsupported exact-thread rollout name')
                    elif stat.S_ISLNK(mode):
                        raise ValueError('Linked rollout entry')
                    elif stat.S_ISDIR(mode):
                        pending.append(path)
        except FileNotFoundError:
            if directory not in roots:
                raise
    return sorted(found.items(), key=lambda item: (item[1][3], str(item[0])))


@dataclass
class State:
    native_id: str
    stamp: tuple
    offset: int = 0
    rows: int = 0
    digest: object = field(default_factory=hashlib.sha256)
    tail_size: int = 0
    tail_digest: bytes = b''
    valid: bool = True
    review: bool = False
    latest: tuple | None = None

    def row(self, line):
        value = transcripts.mapping(transcripts.row_value(line))
        payload = transcripts.mapping(value.get('payload'))
        kind = transcripts.string(value.get('type')) or 'unreadable'
        if not self.rows and (kind != 'session_meta' or len(line) > HEADER_BYTES):
            self.valid = False
        if kind == 'session_meta':
            try:
                if normalize_session_id(payload.get('id')) != self.native_id:
                    self.valid = False
                if 'session_id' in payload:
                    normalize_session_id(payload['session_id'])
            except (TypeError, ValueError):
                self.valid = False
            if (payload.get('source') not in (None, 'cli', 'vscode', 'exec', 'mcp')
                    or payload.get('thread_source') not in (None, 'user')
                    or payload.get('history_base') is not None):
                self.valid = False
        elif kind == 'event_msg':
            inner = payload.get('type')
            item = transcripts.mapping(payload.get('item'))
            item_type = item.get('type') if inner in ('item_started', 'item_completed') else None
            if inner == 'entered_review_mode' or item_type == 'EnteredReviewMode':
                self.review = True
            elif inner == 'exited_review_mode' or item_type == 'ExitedReviewMode':
                self.review = False
        elif kind == 'turn_context' and not transcripts.string(payload.get('type')) and not self.review:
            model, effort = transcripts.turn_settings(payload)
            at = transcripts.string(value.get('timestamp'))
            self.latest = (model, effort, at if at and len(at) <= 128 else None)
        elif kind == 'unreadable' and not transcripts.string(payload.get('type')) and not self.review:
            self.latest = None
        self.rows += 1

    def value(self):
        if not self.valid or not self.rows or self.latest is None:
            return {}
        model, effort, at = self.latest
        if model is None or not at or len(at) > 128:
            return {}
        timestamp = datetime.fromisoformat(at)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return {}
        return {'model': model, 'effort': effort, 'observed_at': at}


def prefix_intact(handle, old):
    """Reprove every consumed byte before reusing identity, review or latest state."""
    handle.seek(0)
    digest, remaining = hashlib.sha256(), old.offset
    while remaining:
        chunk = handle.read(min(65536, remaining))
        if not chunk:
            return False
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.digest() == old.digest.digest()


def scan(path, expected, native_id):
    old = _states.get(path)
    if old is not None and old.native_id == native_id and old.stamp == expected:
        _states.move_to_end(path)
        return old
    with open_rollout(path) as handle:
        if transcripts.signature(os.fstat(handle.fileno())) != expected:
            raise Unstable('Rollout changed before observation')
        compressed = getattr(handle, 'compressed', False)
        append = (not compressed and old is not None and old.native_id == native_id
                  and old.stamp[:2] == expected[:2] and expected[2] > old.stamp[2] and old.offset > 0)
        if append:
            handle.seek(old.offset - old.tail_size)
            append = (hashlib.sha256(handle.read(old.tail_size)).digest() == old.tail_digest
                      and prefix_intact(handle, old))
        if append:
            state = replace(old, stamp=expected, digest=old.digest.copy())
        else:
            state = State(native_id, expected)
        handle.seek(state.offset)
        tail = b''
        for line in handle:
            if compressed and state.rows >= MAX_ROWS:
                raise ValueError('Compressed rollout row limit')
            if not line.endswith(b'\n'):
                break
            state.row(line)
            state.digest.update(line)
            state.offset += len(line)
            tail = (tail + line[-TAIL_BYTES:])[-TAIL_BYTES:]
        if state.offset:
            # Read at most the old-end window, not an indexed prefix or row body.
            # For compressed files retain the rolling last complete bytes instead
            # of seeking backwards and starting another decompression pass.
            if not compressed:
                handle.seek(max(0, state.offset - TAIL_BYTES))
                tail = handle.read(min(state.offset, TAIL_BYTES))
            state.tail_size = len(tail)
            state.tail_digest = hashlib.sha256(tail).digest()
        if transcripts.signature(os.fstat(handle.fileno())) != expected:
            raise Unstable('Rollout changed during observation')
    if transcripts.signature(path.lstat()) != expected:
        raise Unstable('Rollout replaced during observation')
    remember(_states, path, state)
    return state


def agree(new_path, newest, older):
    """A bounded digest proves an older copy's complete prefix, without row indexes."""
    if older.offset > newest.offset:
        return False
    if older.offset == newest.offset:
        return older.digest.digest() == newest.digest.digest()
    with open_rollout(new_path) as handle:
        if transcripts.signature(os.fstat(handle.fileno())) != newest.stamp:
            raise Unstable('Rollout changed before copy proof')
        digest, remaining = hashlib.sha256(), older.offset
        while remaining:
            chunk = handle.read(min(65536, remaining))
            if not chunk:
                return False
            remaining -= len(chunk)
            digest.update(chunk)
        if transcripts.signature(os.fstat(handle.fileno())) != newest.stamp:
            raise Unstable('Rollout changed during copy proof')
    return digest.digest() == older.digest.digest()


def read(home, native_id):
    native_id = normalize_session_id(native_id)
    # One lock bounds cache mutation and avoids two concurrent tail scans
    # publishing incompatible state. No native or runtime lock is acquired.
    with _lock:
        found = paths(home, native_id)
        if not found:
            return {}
        key = str(home), native_id
        stamp_hash = hashlib.sha256()
        for path, stamp in found:
            stamp_hash.update(repr((str(path), stamp)).encode())
        stamps = stamp_hash.digest()
        cached = _copies.get(key)
        if cached is not None and cached[0] == stamps:
            _copies.move_to_end(key)
            return dict(cached[1])
        try:
            states = [scan(path, stamp, native_id) for path, stamp in found]
            newest = states[-1]
            if (any(not state.valid or not state.rows for state in states)
                    or any(not agree(found[-1][0], newest, state) for state in states[:-1])):
                value = {}
            else:
                value = newest.value()
        except Unstable:
            raise
        except (OSError, ValueError, TypeError):
            # A failed decoder may raise only on context-manager exit. Save no
            # error or partial reading; a second full stamp-set check below
            # qualifies only an unchanged negative result for reuse. Discovery
            # failures and observed concurrent mutations never reach the cache.
            value = {}
        if paths(home, native_id) != found:
            raise Unstable('Rollout copies changed during observation')
        remember(_copies, key, (stamps, dict(value)))
        return value
