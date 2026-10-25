"""Finding, opening and reading a Claude session's transcript copies, for every reader in the adapter.

A session's transcript is `<Claude home>/projects/<folder>/<session>.jsonl`. Claude names the folder after the
directory the session was launched in, so a session normally has one copy; a resume from elsewhere can leave
more. Readers get two views of them:

- the newest copy, read incrementally (`Transcript`): usage and the context reading;
- raw copies (`raw_rows`): every copy, every non-empty line parsed or marked `DAMAGED`, fields as stored, never
  deduplicated, for the evidence readers, which look for copies of one row that disagree.

Every file is opened through `open_regular`, and any part of the storage that could hold the session but can't
be read raises `Unreadable` rather than read as absence.
"""
import json
import os
from pathlib import Path
import stat
import threading
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


PROJECT_KEY_LENGTH = 200  # past this, Claude shortens a project folder's name and adds a hash only Bun computes


def project_key(path):
    """Claude's project folder name for a launch directory: the NFC path, every UTF-16 code unit that isn't
    an ASCII letter or digit replaced by '-', as Bun does (so a character outside the BMP gives '--')."""
    return ''.join(c if c.isascii() and c.isalnum() else '-' * (2 if ord(c) > 0xFFFF else 1)
                   for c in unicodedata.normalize('NFC', str(path)))


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


def session_transcripts(native_id, environment=None, workspaces=(), links=False):
    """Every main transcript of `native_id` (`projects/*/<id>.jsonl`), oldest first.

    Raises Unreadable when a folder can't be listed or searched and could hold the session (a dangling link, or a
    folder gone before it was searched, counts as one that can't be searched): Claude files it
    under its launch directory's name, so with `workspaces` (the launch's own, never re-read) an unreadable
    folder of another workspace is skipped. pathlib's glob skips every unreadable folder silently, so a scan
    that finds nothing would otherwise read as proof the transcript is absent.
    """
    return _scan_transcripts(native_id, environment, workspaces, links)[0]


def _scan_transcripts(native_id, environment=None, workspaces=(), links=False):
    """session_transcripts, and whether this call listed the projects folder itself (False when it is missing)."""
    projects = claude_home(environment) / 'projects'
    try:
        entries = list(os.scandir(projects))
    except FileNotFoundError:
        return [], False
    except OSError as error:
        raise Unreadable(projects) from error
    found, files, keys = [], set(), project_keys(workspaces)
    for entry in entries:
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


class Transcript:
    """Usage recorded in one transcript file, read once and then as it grows.

    Claude writes one record per content block of a response, each with the
    response's usage, so usage is counted once per response ID. A row with zero
    usage is never a response: a compaction that keeps a row stores it again with
    zero usage, and an installed compaction's rows arrive as new IDs with zero
    usage (Claude's `<synthetic>` model). Neither replaces a real response's usage
    or becomes the latest request, so `last` stays the latest real response.
    """

    def __init__(self, path):
        self.path, self.inode, self.offset, self.responses, self.last = path, None, 0, {}, None

    def read(self):
        status = self.path.stat()
        if status.st_ino != self.inode or status.st_size < self.offset:
            self.inode, self.offset, self.responses, self.last = status.st_ino, 0, {}, None
        with open_regular(self.path) as handle:
            handle.seek(self.offset)
            data = handle.read()
        end = data.rfind(b'\n') + 1  # an unfinished last line waits for its newline
        for line in data[:end].splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get('message') if isinstance(record, dict) and record.get('type') == 'assistant' else None
            if isinstance(message, dict) and isinstance(message.get('usage'), dict) and message.get('id'):
                if not any(counted(message['usage'], key) for key in USAGE_KEYS):
                    continue
                self.responses[message['id']] = message['usage']
                self.last = record.get('timestamp') or self.last
        self.offset += end
        return self


_transcripts = {}
_transcripts_lock = threading.Lock()


def counted(usage, key):
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def context_reading(native_id, workspaces=(), environment=None):
    """(tokens, timestamp) of the latest main-thread request, or None.

    Tokens are input + cache written + cache read. Subagent transcripts are
    excluded: they have their own context windows.
    """
    mains = session_transcripts(native_id, environment, workspaces)
    if not mains:
        return None
    with _transcripts_lock:
        transcript = _transcripts.setdefault(mains[-1], Transcript(mains[-1])).read()
        responses, stamp = transcript.responses, transcript.last
    if not responses:
        return None
    latest = list(responses.values())[-1]
    return sum(counted(latest, key) for key in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')), stamp


def context_usage(native_id):
    """Tokens in the latest main-thread request, or None."""
    reading = context_reading(native_id)
    return reading[0] if reading else None


def measured_usage(native_id, workspaces=(), environment=None):
    """Cumulative usage of the session and its subagents, from their transcripts. Raises Unreadable rather
    than total a scan that skipped part of the storage."""
    mains = session_transcripts(native_id, environment, workspaces)
    if not mains:
        return None
    paths = [mains[-1], *subagent_transcripts(mains[-1])]
    with _transcripts_lock:
        transcripts = [_transcripts.setdefault(path, Transcript(path)).read() for path in paths]
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
