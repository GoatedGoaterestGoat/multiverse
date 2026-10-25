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
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
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


TAIL = 1 << 16  # how much of what an index already read it checks again when the file changed


class Transcript:
    """One copy's index, read once and then as it grows: an Entry per complete line (metadata only, never bodies),
    and the usage readers' totals from it.

    Claude writes one record per content block of a response, each with the response's usage, so usage is counted
    once per response ID. A row with zero usage is never a response: a compaction that keeps a row stores it again
    with zero usage, and an installed compaction's rows arrive as new IDs with zero usage (Claude's `<synthetic>`
    model). Neither replaces a real response's usage or becomes the latest request, so `last` stays the latest real
    response.

    Claude isn't append-only: it truncates a file at a tombstoned row and writes the rest back in place, and can
    rewrite a whole file. So whenever the file changed since the last read (its size or modification time), the index
    checks the last `TAIL` bytes it already read, which such a rewrite moves, and starts over when they differ (or on
    a new inode, or a smaller file). That keeps usage, which reads on every listing, at one parse per new byte. A
    transcript read (`read(prove=True)`) also proves every byte already indexed unchanged, against a running hash of
    them, once per change of the file, so a position's chained hash stays exact. `rewrites` counts the restarts. An
    unchanged file costs no read, and an append never changes an earlier row's position.
    """

    def __init__(self, path):
        self.path, self.rewrites = path, 0
        self._reset(None)

    def _reset(self, inode):
        self.inode, self.offset, self.entries, self.responses, self.last = inode, 0, [], {}, None
        self.stamp, self.proven, self.prefix, self.tail = None, None, hashlib.sha256(), b''

    def _tail_intact(self, handle):
        """Whether the last bytes already indexed are still exactly what was read."""
        handle.seek(self.offset - len(self.tail))
        return handle.read(len(self.tail)) == self.tail

    def _intact(self, handle):
        """Whether the bytes already indexed are still exactly what was read."""
        digest, left = hashlib.sha256(), self.offset
        handle.seek(0)
        while left:
            chunk = handle.read(min(left, 1 << 22))
            if not chunk:
                return False
            digest.update(chunk)
            left -= len(chunk)
        return digest.digest() == self.prefix.digest()

    def read(self, *, prove=False):
        """Read up to date. `prove`: also prove every byte indexed earlier unchanged, if the file changed since the
        last proof; `self.proven` is the file's stamp then (or when the index was read whole)."""
        with open_regular(self.path) as handle:
            status = os.fstat(handle.fileno())
            stamp = (status.st_mtime_ns, status.st_size)
            if status.st_ino != self.inode or status.st_size < self.offset:
                stale = True
            elif not self.offset:
                stale = False
            elif prove and stamp != self.proven:
                stale = not self._intact(handle)
            else:
                stale = stamp != self.stamp and not self._tail_intact(handle)
            if stale:
                if self.inode is not None:
                    self.rewrites += 1
                self._reset(status.st_ino)
            start = self.offset
            handle.seek(self.offset)
            data = handle.read()
        end = data.rfind(b'\n') + 1  # an unfinished last line waits for its newline
        # Parse the new lines first and change nothing until all of them parsed: a failure leaves the index as it
        # was, so the next read starts from the same place and never counts a row twice.
        added, responses, last, position = [], {}, self.last, self.offset
        chain = self.entries[-1].chain if self.entries else ''
        for line in data[:end].split(b'\n')[:-1]:
            item = entry(position, line + b'\n', chain)
            added.append(item)
            if item.kind == 'assistant' and item.usage is not None and item.response:
                responses[item.response] = item.usage
                last = item.at or last
            position, chain = position + item.size, item.chain
        self.entries.extend(added)
        self.responses.update(responses)
        self.last = last
        self.prefix.update(data[:end])
        self.tail = (self.tail + data[:end])[-TAIL:]
        self.offset += end
        self.stamp = stamp
        if prove or not start:
            self.proven = stamp
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


# What a reader needs to know about one complete line of a copy, without its body: where it is, a hash of its
# bytes for cursors, and the fields labels, the history walk and usage read. Built once per line.
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
    text = _text(content) if row.get('type') in ('user', 'system') else ''
    if attachment.get('type') == 'queued_command' and isinstance(attachment.get('prompt'), str):
        text = attachment['prompt']
    string = lambda value: value if isinstance(value, str) and value else None
    return Entry(
        offset, len(line), digest, chain, kind, string(row.get('uuid')), string(row.get('parentUuid')),
        string(row.get('logicalParentUuid')), string(row.get('timestamp')), string(row.get('version')),
        string(row.get('promptId')), string(origin.get('kind')) if isinstance(origin, dict) else None,
        row.get('isMeta') is True, row.get('isCompactSummary') is True, row.get('isSidechain') is True,
        string(attachment.get('type')), string(attachment.get('commandMode')), text.lstrip()[:200],
        any(block.get('type') == 'tool_result' for block in blocks), 'toolUseResult' in row,
        tuple(block['tool_use_id'] for block in blocks if block.get('type') == 'tool_result' and isinstance(block.get('tool_use_id'), str)),
        string(message.get('id')) if row.get('type') == 'assistant' else None,
        tuple(block['id'] for block in blocks if row.get('type') == 'assistant' and block.get('type') == 'tool_use' and isinstance(block.get('id'), str)),
        usage if usage and any(counted(usage, key) for key in USAGE_KEYS) else None,
        string(message.get('stop_reason')) if row.get('type') == 'assistant' else None,
        tuple(uuid for uuid in preserved if isinstance(uuid, str)) if isinstance(preserved, list) else (),
        string(row.get('sourceToolUseID')) if row.get('isMeta') is True and (row.get('turnCompanion') or row.get('sourceToolUseID')) else None)


INTERRUPTED = '[Request interrupted by user'  # Claude's marker for an Esc: it ends the turn, and usually no turn_duration follows
COMMAND_OUTPUT = ('<local-command-stdout>', '<local-command-stderr>', '<local-command-caveat>')


def labels(entries):
    """(label, source) for each entry, by Claude's rules (docs/design/transcript-protocols.md, Labels)."""
    out, seen, command, span = [], set(), None, False  # command: the promptId of the latest typed command
    for item in entries:
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
            if item.origin:
                label, source = 'prompt', item.origin
            elif item.tool_result:
                label = 'compaction' if span else 'tool_result'
            elif item.head.startswith(COMMAND_OUTPUT) or item.head.startswith('<command-name>') and item.prompt_id == command:
                label = 'command_output'
            elif item.head.startswith('<command-name>'):
                label, command = 'command', item.prompt_id
            elif item.head.startswith('/') and new and not item.meta:
                label, command = 'command', item.prompt_id
            elif item.companion or item.head.startswith(INTERRUPTED) or item.meta:
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
            label = 'command_output' if item.head.startswith(COMMAND_OUTPUT) else 'command'
        elif item.kind == 'assistant':
            label = 'compaction' if span else 'response'
        if item.prompt_id:
            seen.add(item.prompt_id)
        out.append((label, source))
    return out


def ends_turn(item):
    """A native turn-end marker: Claude's turn_duration row, or its interruption row."""
    return item.kind == 'system/turn_duration' or item.kind == 'user' and item.head.startswith(INTERRUPTED)


@dataclass(frozen=True)
class History:
    """The conversation's rows (indexes into the entries), oldest first, from `tip` back across every compaction;
    a row without a ref is on it when the nearest row with a ref before it is.

    `gap` is a parent Claude never stored (its writes failed for a while); behind Claude's own compaction a gap
    doesn't matter, and the history starts at Claude's summary. `tip` is the ref the walk started from.
    """
    rows: tuple
    tip: str | None
    gap: str | None = None
    summary: str | None = None


def walk(entries, tip=None, labelled=None):
    """The history: from `tip` when stored, else the newest leaf of the parent graph,
    back through parent links and across a boundary by its logical parent, plus every block of each response it
    passes and every result answering one of their calls (parallel tool results branch off their own call), and
    the typed /compact row a boundary answers (the boundary's logical parent is the row before it)."""
    labelled = labelled if labelled is not None else labels(entries)
    by_ref, parented, order = {}, set(), {}
    for index, item in enumerate(entries):
        if not item.ref or item.sidechain:
            continue
        by_ref[item.ref], order[item.ref] = item, index
        for link in (item.parent, item.logical):
            if link:
                parented.add(link)
    leaves = [ref for ref in by_ref if ref not in parented]
    if not leaves:
        return History((), None)
    summaries = {item.parent: item.ref for item in by_ref.values() if item.summary and item.parent}
    current = tip if tip in by_ref else max(leaves, key=lambda ref: (by_ref[ref].at or '', order[ref]))
    start, walked, seen, claude, boundaries = current, [], set(), None, []
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
    gap = current or None
    position = {ref: float(index) for ref, index in order.items()}
    summary = None
    if gap and claude is not None:
        count, boundary = claude
        summary = summaries[boundary.ref]
        kept = [ref for ref in boundary.preserved if ref in by_ref]
        seen = set(walked[:count]) | set(kept)
        for index, ref in enumerate(kept, 1):  # after the summary, as Claude delivers them
            position[ref] = order[summary] + index / (len(kept) + 1)
        gap = None
    responses = {by_ref[ref].response for ref in seen} - {None}
    seen |= {ref for ref, item in by_ref.items() if item.response in responses
             and (summary is None or position[ref] > position[summary] or ref in seen)}
    calls = {call for ref in seen for call in by_ref[ref].calls}
    seen |= {ref for ref, item in by_ref.items() if item.kind == 'user' and set(item.answers) & calls}
    for boundary in boundaries:  # the typed /compact row the boundary answers stays: the person typed it
        if boundary.ref not in seen:
            continue
        before = order[boundary.ref]
        after = order.get(boundary.logical, -1)
        typed = [index for index in range(after + 1, before) if labelled[index][0] == 'command'
                 and entries[index].head.startswith('/compact') and entries[index].ref]
        if typed:
            seen.add(entries[typed[-1]].ref)
    # A row without a ref (titles, modes, API request records, ...) belongs to the branch of the nearest row with a
    # ref before it in the file, and comes right after that row: stored order is conversation order.
    keys, owner = {order[ref]: (position[ref], 0, order[ref]) for ref in seen if ref in order}, None
    for index, item in enumerate(entries):
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


def newest_copy(native_id, environment, workspaces=()):
    """The index of the session's newest copy, read up to date, for the transcript reads. Older copies must hold a
    uuid prefix of the newest copy's rows (Claude moves a transcript by renaming it, so more than one copy is an
    anomaly), or CantTell names them. Usage reads the newest copy without this check or the full prefix proof."""
    try:
        paths = session_transcripts(native_id, environment, workspaces)
    except Unreadable as error:
        raise CantTell(f"Claude's storage folder {error.path} can't be read: restore read access to it") from error
    if not paths:
        return None
    with _transcripts_lock:
        try:
            indexes = [_transcripts.setdefault(path, Transcript(path)).read(prove=True) for path in paths]
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
                newest.history_version = getattr(newest, 'history_version', 0) + 1
            newest.copies = paths
        return newest


@dataclass(frozen=True)
class Analysis:
    count: int
    labels: tuple
    history: History
    on: frozenset          # entry indexes on the history
    context: frozenset     # entry indexes in context
    version: int           # rises when the history changes other than by growing


def analyse(index, tip=None):
    """Labels, history and context for the index as read, cached per index until it grows or is rewritten."""
    key = (len(index.entries), index.rewrites, tip)
    cached = getattr(index, 'analysis', None)
    if cached is not None and cached[0] == key:
        return cached[1]
    entries = index.entries
    labelled = tuple(labels(entries))
    history = walk(entries, tip, labelled)
    on = frozenset(history.rows)
    context = frozenset(in_context(entries, history))
    refs = tuple(entries[i].ref for i in history.rows)
    version = getattr(index, 'history_version', 0)
    previous = getattr(index, 'history_refs', None)
    if tip is None:
        if previous is not None and (getattr(index, 'history_rewrites', index.rewrites) != index.rewrites
                                     or not set(previous) <= set(refs)):
            version += 1  # a rewrite, or a rewind that left rows of the history behind
        index.history_version, index.history_refs, index.history_rewrites = version, refs, index.rewrites
    result = Analysis(len(entries), labelled, history, on, context, version)
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
