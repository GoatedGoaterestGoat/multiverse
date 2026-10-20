"""Read-only native rollout evidence; core selects and adopts candidate homes."""
import json
import os
from pathlib import Path
import re
import stat

from ..types import NativeResult, normalize_session_id

MAX_ENTRIES = 20000
HEADER_BYTES = 1024 * 1024


def session_home(environment):
    value = environment.get('CODEX_HOME')
    if value is not None and not isinstance(value, str):
        return None
    if not value or isinstance(value, str) and (value == '~' or value.startswith('~/')):
        home = environment.get('HOME')
        if not isinstance(home, str) or not home or '\0' in home or not Path(home).is_absolute():
            return None
        value = str(Path(home) / ('.codex' if not value else value[2:]))
    if not isinstance(value, str) or not value or '\0' in value or not Path(value).is_absolute():
        return None
    try:
        return str(Path(value).resolve())
    except (OSError, RuntimeError):
        return None


def _unknown(path, reason):
    return NativeResult('unknown', f'Cannot verify Codex rollout storage at {path}: {reason}')


def _metadata_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate metadata fields')
        result[key] = value
    return result


def _header(path, native_id):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return _unknown(path, 'the matching rollout is not a regular file')
            line = handle.readline(HEADER_BYTES + 1)
        if len(line) > HEADER_BYTES or not line.endswith(b'\n'):
            return _unknown(path, 'the metadata header is oversized or incomplete')
        row = json.loads(line, object_pairs_hook=_metadata_object)
        payload = row.get('payload') if isinstance(row, dict) and row.get('type') == 'session_meta' else None
        if not isinstance(payload, dict) or normalize_session_id(payload.get('id')) != native_id:
            return _unknown(path, 'metadata does not identify the requested conversation')
        if 'session_id' in payload:
            # Native session_id names the root; a fork has its own stable id.
            normalize_session_id(payload['session_id'])
    except (OSError, ValueError, RecursionError) as error:
        return _unknown(path, str(error))
    return NativeResult('ok', value={'present': True})


def session_storage_evidence(session, home):
    """Prove an exact thread from native metadata; never treat incomplete reads as absence."""
    try:
        path = Path(home)
        native_id = normalize_session_id(session.native_id)
    except (TypeError, ValueError) as error:
        return _unknown(home, str(error))
    if not path.is_absolute():
        return _unknown(path, 'the candidate home must be absolute')
    roots = (path / 'sessions', path / 'archived_sessions')
    pending, scanned = list(reversed(roots)), 0
    uuid = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
    filename = re.compile(r'rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-' + native_id
                          + r'(?:_' + uuid + r')?\.jsonl(?:\.zst)?')
    while pending:
        directory = pending.pop()
        try:
            if directory.is_symlink():
                return _unknown(directory, 'a linked sessions directory cannot establish absence')
            entries = os.scandir(directory)
        except FileNotFoundError as error:
            if directory in roots:
                continue
            return _unknown(directory, str(error))
        except OSError as error:
            return _unknown(directory, str(error))
        try:
            with entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_ENTRIES:
                        return _unknown(directory, f'the scan exceeded {MAX_ENTRIES} entries')
                    candidate = Path(entry.path)
                    if filename.fullmatch(entry.name):
                        if entry.name.endswith('.zst'):
                            plain = candidate.with_suffix('')
                            if not plain.exists():
                                return _unknown(candidate, 'compressed rollout metadata cannot be verified by this reader')
                            candidate = plain  # Native gives the plain sibling precedence.
                        return _header(candidate, native_id)
                    if entry.name.startswith('rollout-') and native_id in entry.name:
                        return _unknown(candidate, 'unsupported matching rollout filename')
                    if entry.is_symlink():
                        return _unknown(candidate, 'a linked entry cannot establish absence')
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(candidate)
        except OSError as error:
            return _unknown(directory, str(error))
    return NativeResult('ok', value={'present': False})
