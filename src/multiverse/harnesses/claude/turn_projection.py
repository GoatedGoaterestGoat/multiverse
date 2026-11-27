"""Turn cursor evidence from the metadata of a proved Claude transcript."""

import hashlib
import json
import os
import stat
from itertools import chain
from pathlib import Path

from ..transcript_groups import turn_anchors
from ..types import TurnCursorProjection
from . import transcripts as rows

CHUNK = 1 << 20
MAX_APPEND = 1 << 20
COLUMNS = {'ref', 'label', 'codes', 'history'}


class _TranscriptChanged(Exception):
    """An append or replacement raced the proof."""


def _saved(stream, path, check):
    """Verify every saved section, decoding only the columns used by turn projection."""
    check()
    length = os.fstat(stream.fileno()).st_size
    if length > rows.ROW_STORE_BYTES:
        return None
    header = json.loads(stream.readline(65536))
    if (type(header['schema']) is not int or header['schema'] != rows.ROW_SCHEMA
            or type(header['derivation']) is not int or header['derivation'] != rows.ROW_DERIVATION
            or header['path'] != str(path)
            or any(type(header[key]) is not int or header[key] <= 0 for key in ('inode', 'offset', 'rows'))):
        return None
    sections, columns = {}, {}
    for name, size, digest in header['sections']:
        if name in sections or type(size) is not int or not 0 < size <= length - stream.tell():
            return None
        sections[name] = stream.tell(), size, digest
        proof = hashlib.sha256()
        data = bytearray()
        remaining = size
        while remaining:
            check()
            chunk = stream.read(min(remaining, CHUNK))
            if not chunk:
                return None
            proof.update(chunk)
            if name in COLUMNS:
                data.extend(chunk)
            remaining -= len(chunk)
        if proof.hexdigest() != digest:
            return None
        if name in COLUMNS:
            check()
            columns[name] = json.loads(data)
    check()
    if (set(sections) != {*rows._ROW_FIELDS, 'label', 'codes', 'history'}
            or stream.tell() != length or stream.read(1)):
        return None
    count = header['rows']
    if (any(not isinstance(column, list) for column in columns.values())
            or any(len(columns[key]) != count for key in COLUMNS - {'codes', 'history'})
            or not isinstance(header['chain'], str)):
        return None
    return header, sections, columns


def _column(stream, sections, name, count, check):
    """Read an extra column to prove an append, without loading the other row facts."""
    offset, length, digest = sections[name]
    check()
    stream.seek(offset)
    data = stream.read(length)
    check()
    if len(data) == length and hashlib.sha256(data).hexdigest() == digest:
        values = json.loads(data)
        if isinstance(values, list) and len(values) == count:
            return values
    raise ValueError('Invalid Claude projection column')


def _positions(stream, sections, name, count, selected, check):
    values = _column(stream, sections, name, count, check)
    return {i: values[i] for i in selected | {count - 1}}


def _proof_and_tail(path, header, check):
    """Prove the saved prefix and keep a bounded suffix from the same transcript copy."""
    check()
    with rows.open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        left = header['offset']
        growth = before.st_size - left
        if before.st_ino != header['inode'] or not 0 <= growth <= MAX_APPEND:
            return None
        proof = hashlib.sha256()
        while left:
            check()
            chunk = stream.read(min(left, CHUNK * 4))
            if not chunk:
                return None
            proof.update(chunk)
            left -= len(chunk)
        if proof.hexdigest() != header['prefix']:
            return None
        check()
        tail = stream.read(growth)
        after = path.stat()
        if len(tail) != growth or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                                   before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                                           after.st_mtime_ns, after.st_ctime_ns):
            raise _TranscriptChanged
        return tail


def _extension(stream, header, sections, columns, tail, check):
    """Accept a new chain only when it leaves the saved public history intact."""
    if not tail:
        return True
    base = header['rows']
    history = columns['history']
    live = bytearray(base)
    for i in history:
        if type(i) is not int or not 0 <= i < base:
            return False
        live[i] = True
    refs = columns['ref']
    sidechain = _column(stream, sections, 'sidechain', base, check)
    parents = _column(stream, sections, 'parent', base, check)
    main = next((i for i in range(base - 1, -1, -1) if refs[i] and not sidechain[i]), None)
    if main is None or not live[main] or refs[main] in parents:
        return False
    tip = refs[main]
    offset, row_chain = header['offset'], header['chain']
    # A locally classified assistant needs a known non-compaction span. An explicit prompt or
    # an assistant with usage ends any inherited span. A run of normal duration rows need not.
    codes = columns['codes']
    previous = next((codes[code] for code in reversed(columns['label'])
                     if codes[code] in ('prompt', 'response', 'compaction')), None)
    added, added_refs = [], set()
    for line in tail.splitlines(keepends=True):
        if not line.endswith(b'\n'):
            break  # The public reader does not admit a partial line.
        check()
        item = rows.entry(offset, line, row_chain)
        offset, row_chain = offset + item.size, item.chain
        if (item.kind == 'unreadable' or item.sidechain or item.summary or item.logical or item.preserved
                or item.kind == 'system/compact_boundary'
                or item.ref and (item.ref in added_refs or item.parent != tip)):
            return False
        if item.kind == 'user':
            if item.mark in ('caveat', 'command-name'):
                return False  # A local command's caveat and name rows are labelled by earlier rows: the full analysis.
            if item.origin:
                label = 'prompt'
                previous = label
            elif item.mark == 'interrupted' and not item.tool_result:
                label = 'injected'
            else:
                return False  # Older prompt IDs and tool-result branches need the full analysis.
        elif item.kind == 'assistant':
            if not item.usage and previous not in ('prompt', 'response'):
                return False
            label = previous = 'response'
        elif item.kind == 'attachment':
            label = 'mid_turn' if item.attachment == 'queued_command' and item.command_mode == 'prompt' else 'injected'
        elif item.kind == 'system/local_command':
            label = 'command_output' if item.mark in ('output', 'caveat') else 'command'
        else:
            label = 'meta'
        if item.ref:
            tip = item.ref
            added_refs.add(tip)
        added.append((item, label))
    if any(ref in added_refs for ref in chain(refs, parents)):
        return False
    del parents, sidechain
    # Claude's public walk pulls in earlier blocks and tool results by response/call identity.
    # If a new edge could make an old abandoned row live, a suffix-only walk cannot prove the view.
    responses = {item.response for item, _ in added if item.response}
    calls = {call for item, _ in added for call in item.calls}
    for name, links in (('response', responses), ('answers', calls)):
        if links:
            stored = _column(stream, sections, name, base, check)
            if any(not live[i] and (value in links if name == 'response' else links.intersection(value))
                   for i, value in enumerate(stored)):
                return False
    for item, label in added:
        history.append(len(refs))
        refs.append(item.ref)
        if label not in codes:
            codes.append(label)
        columns['label'].append(codes.index(label))
    columns['added'] = [rows.position_of(item) for item, _ in added]
    return True


def _references(stream, header, sections, columns, transitions, check):
    """Apply the public grouping rule to the proved history and return exact positions."""
    refs = columns.pop('ref')
    wanted = {ref for edge in transitions for ref in (edge.turn, edge.ref) if ref is not None}
    found = {}
    for i, ref in enumerate(refs):
        if ref in wanted:
            found[ref] = i if ref not in found else None
    count = len(refs)
    del refs
    reported = {i for i in found.values() if i is not None}
    anchors = {}
    seen = bytearray(count)
    tick = rows.ticker(check)
    history, labels, codes = (columns[key] for key in ('history', 'label', 'codes'))

    def ordered_labels():
        for i in history:
            tick()
            if type(i) is not int or not 0 <= i < count or seen[i]:
                raise ValueError('Invalid saved Claude history')
            seen[i] = True
            code = labels[i]
            if type(code) is not int or not 0 <= code < len(codes) or not isinstance(codes[code], str):
                raise ValueError('Invalid saved Claude row label')
            yield i, codes[code]

    for i, label, owner in turn_anchors(ordered_labels()):
        if i in reported:
            anchors[i] = owner, label

    base = header['rows']
    selected = {i for i in reported if i < base}
    offsets, sizes, chains = (
        _positions(stream, sections, name, base, selected, check)
        for name in ('offset', 'size', 'chain')
    )
    if chains[base - 1] != header['chain'] or offsets[base - 1] + sizes[base - 1] != header['offset']:
        return None

    def position(i):
        if i >= base:
            return columns['added'][i - base]
        return f'{offsets[i] + sizes[i]}:{chains[i]}'

    proved = []
    for edge in transitions:
        if edge.ref is None:
            proved.append(TurnCursorProjection())
            continue
        if edge.ref not in found:
            proved.append(TurnCursorProjection(pending=True))
            continue
        anchor, row = found.get(edge.turn), found.get(edge.ref)
        if (anchor is None or row is None or anchors.get(row, (None,))[0] != anchor
                or anchors.get(anchor, (None, None))[1] not in ('prompt', 'command')):
            return None
        proved.append(TurnCursorProjection(turn_position=position(anchor), turn_ref=edge.turn,
                                           turn_nearest=edge.turn, row_position=position(row),
                                           row_nearest=edge.ref, kind=anchors[anchor][1]))
    check()
    return tuple(proved)


def project(session, home, store, transitions, check):
    if store is None:
        return None
    paths = rows.session_transcripts(session.native_id, {'CLAUDE_CONFIG_DIR': home}, (session.workspace,), check=check)
    if len(paths) != 1:
        return None  # The general reader validates copies against one another.
    path = Path(paths[0])
    check()
    store_fd = os.open(store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        check()
        fd = os.open(rows.row_file(store, path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=store_fd)
        with os.fdopen(fd, 'rb') as stream:
            return _project_index(stream, path, transitions, check)
    finally:
        os.close(store_fd)


def _project_index(stream, path, transitions, check):
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        return None
    saved = _saved(stream, path, check)
    if saved is None:
        return None
    header, sections, columns = saved
    for attempt in range(2):
        try:
            tail = _proof_and_tail(path, header, check)
            break
        except _TranscriptChanged:
            if attempt == 1:
                return None
    if tail is None or not _extension(stream, header, sections, columns, tail, check):
        return None
    return _references(stream, header, sections, columns, transitions, check)
