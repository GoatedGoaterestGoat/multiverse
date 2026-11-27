"""Finding, opening and reading a Claude session's transcript copies, for every reader in the adapter.

A session's transcript is `<Claude home>/projects/<folder>/<session>.jsonl`. Claude names the folder after the
directory the session was launched in, so a session normally has one copy. Claude moves a transcript by renaming
it, so more than one copy is an anomaly, which the transcript reads check (`newest_copy`). Readers get two views:

- the newest copy, indexed incrementally (`Transcript`): usage, the context reading and the transcript reads;
- raw copies (`raw_rows`): every copy, every non-empty line parsed or marked `DAMAGED`, fields as stored, never
  deduplicated, for the evidence readers, which look for copies of one row that disagree.

Every file is opened through `open_regular`, and any part of the storage that could hold the session but can't
be read raises `Unreadable` rather than read as absence.
"""
from collections import OrderedDict
from dataclasses import dataclass
import contextlib
import contextvars
import dataclasses
import itertools
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
from time import monotonic
import unicodedata

from ..types import Usage

USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')

def claude_home(environment=None):
    """Claude's storage root: `environment` as a launch sees it, else this process's."""
    source = environment or os.environ
    if source.get('CLAUDE_CONFIG_DIR'):
        return Path(source['CLAUDE_CONFIG_DIR'])
    return (Path(source['HOME']) if source.get('HOME') else Path.home()) / '.claude'


class Unreadable(Exception):
    """A folder or file under Claude's storage that can't be read, so a lookup can't tell what is there."""

    def __init__(self, path):
        super().__init__(f"{path} can't be read")
        self.path = str(path)


def open_regular(path):
    """`path` opened for reading, through any link, only when it is a regular file, and never waiting: a FIFO,
    device or directory at a transcript's name raises Unreadable. The type is read from the opened descriptor, so a
    file swapped in after a check can't slip past, and a FIFO with no writer can't hold the open (or a lock)."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Unreadable(path)
        os.set_blocking(descriptor, True)
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def read_regular(path):
    """The whole of a regular file (open_regular)."""
    with open_regular(path) as handle:
        return handle.read()


PROJECT_KEY_LENGTH = 200  # past this, Claude keeps a project folder name's first 200 characters and adds a hash


def _hash(text):
    """Claude's string hash: h = h * 31 + code unit over the UTF-16 code units, as a signed 32-bit integer."""
    h, data = 0, text.encode('utf-16-le')
    for i in range(0, len(data), 2):
        h = (h * 31 + (data[i] | data[i + 1] << 8)) & 0xFFFFFFFF
    return h - (1 << 32) if h & 0x80000000 else h


def _base36(number):
    digits, out = '0123456789abcdefghijklmnopqrstuvwxyz', ''
    while True:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
        if not number:
            return out


def project_key(path):
    """Claude's project folder name for a launch directory: the NFC path, every UTF-16 code unit that isn't
    an ASCII letter or digit replaced by '-', as Bun does (so a character outside the BMP gives '--'). Past 200
    characters, its first 200, '-', and the path's hash in base 36 (Claude Code 2.1.312's own rule)."""
    text = unicodedata.normalize('NFC', str(path))
    key = ''.join(c if c.isascii() and c.isalnum() else '-' * (2 if ord(c) > 0xFFFF else 1) for c in text)
    return key if len(key) <= PROJECT_KEY_LENGTH else f'{key[:PROJECT_KEY_LENGTH]}-{_base36(abs(_hash(text)))}'


def could_hold(name, workspaces):
    """Whether Claude could file a session launched in one of `workspaces` under the project folder `name`.

    The name is lossy, so this errs towards yes: the folder's name, ignoring case, is a workspace's key or
    starts with it plus '-' (Claude's own worktrees under the workspace, and the hashed form past 200 units).
    With no absolute workspace to go by, any folder could.
    """
    return holds_name(name, project_keys(workspaces))


def project_keys(workspaces):
    """The folder keys of `workspaces` (each absolute one as recorded and as its real path), for holds_name; resolved
    once per scan, since the listing scans a shared store per agent."""
    return {project_key(form)[:PROJECT_KEY_LENGTH].casefold()
            for workspace in workspaces if isinstance(workspace, str) and os.path.isabs(workspace)
            for form in (workspace, os.path.realpath(workspace))}


def holds_name(name, keys):
    """could_hold, from the keys project_keys gave; no keys means any folder could."""
    folded = name.casefold()
    return not keys or any(folded == key or folded.startswith(key + '-') for key in keys)


def session_transcripts(native_id, environment=None, workspaces=(), links=False, check=None):
    """Every main transcript of `native_id` (`projects/*/<id>.jsonl`), oldest first.

    Raises Unreadable when a folder can't be listed or searched and could hold the session (a dangling link, or a
    folder gone before it was searched, counts as one that can't be searched): Claude files it
    under its launch directory's name, so with `workspaces` (the launch's own, never re-read) an unreadable
    folder of another workspace is skipped. pathlib's glob skips every unreadable folder silently, so a scan
    that finds nothing would otherwise read as proof the transcript is absent. `check`: called before the listing
    and between its entries; it raises past a read's deadline.
    """
    return _scan_transcripts(native_id, environment, workspaces, links, check)[0]


def _scan_transcripts(native_id, environment=None, workspaces=(), links=False, check=None):
    """session_transcripts, and whether this call listed the projects folder itself (False when it is missing)."""
    projects = claude_home(environment) / 'projects'
    found, files, keys = [], set(), project_keys(workspaces)
    if check is not None:
        check()  # right before the listing opens
    try:
        listing = os.scandir(projects)
    except FileNotFoundError:
        return [], False
    except OSError as error:
        raise Unreadable(projects) from error
    entries, taking = [], iter(listing)
    try:
        while True:  # taken one at a time: a deadline mid-listing fetches no further entry
            if check is not None:
                check()
            try:
                entries.append(next(taking))
            except StopIteration:
                break
            except OSError as error:  # the listing failing partway is the folder not being listable
                raise Unreadable(projects) from error
    finally:
        close = getattr(listing, 'close', None)
        if close is not None:
            close()
    for entry in entries:
        if check is not None:
            check()  # outside the handlers below: a deadline is never taken for an unreadable folder
        candidate = Path(entry.path) / f'{native_id}.jsonl'
        holds = holds_name(entry.name, keys)
        try:
            if holds:
                # Resolved now, through any link, never from the listing's type: a dangling or looping link, or a
                # folder gone since the listing, proves nothing; a link to a real folder is searched through it.
                if not stat.S_ISDIR(os.stat(entry.path).st_mode):
                    continue
            elif not entry.is_dir():
                continue
            # `links` finds a link (even a dangling one) where a transcript should be, for readers that refuse it.
            status = candidate.lstat() if links else candidate.stat()
            if (status.st_dev, status.st_ino) in files:
                continue  # the same file through another path (a linked project folder): read it once
            files.add((status.st_dev, status.st_ino))
            found.append((status.st_mtime, candidate))
        except FileNotFoundError as error:
            if holds and not os.path.isdir(entry.path):  # the folder itself is gone: a dangling link, or removed mid-scan
                raise Unreadable(entry.path) from error
            continue
        except OSError as error:
            if holds:  # a project folder that can't be searched could hold it
                raise Unreadable(entry.path) from error
    return [path for _, path in sorted(found)], True


IDENTITY_BYTES = 1 << 20


def transcript_identity(path, native_id):
    """None when `path` is a regular file whose rows name session `native_id` (read within its first MiB), else
    why it proves nothing: a link, a directory or other non-regular file, an unreadable or empty file, or rows of
    no session or another one."""
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            return f'{path} is a link, not a transcript'
        if not stat.S_ISREG(path.lstat().st_mode):
            return f'{path} is not a regular file'
        with open_regular(path) as handle:
            head = handle.read(IDENTITY_BYTES)
    except Unreadable:
        return f'{path} is not a regular file'
    except OSError as error:
        return f"{path} can't be read ({error.strerror or error})"
    named = set()
    for raw in head.split(b'\n')[:-1] if len(head) == IDENTITY_BYTES else head.split(b'\n'):
        try:
            row = json.loads(raw) if raw.strip() else None
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get('sessionId'), str):
            named.add(row['sessionId'])
    if named - {native_id}:
        return f"{path} holds another session's rows"
    if native_id not in named:
        return f'{path} has no row naming this session'
    return None


def subagent_transcripts(main):
    """The subagent transcripts beside a main transcript; Unreadable when part of that folder can't be read."""
    folder = main.parent / main.stem / 'subagents'
    found = []

    def unreadable(error):
        if not isinstance(error, FileNotFoundError):
            raise Unreadable(error.filename or folder) from error

    for directory, _, files in os.walk(folder, onerror=unreadable):
        found.extend(Path(directory) / name for name in files if name.endswith('.jsonl'))
    return sorted(found)


def has_saved_conversation(native_id, environment=None, workspaces=()):
    """Whether Claude has written a transcript for this session.

    Claude writes nothing until the first message, so a session that was never
    used has none, and `--resume` refuses it. When its storage can't be read this
    says yes: resume then tries the conversation, and Claude says if it is gone.
    """
    try:
        return bool(session_transcripts(native_id, environment, workspaces))
    except Unreadable:
        return True


TAIL = 1 << 16  # how much of what an index already read it checks again when the file grew
PROOF_INTERVAL = 60  # seconds: a file that keeps growing is proved in full at least this often, even for usage


class Transcript:
    """One copy's index, read once and then as it grows: an Entry per complete line (metadata only, never bodies;
    none for a usage index), and the usage readers' totals from it.

    Claude writes one record per content block of a response, each with the response's usage, so usage is counted
    once per response ID. A row with zero usage is never a response: a compaction that keeps a row stores it again
    with zero usage, and an installed compaction's rows arrive as new IDs with zero usage (Claude's `<synthetic>`
    model). Neither replaces a real response's usage or becomes the latest request, so `last` stays the latest real
    response, and `observed` its model, effort and time: the latest main-thread turn's, as Claude recorded them.
    Only this conversation's turn counts: never a sidechain row, nor one whose sessionId names another conversation
    (null included; a row without the field is this one's, as the delivery and prompt readers hold). A complete row
    that can't be read after it may be the newer reply, so it makes `observed` unknown (None) until a later reply.

    Claude isn't append-only: it truncates a file at a tombstoned row and writes the rest back in place, and can
    rewrite a whole file. The index starts over on a new inode or a smaller file, and otherwise, when the file
    changed since the last read (its size or modification time), proves what it already read against a running hash
    of those bytes, starting over when they differ. A transcript read (`read(prove=True)`) proves every byte, once per
    change, so a position's chained hash stays exact. Usage, which reads on every listing, proves every byte when a
    change added no bytes (no append, so something rewrote it) or when `PROOF_INTERVAL` has passed since the last
    proof; for the ordinary append in between it checks only the last `TAIL` bytes, which a tombstone always moves,
    so it stays at one parse per new byte, and the due proof still runs once the file has gone quiet after such
    a change. `rewrites` counts the restarts. An unchanged file costs no read, and an
    append never changes an earlier row's position.
    """

    def __init__(self, path, rows=True):
        # rows=False: a usage index, which keeps the totals and the latest turn but no Entry per row.
        self.path, self.rewrites, self.rows = path, 0, rows
        self.saved = None  # a usage index another process saved, proved at the next read before it counts
        self.saved_offset = 0  # how far the copy in the agent's store reaches, as this process last wrote or took it
        self.saved_rows = None  # a full row index another process saved (ROW_STORE), proved at the next read
        self.rows_saved = (0, None)  # how far the saved row index reaches, and monotonic() when this process wrote it
        self._reset(None)

    def _reset(self, inode):
        self.epoch = next(_epochs)
        self.inode, self.offset, self.entries, self.responses, self.last = inode, 0, [], {}, None
        self.observed, self.chain = None, ''
        self.stamp, self.proven, self.prefix, self.tail = None, None, hashlib.sha256(), b''
        self.proven_at = None  # monotonic() at the last full proof (or whole read)
        self.views = {}  # the adapter's views of this index as read, by tip and what they were built from
        self.unproved = False  # a change was accepted on the tail check alone since that proof

    def _tail_intact(self, handle):
        """Whether the last bytes already indexed are still exactly what was read."""
        handle.seek(self.offset - len(self.tail))
        return handle.read(len(self.tail)) == self.tail

    def _intact(self, handle, check=None):
        """Whether the bytes already indexed are still exactly what was read. `check`: called between chunks."""
        digest, left = hashlib.sha256(), self.offset
        handle.seek(0)
        while left:
            if check is not None:
                check()
            chunk = handle.read(min(left, 1 << 22))
            if not chunk:
                return False
            digest.update(chunk)
            left -= len(chunk)
        return digest.digest() == self.prefix.digest()

    def saved_state(self):
        """What another process needs to take this usage index over: where it read to and a digest of those bytes,
        the latest time and turn, and each response's counted usage. No transcript text."""
        return {'inode': self.inode, 'offset': self.offset, 'prefix': self.prefix.hexdigest(), 'chain': self.chain,
                'last': self.last, 'observed': self.observed,
                'responses': {key: [counted(usage, name) for name in USAGE_KEYS] for key, usage in self.responses.items()}}

    def adopt(self, saved):
        """Take a usage index another process saved (saved_state's shape), to prove at the next read; one that
        doesn't have that shape is ignored, and the next read starts from the beginning."""
        try:
            observed, responses = saved['observed'], saved['responses']
            shaped = (all(type(saved[key]) is int and saved[key] >= 0 for key in ('inode', 'offset'))
                      and isinstance(saved['prefix'], str) and re.fullmatch('[0-9a-f]{64}', saved['prefix'])
                      and isinstance(saved['chain'], str) and (saved['last'] is None or isinstance(saved['last'], str))
                      and (observed is None or (isinstance(observed, dict) and set(observed) == {'model', 'effort', 'observed_at'}
                                                and all(v is None or isinstance(v, str) for v in observed.values())))
                      and isinstance(responses, dict)
                      and all(isinstance(counts, list) and len(counts) == len(USAGE_KEYS)
                              and all(type(n) is int and n >= 0 for n in counts) for counts in responses.values()))
        except (KeyError, TypeError):
            shaped = False
        if shaped and saved['offset']:
            self.saved = saved

    def _take_saved(self, handle, status):
        """Prove the saved index's bytes unchanged (the same inode, then a hash of every byte it read, which costs
        no parse) and take it over; False, and nothing taken, when they changed."""
        saved, self.saved = self.saved, None
        if status.st_ino != saved['inode'] or status.st_size < saved['offset']:
            return False
        digest, left, tail = hashlib.sha256(), saved['offset'], b''
        handle.seek(0)
        while left:
            chunk = handle.read(min(left, 1 << 22))
            if not chunk:
                return False
            digest.update(chunk)
            tail = (tail + chunk)[-TAIL:]
            left -= len(chunk)
        if digest.hexdigest() != saved['prefix']:
            return False
        self._reset(status.st_ino)
        self.offset, self.prefix, self.tail, self.chain = saved['offset'], digest, tail, saved['chain']
        self.responses = {key: dict(zip(USAGE_KEYS, counts)) for key, counts in saved['responses'].items()}
        self.last, self.observed = saved['last'], dict(saved['observed']) if saved['observed'] else None
        # Proved now: the next lines are read as an append to it.
        self.stamp = self.proven = (status.st_mtime_ns, status.st_size)
        self.proven_at, self.saved_offset = monotonic(), self.offset
        return True

    def rows_snapshot(self):
        """This index's rows and the proof of the bytes they cover, taken together (under _transcripts_lock, which a
        read holds while it changes them), for a save to serialize: a read that goes on meanwhile can't put newer
        rows or a newer offset in the file, nor have the save mark them saved."""
        return _Snapshot(self.inode, self.offset, self.prefix.hexdigest(), self.chain, self.entries[:], self.epoch)

    def row_sections(self, analysis):
        """The saved form of this full index and its analysis (from the newest row, for these rows): a JSON header,
        then one JSON array per row field and for the labels and history, each with its length and SHA-256 in the
        header. No row text: a row keeps its mark, and a view reads a preview from the transcript. None past
        ROW_STORE_BYTES."""
        saving = _SERIALIZING.get()  # save_rows' snapshot and deadline, while it serializes
        check, snap = (saving.check, saving.snapshot) if saving is not None else (None, self.rows_snapshot())
        entries, tick, blobs = snap.entries, ticker(check), []

        def step():
            if check is not None:
                check()

        def encode(values):  # in chunks, each after a check, so no pass over every row runs past the deadline
            parts = []
            for start in range(0, len(values), ENCODE_ROWS):
                step()
                parts.append(json.dumps(values[start:start + ENCODE_ROWS], separators=(',', ':'))[1:-1])
            return ('[' + ','.join(parts) + ']').encode()

        for name in _ROW_FIELDS:
            step()  # before each section's projection
            values = []
            for item in entries:
                if tick is not None:
                    tick()
                values.append(_saved_field(name, getattr(item, name)))
            blobs.append((name, encode(values)))
        step()
        codes = sorted({label for label, _ in analysis.labels})
        number, labels = {label: n for n, label in enumerate(codes)}, []
        for label, _ in analysis.labels:
            if tick is not None:
                tick()
            labels.append(number[label])
        blobs.append(('label', encode(labels)))
        blobs.append(('codes', encode(codes)))
        step()
        blobs.append(('history', encode(list(analysis.history.rows))))
        step()
        header = {'schema': ROW_SCHEMA, 'derivation': ROW_DERIVATION, 'path': str(self.path), 'inode': snap.inode,
                  'offset': snap.offset, 'prefix': snap.prefix, 'chain': snap.chain, 'rows': len(entries),
                  'sections': [[name, len(blob), hashlib.sha256(blob).hexdigest()] for name, blob in blobs]}
        data = json.dumps(header, separators=(',', ':')).encode() + b'\n' + b''.join(blob for _, blob in blobs)
        return data if len(data) <= ROW_STORE_BYTES else None

    def adopt_rows(self, data, check=None):
        """Take a saved row index (row_sections' form) to prove at the next read: one with any fault (another
        schema, reader version or path, a section's length or checksum, a field of the wrong shape) is ignored, and
        the next read parses from the start. `check` holds a read's deadline through it."""
        try:
            end = data.index(b'\n')
            header = json.loads(data[:end])
            if (header.get('schema') != ROW_SCHEMA or header.get('derivation') != ROW_DERIVATION
                    or header.get('path') != str(self.path)
                    or not all(type(header.get(key)) is int and header[key] >= 0 for key in ('inode', 'offset', 'rows'))
                    or not header['offset'] or not isinstance(header.get('chain'), str)
                    or not re.fullmatch('[0-9a-f]{64}', str(header.get('prefix')))):
                return False
            position, got = end + 1, {}
            for name, size, digest in header['sections']:
                if check is not None:
                    check()
                blob = data[position:position + size]
                position += size
                if len(blob) != size or hashlib.sha256(blob).hexdigest() != digest:
                    return False
                got[name] = json.loads(blob)
            count = header['rows']
            if (position != len(data) or set(got) != {*_ROW_FIELDS, 'label', 'codes', 'history'}
                    or any(len(got[name]) != count for name in (*_ROW_FIELDS, 'label'))
                    or not all(type(n) is int and 0 <= n < count for n in got['history'])
                    or not all(type(n) is int and 0 <= n < len(got['codes']) for n in got['label'])
                    or not all(usage is None or isinstance(usage, dict) and set(usage) <= set(USAGE_KEYS)
                               and all(type(n) in (int, float) for n in usage.values()) for usage in got['usage'])):
                return False
            columns = [[tuple(value) for value in got[name]] if name in _TUPLES else got[name] for name in _ROW_FIELDS]
            columns.insert(_HEAD, [None] * count)  # no text: a view reads it from the transcript (with_text)
            entries = []
            for values in zip(*columns):
                if check is not None:
                    check()
                entries.append(Entry(*values))
            if not entries or entries[-1].chain != header['chain']:
                return False
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
        self.saved_rows = {**{key: header[key] for key in ('inode', 'offset', 'prefix', 'chain')}, 'entries': entries}
        return True

    def _take_rows(self, handle, status, check):
        """Prove the saved row index's bytes unchanged (the same inode, then a hash of every byte it covers) and take
        it over; False, and the index left empty, when they changed. A read's deadline cutting the proof keeps the
        saved rows, to prove at the next read."""
        saved = self.saved_rows
        if status.st_ino != saved['inode'] or status.st_size < saved['offset']:
            self.saved_rows = None
            return False
        digest, left, tail = hashlib.sha256(), saved['offset'], b''
        handle.seek(0)
        while left:
            if check is not None:
                check()
            chunk = handle.read(min(left, 1 << 22))
            if not chunk:
                self.saved_rows = None
                return False
            digest.update(chunk)
            tail = (tail + chunk)[-TAIL:]
            left -= len(chunk)
        if digest.hexdigest() != saved['prefix']:
            self.saved_rows = None
            return False
        conversation, responses, last, observed = self.path.stem, {}, None, None
        for item in saved['entries']:  # in locals: a cut here keeps the saved rows pending, and publishes nothing
            if check is not None:
                check()
            last, observed = _account(item, conversation, responses, last, observed)
        if check is not None:
            check()
        self.saved_rows = None
        self._reset(status.st_ino)
        self.offset, self.prefix, self.tail, self.chain = saved['offset'], digest, tail, saved['chain']
        self.entries, self.responses, self.last, self.observed = saved['entries'], responses, last, observed
        self.stamp = self.proven = (status.st_mtime_ns, status.st_size)
        self.proven_at, self.rows_saved = monotonic(), (self.offset, None)
        return True

    def read(self, *, prove=False, check=None):
        """Read up to date. `prove`: also prove every byte indexed earlier unchanged, if the file changed since the
        last proof; `self.proven` is the file's stamp then (or when the index was read whole). `check`: called
        between chunks and every CHECK_ROWS rows; it raises past a read's deadline, and the index is then left as
        it was (or empty, when a proof had already found it stale), never partly read."""
        if check is not None:
            check()  # right before the transcript opens
        with open_regular(self.path) as handle:
            status = os.fstat(handle.fileno())
            if self.saved is not None:
                self._take_saved(handle, status)
            if self.saved_rows is not None:
                self._take_rows(handle, status, check)
            stamp = (status.st_mtime_ns, status.st_size)
            proved = tail_only = False
            if status.st_ino != self.inode or status.st_size < self.offset:
                stale = True
            elif not self.offset:
                stale = False
            elif prove and stamp != self.proven:
                stale, proved = not self._intact(handle, check), True
            else:
                due = self.proven_at is None or monotonic() - self.proven_at >= PROOF_INTERVAL
                if stamp == self.stamp and not (self.unproved and due):
                    stale = False  # unchanged since a read that proved it, or not due yet
                elif stamp == self.stamp or status.st_size == self.stamp[1] or due:
                    stale, proved = not self._intact(handle, check), True  # no append to explain the change, or due a proof
                else:
                    stale, tail_only = not self._tail_intact(handle), True
            if stale:
                if self.inode is not None:
                    self.rewrites += 1
                self._reset(status.st_ino)
                self.rows_saved = (0, None)  # a rewrite: whatever was saved covers other bytes now
            start = self.offset
            handle.seek(self.offset)
            # Read, split and parse a chunk at a time, so a deadline is checked throughout (one read and split of a
            # large file is half a second alone). Change nothing until every new line parsed: a failure or a deadline
            # leaves the index as it was, so the next read starts from the same place and never counts a row twice.
            added, responses, last, observed, position = [], {}, self.last, self.observed, self.offset
            conversation = self.path.stem  # a main transcript is named after its session
            chain, prefix, tail, read_to = self.chain, self.prefix.copy(), self.tail, self.offset
            pending = b''  # the start of a line whose newline hasn't been read yet
            while True:
                if check is not None:
                    check()
                part = handle.read(READ_CHUNK)
                if not part:
                    break  # an unfinished last line waits for its newline
                buffer = pending + part
                end = buffer.rfind(b'\n') + 1
                whole, pending = buffer[:end], buffer[end:]
                if not whole:
                    continue
                for line in whole.split(b'\n')[:-1]:
                    if check is not None:
                        check()  # every row: a check is cheap next to a row's parse
                    item = entry(position, line + b'\n', chain)
                    if self.rows:
                        added.append(item)
                    last, observed = _account(item, conversation, responses, last, observed)
                    position, chain = position + item.size, item.chain
                prefix.update(whole)
                tail = (tail + whole)[-TAIL:]
                read_to += len(whole)
        self.entries.extend(added)
        self.chain = chain
        self.responses.update(responses)
        self.last, self.observed = last, observed
        self.prefix, self.tail, self.offset = prefix, tail, read_to
        self.stamp = stamp
        if prove or proved or not start:
            self.proven, self.proven_at, self.unproved = stamp, monotonic(), False
        elif tail_only:
            self.unproved = True
        return self


def _saved_field(name, value):
    """A row field as the saved index keeps it: tuples as lists, and of an assistant row's usage only the numeric
    counters mv2 reads (Claude's usage can hold other keys, text among them)."""
    if name in _TUPLES:
        return list(value)
    if name == 'usage' and value is not None:
        return {key: value[key] for key in USAGE_KEYS if type(value.get(key)) in (int, float)}
    return value


def _account(item, conversation, responses, last, observed):
    """One row's part in the index's usage: its response's counts, the latest reply's time and observed settings."""
    if item.kind == 'unreadable':
        observed = None
    if item.kind == 'assistant' and item.usage is not None and item.response:
        responses[item.response] = {key: item.usage[key] for key in USAGE_KEYS if key in item.usage}
        last = item.at or last
        if not item.sidechain and item.model != '<synthetic>' and not (item.has_session and item.session != conversation):
            # The latest reply's own fields, even when its model is missing: never an older turn's.
            observed = {'model': item.model, 'effort': item.effort, 'observed_at': item.at}
    return last, observed


# A process that opens reads a transcript's rows from its start: tens of seconds for a large one on a loaded machine.
# So a full index is also kept in the agent's folder (`ROW_STORE`, one file per transcript copy): a new process
# proves it by a hash of the bytes it covers, takes its rows, and parses only what Claude wrote since. It is mv2's own
# state, checked against corruption and transcript change, not against deliberate rewriting by the same user. It
# holds no row text (each row's mark stands for what labelling needs from it), and is written only by a process
# that has just read, at most every ROW_SAVE_INTERVAL per transcript, and only when the read's deadline leaves
# ROW_SAVE_MARGIN: never by a turn event's 0.5 s read. ROW_DERIVATION changes whenever the reader would derive a
# row's fields or labels differently, so an index from older code is read again rather than trusted.
ROW_STORE = 'claude-rows'
ROW_SCHEMA = 1
ROW_DERIVATION = 3
ROW_STORE_BYTES = 128 << 20
ENCODE_ROWS = 16384  # rows per encoded chunk of a saved section, each after a deadline check
ROW_SAVE_INTERVAL = 5
ROW_SAVE_MARGIN = 2.0


def row_file(store, path):
    """The saved row index of the transcript at `path`, in an agent's ROW_STORE folder."""
    return Path(store) / f'{hashlib.sha256(str(path).encode()).hexdigest()[:24]}.idx'


_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def _rows_folder(store, create=False):
    """The agent's ROW_STORE folder, opened without following a link (a descriptor), or None when it is missing (and
    not `create`), a link, or not a folder: the index is then neither read nor written."""
    flags = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | os.O_NONBLOCK
    try:
        return os.open(store, flags)
    except FileNotFoundError:
        if not create:
            return None
    except OSError:
        return None
    try:
        os.mkdir(store)
    except FileExistsError:
        pass
    except OSError:
        return None
    try:
        return os.open(store, flags)
    except OSError:
        return None


def _load_rows(file, check=None):
    """A saved row index's bytes, or None when there is none, or it (or its folder) is a link, not a regular file,
    unreadable or past ROW_STORE_BYTES (read through the opened file, so one swapped in after any check can't be
    read past the bound)."""
    if check is not None:
        check()
    folder = _rows_folder(file.parent)
    if folder is None:
        return None
    try:
        descriptor = os.open(file.name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)  # a FIFO never blocks it
    except OSError:
        os.close(folder)
        return None
    try:
        with os.fdopen(descriptor, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            if check is not None:
                check()  # between the open and the read, as for the transcript
            data = handle.read(ROW_STORE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(folder)
    return data if len(data) <= ROW_STORE_BYTES else None


_SERIALIZING = contextvars.ContextVar('serializing', default=None)  # save_rows' check, for row_sections


class SaveStopped(Exception):
    """An optional save's own budget ran out: nothing was replaced, and the read it followed stands."""


def save_rows(index, analysis, store, check=None, interval=True):
    """Write `index`'s rows and its analysis (from the newest row) to the agent's store, whole, beside the file and
    moved over it; only when it reaches further than the copy there, and with `interval`, at most every
    ROW_SAVE_INTERVAL. True when the saved copy now reaches as far as `index`. `check` holds a read's deadline
    through it: past it the temporary file is removed and nothing is replaced. Never raises otherwise: a store that
    can't be written only means the next process reads from the start."""
    reached, written = index.rows_saved
    if store is None or not index.offset or analysis.count != len(index.entries):
        return False
    if index.offset == reached:
        return True
    if interval and written is not None and monotonic() - written < ROW_SAVE_INTERVAL:
        return False
    with _holding(_transcripts_lock, check):  # a read holds it while it changes the index
        snap = index.rows_snapshot()
    if not snap.offset or snap.epoch != analysis.epoch or len(snap.entries) != analysis.count:
        return False  # the index moved on since its analysis: the next save writes it
    token = _SERIALIZING.set(_Saving(check, snap))
    try:
        data = index.row_sections(analysis)
    finally:
        _SERIALIZING.reset(token)
    if check is not None:
        check()  # serialization scales with the transcript
    if data is None:
        return False
    name = row_file(store, index.path).name
    folder = _rows_folder(store, create=True)
    if folder is None:
        return False
    temporary = None
    try:
        # Created new beside it, in the folder as opened (never through a link), and moved over it whole.
        candidate = f'{name}.{secrets.token_hex(8)}.tmp'
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=folder)
        temporary = candidate
        with os.fdopen(descriptor, 'wb') as handle:
            for start in range(0, len(data), READ_CHUNK):
                if check is not None:
                    check()
                handle.write(data[start:start + READ_CHUNK])
        # Publication, under the lock taken within the deadline: an older save never replaces what a newer one of
        # this index published, nor one after a rewrite, and the mark always names what the file now holds.
        with _holding(_transcripts_lock, check):
            if check is not None:
                check()  # the acquire itself can cross the deadline
            # Only the index this process holds for the transcript publishes: an evicted one's save would replace
            # its successor's newer file.
            if _transcripts.get(index.path) is not index or index.epoch != snap.epoch or index.rows_saved[0] > snap.offset:
                return False
            os.replace(temporary, name, src_dir_fd=folder, dst_dir_fd=folder)
            temporary = None
            index.rows_saved = (snap.offset, monotonic())
            return index.offset == snap.offset
    except OSError:
        return False
    finally:
        if temporary:
            try:
                os.unlink(temporary, dir_fd=folder)
            except OSError:
                pass
        os.close(folder)


def with_text(handle, item):
    """`item` with its head, read from the transcript at its offset, for a row taken from a saved index (which keeps
    no text); its head as a cold read finds it, or empty when the line there is no longer that row."""
    if item.head is not None:
        return item
    handle.seek(item.offset)
    line = handle.read(item.size)
    if len(line) != item.size or hashlib.sha256(line).hexdigest()[:16] != item.digest:
        return dataclasses.replace(item, head='')
    try:
        row = json.loads(line)
        message = row.get('message') if isinstance(row.get('message'), dict) else {}
        attachment = row.get('attachment') if isinstance(row.get('attachment'), dict) else {}
        head = _head(row, message.get('content', row.get('content')), attachment)
    except (ValueError, AttributeError, TypeError):
        head = ''
    return dataclasses.replace(item, head=head)


# Indexes by path. Usage reads every agent's transcripts on each listing, so its indexes keep no Entry per row (a
# full index holds about 1.5 times its file); the transcript reads' full indexes are kept for the most recently read
# transcripts only.
USAGE_INDEXES = 256
FULL_INDEXES = 8
_usage = OrderedDict()
_transcripts = OrderedDict()
_transcripts_lock = threading.Lock()
# History versions come from one counter that only grows, for every transcript: a full index built again after its
# eviction starts above any version a reader holds from before, so the reader parses again rather than trusting a
# cursor into a history that changed while no index watched it.
_versions = itertools.count(1)
_epochs = itertools.count(1)  # one per index reset, for save_rows' snapshots


READ_CHUNK = 1 << 22  # bytes read at a time
CHECK_ROWS = 256  # rows between deadline checks: about 50 ms of parsing a large transcript


def ticker(check, every=CHECK_ROWS):
    """A function to call once per row: it calls `check` (which raises past a read's deadline) every `every` rows.
    None when there is no check."""
    if check is None:
        return None
    count = 0

    def tick():
        nonlocal count
        count += 1
        if count % every == 0:
            check()
    return tick


def _index(path, rows, saved=None, store=None, check=None):
    """The kept index for `path` (a full one when `rows`), the most recently used last; under _transcripts_lock.
    `saved`: an agent's saved usage indexes by path, one of which a new usage index takes over if it proves.
    `store`: the agent's ROW_STORE folder, whose saved row index a new full index takes over if it proves."""
    cache, limit = (_transcripts, FULL_INDEXES) if rows else (_usage, USAGE_INDEXES)
    index = cache.get(path)
    if index is None:
        index = cache[path] = Transcript(path, rows)
        if rows:
            index.history_version = next(_versions)
            data = _load_rows(row_file(store, path), check) if store is not None else None
            if data is not None:
                if check is not None:
                    check()  # right before its rows are taken
                index.adopt_rows(data, check)
        elif saved and isinstance(saved.get(str(path)), dict):
            index.adopt(saved[str(path)])
    cache.move_to_end(path)
    while len(cache) > limit:
        cache.popitem(last=False)
    return index


# A process that opens reads every transcript from its start, about a second per large agent, so each agent's usage
# indexes are also kept in its state folder (`USAGE_STORE`): a new process proves one by a hash of the bytes it read,
# which costs no parse, and parses only what Claude wrote since. It holds only positions, digests and counts, for the
# session's current transcripts; one past STORE_BYTES is neither written nor read.
USAGE_STORE = 'claude-usage.json'
STORE_BYTES = 32 << 20
SAVE_INTERVAL = 5  # seconds between one process's writes of one agent's store
_saved_at = {}  # store path: monotonic() of this process's last write


def _load_store(store):
    """An agent's saved usage indexes by transcript path, or {} when there's none or it can't be used. Bounded on
    the opened file, so one swapped in after any check can't be read past STORE_BYTES."""
    try:
        with open_regular(store) as handle:
            text = handle.read(STORE_BYTES + 1)
        if len(text) > STORE_BYTES:
            return {}
        data = json.loads(text)
    except (OSError, ValueError, Unreadable):
        return {}
    indexes = data.get('transcripts') if isinstance(data, dict) and data.get('schema') == 1 else None
    return indexes if isinstance(indexes, dict) else {}


def _save_store(store, states):
    """Write an agent's usage indexes ({path: saved_state}) whole, beside the store and moved over it. The
    temporary file has a random name and is created new, never through an entry already there."""
    text = json.dumps({'schema': 1, 'transcripts': states}, separators=(',', ':')).encode()
    if len(text) > STORE_BYTES:
        return False
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(dir=store.parent, prefix=f'{store.name}.', suffix='.tmp')
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(text)
        os.replace(temporary, store)
    except OSError:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        return False
    return True


def counted(usage, key):
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def context_reading(native_id, workspaces=(), environment=None, store=None):
    """(tokens, timestamp, observed) of the latest main-thread request, or None.

    Tokens are input + cache written + cache read. Subagent transcripts are
    excluded: they have their own context windows. `observed` is {'model',
    'effort', 'observed_at'} of the latest main-thread turn, as Claude recorded
    them, or None before one.
    """
    mains = session_transcripts(native_id, environment, workspaces)
    if not mains:
        return None
    with _transcripts_lock:
        saved = _load_store(store) if store and mains[-1] not in _usage else None
        transcript = _index(mains[-1], rows=False, saved=saved).read()
        responses, stamp, observed = transcript.responses, transcript.last, transcript.observed
    if not responses:
        return None
    latest = list(responses.values())[-1]
    tokens = sum(counted(latest, key) for key in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
    return tokens, stamp, dict(observed) if observed else None


def context_usage(native_id):
    """Tokens in the latest main-thread request, or None."""
    reading = context_reading(native_id)
    return reading[0] if reading else None


def measured_usage(native_id, workspaces=(), environment=None, store=None):
    """Cumulative usage of the session and its subagents, from their transcripts. Raises Unreadable rather
    than total a scan that skipped part of the storage. `store`: the agent's saved usage indexes (USAGE_STORE),
    taken over where they prove and written again after a read that moved."""
    mains = session_transcripts(native_id, environment, workspaces)
    if not mains:
        return None
    paths = [mains[-1], *subagent_transcripts(mains[-1])]
    with _transcripts_lock:
        saved = _load_store(store) if store and any(path not in _usage for path in paths) else None
        transcripts = [_index(path, rows=False, saved=saved).read() for path in paths]
        states = None
        if store and (monotonic() - _saved_at.get(store, -SAVE_INTERVAL) >= SAVE_INTERVAL
                      and any(transcript.offset != transcript.saved_offset for transcript in transcripts)):
            states = {str(path): transcript.saved_state() for path, transcript in zip(paths, transcripts) if transcript.offset}
            _saved_at[store] = monotonic()
    if states is not None and _save_store(store, states):
        with _transcripts_lock:
            for transcript in transcripts:
                transcript.saved_offset = states.get(str(transcript.path), {}).get('offset', 0)
    responses = [usage for transcript in transcripts for usage in transcript.responses.values()]
    cached = sum(counted(usage, 'cache_read_input_tokens') for usage in responses)
    written = sum(counted(usage, 'cache_creation_input_tokens') for usage in responses)
    inputs = sum(counted(usage, 'input_tokens') for usage in responses) + cached + written
    outputs = sum(counted(usage, 'output_tokens') for usage in responses)
    last = max((transcript.last for transcript in transcripts if transcript.last), default='')
    return Usage(native_id, inputs, outputs, inputs + outputs, 'claude-transcript', last, cached, written)


DAMAGED = object()  # a line that isn't valid JSON: damaged, or the last line cut short while Claude writes it


def raw_rows(path):
    """Every non-empty line of the copy at `path`, parsed, or DAMAGED where it doesn't parse; read whole through
    open_regular, so a copy that can't be read raises (OSError or Unreadable) before any row is given."""
    rows = []
    for line in read_regular(path).split(b'\n'):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            rows.append(DAMAGED)
    return rows


# What a reader needs to know about one complete line of a copy, without its body: where it is, a hash of its
# bytes for cursors, and the fields labels, the history walk and usage read. Built once per line.
@dataclass(frozen=True)
class _Snapshot:
    inode: int
    offset: int
    prefix: str
    chain: str
    entries: list
    epoch: int


@dataclass(frozen=True)
class _Saving:  # what save_rows hands row_sections while it serializes
    check: object
    snapshot: _Snapshot


@dataclass(frozen=True)
class Entry:
    offset: int
    size: int
    digest: str                     # the line's own hash, to check its bytes when reading its body
    chain: str                      # a hash of every line up to and including this one
    kind: str                       # the stored type ('assistant', 'system/turn_duration', ...), or 'unreadable'
    ref: str | None = None          # Claude's uuid
    parent: str | None = None
    logical: str | None = None      # a boundary's link to the conversation before it
    at: str | None = None
    version: str | None = None
    prompt_id: str | None = None
    origin: str | None = None       # origin.kind, on the row or a queued command's attachment
    meta: bool = False
    summary: bool = False           # Claude's own compaction summary
    sidechain: bool = False
    attachment: str | None = None
    command_mode: str | None = None
    head: str = ''                  # the start of a user or command row's text
    tool_result: bool = False
    tool_use_result: bool = False   # a real tool result carries it; a compaction's stored copy doesn't
    answers: tuple = ()             # tool_use ids a result answers
    response: str | None = None     # an assistant row's message id
    calls: tuple = ()               # tool_use ids an assistant row makes
    usage: dict | None = None       # an assistant row's usage, when any of it is nonzero
    stop: str | None = None
    preserved: tuple = ()           # a boundary's preservedMessages, in Claude's order
    companion: str | None = None    # a skill's instructions: the tool call they belong to
    model: str | None = None        # an assistant row's model, as Claude recorded it (`<synthetic>` for its own rows)
    effort: str | None = None       # the effort that row's request used, as Claude recorded it
    session: str | None = None      # the row's sessionId, when it is a string
    has_session: bool = False       # whether the row has a sessionId at all (null included)
    mark: str = ''                  # which marker head starts with, all labelling reads (_mark): kept without the text


# The saved row index's sections: every field but head, which it keeps no text of (ROW_STORE).
_ROW_FIELDS = tuple(field.name for field in dataclasses.fields(Entry) if field.name != 'head')
_HEAD = [field.name for field in dataclasses.fields(Entry)].index('head')
_TUPLES = frozenset(field.name for field in dataclasses.fields(Entry) if field.name in ('answers', 'calls', 'preserved'))


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(str(block.get('text', '')) for block in content if isinstance(block, dict) and block.get('type') == 'text')
    return ''


def _map(value):
    return value if isinstance(value, dict) else {}


def entry(offset, line, previous=''):
    """The Entry for one complete line (its newline included) at `offset`, after the line whose chain is `previous`.
    A line that isn't a JSON object, or whose fields have a shape this doesn't know, is `unreadable`: one damaged
    row never stops a read."""
    digest = hashlib.sha256(line).hexdigest()[:16]
    chain = hashlib.sha256(f'{previous}:{digest}'.encode()).hexdigest()[:16]
    try:
        row = json.loads(line)
    except ValueError:
        row = None
    if not isinstance(row, dict):
        return Entry(offset, len(line), digest, chain, 'unreadable')
    try:
        return _fields(offset, line, digest, chain, row)
    except (AttributeError, KeyError, TypeError, ValueError):
        return Entry(offset, len(line), digest, chain, 'unreadable')


def _fields(offset, line, digest, chain, row):
    kind = str(row.get('type') or '?') + (f"/{row['subtype']}" if isinstance(row.get('subtype'), str) else '')
    message = row.get('message') if isinstance(row.get('message'), dict) else {}
    attachment = row.get('attachment') if isinstance(row.get('attachment'), dict) else {}
    origin = row.get('origin') if isinstance(row.get('origin'), dict) else attachment.get('origin')
    content = message.get('content', row.get('content'))
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    usage = message.get('usage') if row.get('type') == 'assistant' and isinstance(message.get('usage'), dict) else None
    preserved = _map(_map(row.get('compactMetadata')).get('preservedMessages')).get('uuids')
    string = lambda value: value if isinstance(value, str) and value else None
    head = _head(row, content, attachment)
    return Entry(
        offset, len(line), digest, chain, kind, string(row.get('uuid')), string(row.get('parentUuid')),
        string(row.get('logicalParentUuid')), string(row.get('timestamp')), string(row.get('version')),
        string(row.get('promptId')), string(origin.get('kind')) if isinstance(origin, dict) else None,
        row.get('isMeta') is True, row.get('isCompactSummary') is True, row.get('isSidechain') is True,
        string(attachment.get('type')), string(attachment.get('commandMode')), head,
        any(block.get('type') == 'tool_result' for block in blocks), 'toolUseResult' in row,
        tuple(block['tool_use_id'] for block in blocks if block.get('type') == 'tool_result' and isinstance(block.get('tool_use_id'), str)),
        string(message.get('id')) if row.get('type') == 'assistant' else None,
        tuple(block['id'] for block in blocks if row.get('type') == 'assistant' and block.get('type') == 'tool_use' and isinstance(block.get('id'), str)),
        usage if usage and any(counted(usage, key) for key in USAGE_KEYS) else None,
        string(message.get('stop_reason')) if row.get('type') == 'assistant' else None,
        tuple(uuid for uuid in preserved if isinstance(uuid, str)) if isinstance(preserved, list) else (),
        string(row.get('sourceToolUseID')) if row.get('isMeta') is True and (row.get('turnCompanion') or row.get('sourceToolUseID')) else None,
        string(message.get('model')) if row.get('type') == 'assistant' else None,
        # Claude reads a turn's effort as perTurnEffort when the row has the key (null: none), else effort.
        string(row['perTurnEffort'] if 'perTurnEffort' in row else row.get('effort')) if row.get('type') == 'assistant' else None,
        string(row.get('sessionId')), 'sessionId' in row, _mark(head))


def _head(row, content, attachment):
    """The start of a user or command row's text, as labelling, previews and command names read it."""
    text = _text(content) if row.get('type') in ('user', 'system') else ''
    if attachment.get('type') == 'queued_command' and isinstance(attachment.get('prompt'), str):
        text = attachment['prompt']
    return text.lstrip()[:200]


def _mark(head):
    """The one thing labelling needs from a row's text: which of Claude's markers it starts with, if any."""
    if head.startswith(CAVEAT):
        return 'caveat'
    if head.startswith(COMMAND_OUTPUT):
        return 'output'
    if head.startswith('<command-name>'):
        return 'command-name'
    if head.startswith('/compact'):
        return 'compact'
    if head.startswith('/'):
        return 'slash'
    if head.startswith(INTERRUPTED):
        return 'interrupted'
    return ''


INTERRUPTED = '[Request interrupted by user'  # Claude's marker for an Esc: it ends the turn, and usually no turn_duration follows
COMMAND_OUTPUT = ('<local-command-stdout>', '<local-command-stderr>', '<local-command-caveat>')
CAVEAT = '<local-command-caveat>'  # the first of the user rows Claude stores a local command as (2.1.332: /reload-plugins)


def labels(entries, check=None):
    """(label, source) for each entry, by Claude's rules (docs/design/transcript-protocols.md, Labels)."""
    out, seen, command, span = [], set(), None, False  # command: the promptId of the latest typed command
    caveat = None  # (promptId, ref) of the local command a caveat opened: its '<command-name>' row is its output
    outputs = set()  # the refs of command output rows: a meta row under one is the command's report (/context's)
    tick = ticker(check)
    for item in entries:
        if tick is not None:
            tick()
        label, source = 'meta', None
        new = item.prompt_id is not None and item.prompt_id not in seen
        if item.kind == 'system/compact_boundary':
            label, span = 'compaction', True
        elif span and (item.kind == 'assistant' and item.usage or item.kind == 'user' and item.origin
                       or item.tool_result and item.tool_use_result):
            span = False  # the first real row ends what a compaction stored
        if label == 'compaction':
            pass
        elif item.summary:
            label = 'compaction'
        elif item.kind == 'user':
            # Where Claude stores a local command as user rows, its caveat is its first row: the command, whatever its
            # origin (a command mv2 runs has it stamped); the '<command-name>' row after it is its output, matched by
            # promptId or by the caveat being its parent (2.1.332 stores /clear's caveat with no promptId, and its
            # name row with another or none). A caveat a compaction stored inside its span keeps the label it had, so
            # its command row stays the turn's.
            if item.mark == 'caveat' and item.meta and not span:
                label, command = 'command', item.prompt_id
                caveat = (item.prompt_id, item.ref)
            elif item.mark == 'command-name' and caveat is not None and (
                    item.prompt_id is not None and item.prompt_id == caveat[0]
                    or item.parent is not None and item.parent == caveat[1]):
                label = 'command_output'
            elif item.origin:
                label, source = 'prompt', item.origin
            elif item.tool_result:
                label = 'compaction' if span else 'tool_result'
            elif item.mark in ('output', 'caveat') or item.mark == 'command-name' and item.prompt_id == command:
                label = 'command_output'
            elif item.mark == 'command-name':
                label, command = 'command', item.prompt_id
            elif item.mark in ('slash', 'compact') and new and not item.meta:
                label, command = 'command', item.prompt_id
            elif item.meta and item.parent is not None and item.parent in outputs:
                label = 'command_output'
            elif item.companion or item.mark == 'interrupted' or item.meta:
                label = 'injected'
            elif span:
                label = 'compaction'
            elif new:
                label = 'prompt'  # a Claude without origin: a new promptId with content, not meta, not a command
            else:
                label = 'injected'
        elif item.kind == 'attachment':
            label = 'mid_turn' if item.attachment == 'queued_command' and item.command_mode == 'prompt' else 'injected'
            source = item.origin if label == 'mid_turn' else None
        elif item.kind == 'system/local_command':
            label = 'command_output' if item.mark in ('output', 'caveat') else 'command'  # a system caveat is output
        elif item.kind == 'assistant':
            label = 'compaction' if span else 'response'
        if item.prompt_id:
            seen.add(item.prompt_id)
        if label == 'command_output' and item.ref:
            outputs.add(item.ref)
        out.append((label, source))
    return out


def ends_turn(item):
    """A native turn-end marker: Claude's turn_duration row, or its interruption row."""
    return item.kind == 'system/turn_duration' or item.kind == 'user' and item.mark == 'interrupted'


@dataclass(frozen=True)
class History:
    """The conversation's rows (indexes into the entries), oldest first, from `tip` back across every compaction;
    a row without a ref is on it when the nearest row with a ref before it is.

    `gap` is a parent Claude never stored (its writes failed for a while), or a link back into rows the walk already
    passed: an automatic compaction's boundary links to a row Claude stored after its summary. Behind Claude's own
    compaction a gap doesn't matter, and the history starts at Claude's summary. The rows a compaction keeps
    (`preservedMessages`) come right after its summary, as Claude rebuilds the session, though on disk they keep their
    old parents before the boundary. `tip` is the ref asked for, else the newest leaf the walk started from.
    """
    rows: tuple
    tip: str | None
    gap: str | None = None
    summary: str | None = None


def _summaries(by_ref, order):
    """{boundary ref: the summary Claude wrote for it}. Claude stores its summary under the boundary, or, for a
    compaction run as a turn starts, under the turn's attachment rows (instructions, session context, the date)
    stored between them; a summary reached only through a message row is not that boundary's."""
    found = {}
    for ref in sorted((ref for ref, item in by_ref.items() if item.summary), key=order.get):
        link, passed = by_ref[ref].parent, set()
        while link in by_ref and by_ref[link].kind == 'attachment' and link not in passed:
            passed.add(link)
            link = by_ref[link].parent
        if link in by_ref and by_ref[link].kind == 'system/compact_boundary':
            found.setdefault(link, ref)
    return found


def _keeping(by_ref, order, summaries, tip):
    """The latest of Claude's own compactions stored after `tip` that kept it, or None: the live session then holds
    `tip` after that compaction's summary, so its history is read from the summary."""
    found = [item for ref, item in by_ref.items() if item.kind == 'system/compact_boundary' and ref in summaries
             and tip in item.preserved]
    return max(found, key=lambda item: order[item.ref]) if found else None


def walk(entries, tip=None, labelled=None, check=None):
    """The history: from `tip` when stored, else the newest leaf of the parent graph (the last stored: a batch of
    rows, such as a compaction's install, can carry older timestamps than a row before it), back through parent links and across a boundary by its logical parent, plus every block of each response it
    passes and every result answering one of their calls (parallel tool results branch off their own call), and
    the typed /compact row a boundary answers (the boundary's logical parent is the row before it)."""
    labelled = labelled if labelled is not None else labels(entries, check)
    tick = ticker(check)
    by_ref, order = {}, {}
    for index, item in enumerate(entries):
        if tick is not None:
            tick()
        if not item.ref or item.sidechain:
            continue
        by_ref[item.ref], order[item.ref] = item, index
    # An automatic compaction's boundary links forward, to a row Claude stores after its summary: until a later row
    # hangs off that one, it is the newest row, so the link must not take it off the leaves.
    parented = {item.parent for item in by_ref.values() if item.parent}
    parented |= {item.logical for ref, item in by_ref.items() if item.logical and order.get(item.logical, -1) < order[ref]}
    leaves = [ref for ref in by_ref if ref not in parented]
    if not leaves:
        return History((), None)
    summaries = _summaries(by_ref, order)
    current = tip if tip in by_ref else max(leaves, key=order.get)
    start = current
    # A tip a later compaction kept (its kept reply, right after an idle or automatic compaction): read from that
    # compaction's summary, so the boundary and the summary are on the history and rows stored after it aren't.
    keeping = _keeping(by_ref, order, summaries, tip) if tip in by_ref else None
    if keeping is not None:
        current = summaries[keeping.ref]
    walked, seen, claude, boundaries = [], set(), None, []
    while current in by_ref and current not in seen:
        seen.add(current)
        walked.append(current)
        item = by_ref[current]
        if item.kind == 'system/compact_boundary':
            boundaries.append(item)
            if item.ref in summaries:
                claude = (len(walked), item)
            current = item.logical
        else:
            current = item.parent
    gap = current or None  # never stored, or already walked: the walk can't go on either way
    position = {ref: float(index) for ref, index in order.items()}
    summary = None
    if gap and claude is not None:
        count, boundary = claude
        summary = summaries[boundary.ref]
        seen = set(walked[:count]) | {ref for ref in boundary.preserved if ref in by_ref}
        gap = None
    else:
        seen = set(walked)
    # A compaction's kept rows are in the live session, wherever their old parents lead.
    for boundary in boundaries:
        if summaries.get(boundary.ref) in seen:
            seen |= {ref for ref in boundary.preserved if ref in by_ref}
    responses = {by_ref[ref].response for ref in seen} - {None}
    seen |= {ref for ref, item in by_ref.items() if item.response in responses
             and (summary is None or position[ref] > position[summary] or ref in seen)}
    calls = {call for ref in seen for call in by_ref[ref].calls}
    seen |= {ref for ref, item in by_ref.items() if item.kind == 'user' and set(item.answers) & calls}
    # A compaction's kept rows come right after its summary, as Claude delivers them: once every row is in (a response's
    # other blocks and parallel results join after the walk), and the newest compaction first, as the live session
    # holds a row kept twice after the later summary.
    placed = set()
    for boundary in boundaries:
        note = summaries.get(boundary.ref)
        kept = [ref for ref in boundary.preserved if ref in seen and ref not in placed]
        if note in seen and kept:
            for index, ref in enumerate(kept, 1):
                position[ref] = order[note] + index / (len(kept) + 1)
            placed.update(kept)
    for boundary in boundaries:  # the typed /compact row the boundary answers stays: the person typed it
        if boundary.ref not in seen:
            continue
        before = order[boundary.ref]
        after = order.get(boundary.logical, -1)
        typed = [index for index in range(after + 1, before) if labelled[index][0] == 'command'
                 and entries[index].mark == 'compact' and entries[index].ref]
        if typed:
            seen.add(entries[typed[-1]].ref)
    # A row without a ref (titles, modes, API request records, ...) belongs to the branch of the nearest row with a
    # ref before it in the file, and comes right after that row: stored order is conversation order.
    if check is not None:
        check()  # the passes above are one each over the rows, about 0.1 s for a large transcript
    keys, owner = {order[ref]: (position[ref], 0, order[ref]) for ref in seen if ref in order}, None
    for index, item in enumerate(entries):
        if tick is not None:
            tick()
        if item.ref and not item.sidechain:
            owner = item.ref
        elif not item.ref and not item.sidechain and (owner is None or owner in seen):
            keys[index] = (position[owner] if owner else -1.0, 1, index)
    rows = tuple(sorted(keys, key=keys.get))
    return History(rows, start, gap, summary)


def in_context(entries, history):
    """Indexes of the history's rows from its latest compaction on (Claude's summary, or a boundary and what a
    hook stored after it); every history row when the session never compacted."""
    rows = list(history.rows)
    marks = [i for i, index in enumerate(rows) if entries[index].kind == 'system/compact_boundary']
    return set(rows[marks[-1]:]) if marks else set(rows)


def context_from(entries, history):
    """The index into the history of the first row in context: its latest compaction boundary (0 when none)."""
    marks = [i for i, index in enumerate(history.rows) if entries[index].kind == 'system/compact_boundary']
    return marks[-1] if marks else 0


class CantTell(Exception):
    """A read that can't say what the transcript holds: copies that disagree, or storage it can't read."""


class _Stopped(Exception):
    """A read's check raised: carried past the handlers that turn an OSError into unreadable storage, so a deadline
    is never mistaken for one, whatever the check raises."""
    def __init__(self, error):
        super().__init__(error)
        self.error = error


def _carried(check):
    if check is None:
        return None

    def carried():
        try:
            check()
        except Exception as error:
            raise _Stopped(error) from error
    return carried


@contextlib.contextmanager
def _holding(lock, check):
    """The lock, taken under the read's deadline: a read past it never waits on another read holding the lock."""
    if check is None:
        with lock:
            yield
        return
    check()
    while not lock.acquire(timeout=LOCK_WAIT):
        check()
    try:
        yield
    finally:
        lock.release()


LOCK_WAIT = .01  # seconds between deadline checks while another read holds the shared index lock


def newest_copy(native_id, environment, workspaces=(), check=None, store=None):
    """_newest_copy, raising whatever `check` raised as itself. `store`: the agent's ROW_STORE folder."""
    try:
        return _newest_copy(native_id, environment, workspaces, check, store)
    except _Stopped as stopped:
        raise stopped.error from None


def _newest_copy(native_id, environment, workspaces=(), check=None, store=None):
    """The index of the session's newest copy, read up to date, for the transcript reads. Older copies must hold a
    uuid prefix of the newest copy's rows (Claude moves a transcript by renaming it, so more than one copy is an
    anomaly), or CantTell names them. Usage reads the newest copy without this check or the full prefix proof."""
    carried = _carried(check)
    try:
        paths = session_transcripts(native_id, environment, workspaces, check=carried)
    except Unreadable as error:
        raise CantTell(f"Claude's storage folder {error.path} can't be read: restore read access to it") from error
    if not paths:
        return None
    with _holding(_transcripts_lock, carried):
        try:
            indexes = [_index(path, rows=True, store=store, check=carried).read(prove=True, check=carried)
                       for path in paths]
        except (OSError, Unreadable) as error:
            raise CantTell(f"Claude's transcript can't be read: {error}") from error
        newest = indexes[-1]
        refs = [item.ref for item in newest.entries if item.ref]
        for older in indexes[:-1]:
            mine = [item.ref for item in older.entries if item.ref]
            if refs[:len(mine)] != mine:
                raise CantTell(f'Copies of this session disagree: {older.path} is not part of {newest.path}')
        if getattr(newest, 'copies', None) != paths:  # a change of newest copy is a rewrite for readers
            if getattr(newest, 'copies', None) is not None:
                newest.history_version = next(_versions)
            newest.copies = paths
        return newest


@dataclass(frozen=True)
class Analysis:
    count: int
    labels: tuple
    history: History
    on: frozenset          # entry indexes on the history
    context: frozenset     # entry indexes in context
    version: int           # rises when the history changes other than by growing at its end
    epoch: int = 0         # the index's epoch these rows were read in (Transcript._reset starts a new one)


def _on_history(history):
    return frozenset(history.rows)


def _history_refs(entries, history):
    return tuple(entries[i].ref for i in history.rows)


def analyse(index, tip=None, check=None):
    """Labels, history and context for the index as read, cached per index until it grows or is rewritten. `check`
    is called through it; past a deadline it raises, and nothing is cached or changed."""
    # The epoch and the rows together: rows only grow within an epoch, so (epoch, count) names exactly these rows,
    # and a save can tell an analysis of other rows of the same count apart.
    epoch, entries = index.epoch, index.entries[:]
    key = (len(entries), epoch, tip)
    cached = getattr(index, 'analysis', None)
    if cached is not None and cached[0] == key:
        return cached[1]
    step = check if check is not None else (lambda: None)
    step()
    labelled = tuple(labels(entries, check))
    step()
    history = walk(entries, tip, labelled, check)
    step()
    on = _on_history(history)
    step()
    context = frozenset(in_context(entries, history))
    step()
    refs = _history_refs(entries, history)
    previous = getattr(index, 'history_refs', None)
    # A rewrite, a rewind that left rows of the history behind, or a compaction that put rows before ones already
    # read (its boundary and summary come before the rows it kept).
    rewritten = tip is None and previous is not None and (
        getattr(index, 'history_rewrites', index.rewrites) != index.rewrites or refs[:len(previous)] != previous)
    step()  # last, with everything decided: nothing below waits, and nothing above changed the index
    version = next(_versions) if rewritten else getattr(index, 'history_version', 0)
    if tip is None:
        index.history_version, index.history_refs, index.history_rewrites = version, refs, index.rewrites
    result = Analysis(len(entries), labelled, history, on, context, version, epoch)
    index.analysis = (key, result)
    return result


def body(path, offset, size, cap):
    """A row's data as stored, a fresh object per call; None over `cap` bytes or when it doesn't parse."""
    if size > cap:
        return None
    with open_regular(path) as handle:
        handle.seek(offset)
        raw = handle.read(size)
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None



def position_of(item):
    """The provider's position after a row: the offset there and a hash chained over every line up to it, so it
    changes whenever any byte before it changes (an equal-length rewrite of an earlier row included) and never on an
    append. Core adds the nearest ref and makes the cursor."""
    return f'{item.offset + item.size}:{item.chain}'


def command_of(head):
    """(name, args) of a command row's text: a typed '/name args', or Claude's <command-name>/<command-args> echo."""
    if head.startswith('/'):
        name, _, args = head.partition(' ')
        return name, args.strip() or None
    name = re.search(r'<command-name>(.*?)</command-name>', head)
    args = re.search(r'<command-args>(.*?)</command-args>', head)
    return (name.group(1).strip() if name else None), (args.group(1).strip() or None if args else None)
