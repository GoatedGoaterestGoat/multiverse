"""Conversation ownership and append-first session lineage. No provider discovery."""
from contextlib import contextmanager
import json
import os
from pathlib import Path

from ..harnesses.types import normalize_session_id
from ..store import lock, now, read, uid, write
from . import parent, tree, records


class SessionConflict(ValueError):
    """A native conversation already belongs to a different agent."""


def top(root):
    root = Path(root).resolve()
    while (above := parent(root)) is not None:
        root = above
    return root


@contextmanager
def namespace(root):
    """Order: agent launch(s), namespace(s) in path order, then properties.

    Never acquire another agent's launch lock while holding this lock. Claims
    inspect journal tips without changing other agents' pointers.
    """
    with lock(top(root) / 'state' / '.sessions.lock'):
        yield


def canonical_home(home):
    if not isinstance(home, str) or not home or '\0' in home or not Path(home).is_absolute():
        raise ValueError('Native session storage home must be an absolute path')
    return str(Path(home).resolve())


def _stored_home(home):
    """A historical scope was resolved when written; do not follow later symlinks."""
    if (not isinstance(home, str) or not home or '\0' in home or not Path(home).is_absolute() or home.startswith('//')
            or os.path.normpath(home) != home):
        raise ValueError('invalid stored storage scope')
    return home


def journal(directory):
    path = directory / 'sessions.jsonl'
    if not path.exists():
        return []
    rows, identities = [], set()
    try:
        with path.open() as stream:
            while line := stream.readline(16385):
                if not line.endswith('\n') or len(line) > 16384:
                    raise ValueError('incomplete or oversized row')
                row = json.loads(line)
                if (not isinstance(row, dict) or row.get('schema') != 1
                        or not all(isinstance(row.get(key), str) and row[key] for key in
                                   ('id', 'at', 'harness', 'home', 'launch', 'from', 'to', 'reason'))):
                    raise ValueError('invalid row')
                for key in ('id', 'from', 'to'):
                    if normalize_session_id(row[key]) != row[key]:
                        raise ValueError('noncanonical identity')
                if _stored_home(row['home']) != row['home'] or row['id'] in identities:
                    raise ValueError('invalid scope or repeated transition')
                if rows and rows[-1]['to'] != row['from']:
                    raise ValueError('disconnected transitions')
                identities.add(row['id'])
                rows.append(row)
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f'Invalid session journal: {error}') from error
    return rows


def effective(directory, data):
    """Validate a journal and compute its authoritative pointer, without writes."""
    rows = journal(directory)
    if rows:
        last = rows[-1]
        if any(row['harness'] != data.get('harness') or row['home'] != last['home'] for row in rows):
            raise ValueError('Invalid session journal: provider or storage changes within a lineage')
        if data.get('native_session_id') not in (last['from'], last['to']):
            raise ValueError('Invalid session journal: pointer conflicts with its latest transition')
        if data.get('native_session_home') not in (None, last['home']):
            raise ValueError('Invalid session journal: storage conflicts with its latest transition')
        data = {**data, 'native_session_id': last['to'], 'native_session_home': last['home']}
    return data, rows


def recover(directory, data):
    updated, rows = effective(directory, data)
    repaired = bool(rows and updated != data)
    if repaired:
        if data.get('native_session_id') != rows[-1]['to']:
            updated['updated_at'] = rows[-1]['at']
        write(directory / 'properties.json', updated)
    return updated, rows[-1] if repaired else None


def _recorded_provider(data):
    value = data.get('native_session_provider')
    if value is None:
        identity = data.get('native_service_identity')
        value = identity.get('provider') if isinstance(identity, dict) else None
    return value if isinstance(value, str) and value else None


def provider(root, data):
    if known := _recorded_provider(data):
        return known
    if data.get('native_session_provider') is not None or data.get('native_service_identity') is not None:
        return None  # Malformed stored scope is unknown, never a different namespace.
    if not data.get('harness'):
        return None  # Minimal old records have no known provider namespace.
    # Legacy aliases predate the stored provider. Reading configuration is not
    # a native probe and does not load plugin code.
    from .. import harnesses
    try:
        family = getattr(harnesses.load(root, data['harness']), 'family', '')
    except (OSError, ValueError, TypeError, KeyError):
        return None  # Binding alone never requires a surviving harness definition.
    return family if isinstance(family, str) and family else data['harness']


def _journal_mentions(path, native_id):
    """Inspect damaged, readable evidence without parsing or repairing another agent."""
    return records.mentions(path, native_id)


def claim(root, directory, data, native_id, home=None):
    """Caller holds namespace. Earlier generations remain owned by their agent."""
    own_provider = provider(root, data)
    own_directory = directory.resolve()
    own_home = canonical_home(home) if home is not None else data.get('native_session_home')
    for scope in tree(top(root)):
        for path in (scope / 'state' / 'agents').glob('*/properties.json'):
            if path.parent.resolve() == own_directory:
                continue
            try:
                other = records.properties(path)
            except records.RecordError as error:
                owner = f'agent {path.parent.name}'
                journal = path.parent / 'sessions.jsonl'
                try:
                    known = (error.data or {}).get('native_session_id')
                    mentioned = known == native_id or records.mentions(path, native_id)
                    try:
                        journal.stat()
                    except FileNotFoundError:
                        pass  # No earlier generation exists for this peer.
                    else:
                        mentioned = records.mentions(journal, native_id) or mentioned
                except (OSError, ValueError) as unreadable:
                    raise SessionConflict(f'Cannot check {owner}: {path} or {journal} cannot be read safely '
                                          f'({unreadable}); fix permissions or repair the record before retrying') from error
                if mentioned:
                    raise SessionConflict(f'Cannot claim session reserved by {owner}: {path} or {journal} '
                                          'mentions this session; repair the damaged record before retrying') from error
                continue
            # A valid lineage cannot change its provider or storage scope. Exclude
            # known disjoint namespaces before touching potentially unreadable
            # journals. Unknown legacy/malformed scope still needs inspection.
            other_provider = _recorded_provider(other)
            if own_provider and other_provider:
                if other_provider != own_provider:
                    continue
                try:
                    different_home = _stored_home(own_home) != _stored_home(other.get('native_session_home'))
                except ValueError:
                    different_home = False
                if different_home:
                    continue
            try:
                other, rows = effective(path.parent, other)
            except (OSError, ValueError, TypeError) as error:
                journal_path = path.parent / 'sessions.jsonl'
                owner = f'{other.get("name", path.parent.name)} ({path.parent.name})'
                if isinstance(error, OSError):
                    raise SessionConflict(f'Cannot check agent {owner}: {journal_path} cannot be read '
                                          f'({error}); fix its permissions before retrying') from error
                try:
                    mentioned = other.get('native_session_id') == native_id or _journal_mentions(journal_path, native_id)
                except (OSError, ValueError) as unreadable:
                    raise SessionConflict(f'Cannot check agent {owner}: {journal_path} cannot be read safely '
                                          f'({unreadable}); fix its permissions or repair the journal before retrying') from error
                if mentioned:
                    raise SessionConflict(f'Cannot claim session reserved by agent {owner}: {journal_path} is invalid '
                                          f'({error}); repair that agent\'s journal before retrying') from error
                continue  # Damage proven unrelated to this requested ID must not block the tree.
            identities = {other.get('native_session_id'), *(row['from'] for row in rows), *(row['to'] for row in rows)}
            other_provider = provider(scope, other) if native_id in identities else None
            if native_id not in identities or (own_provider and other_provider and other_provider != own_provider):
                continue
            homes = {other.get('native_session_home'), *(row['home'] for row in rows)}
            if own_home and None not in homes and own_home not in homes:
                continue
            raise SessionConflict(f'This terminal switched to another agent\'s session ({other.get("name", "another agent")}); '
                                  'return to this agent\'s own conversation before sending messages')


def transition(root, directory, previous, target, reason, launch_id, home):
    """Caller holds launch.lock. A complete journal row is durable before the pointer."""
    if not isinstance(launch_id, str) or not launch_id:
        raise ValueError('A session change requires the exact launch ID')
    previous, target = normalize_session_id(previous), normalize_session_id(target)
    if not isinstance(reason, str) or not reason or len(reason) > 64 or not all(c.isalnum() or c in '-_' for c in reason):
        raise ValueError('Session reason must be a short name')
    with namespace(root), lock(directory / '.properties.lock'):
        if read(directory / 'runtime.json').get('launch_id') != launch_id:
            raise ValueError('Outdated launch')
        data, repaired = recover(directory, read(directory / 'properties.json'))
        home = canonical_home(home if home is not None else data.get('native_session_home'))
        if data.get('native_session_home') not in (None, home):
            raise ValueError('Native session storage differs from this launch')
        if data.get('native_session_id') == target:
            return data, repaired
        if data.get('native_session_id') != previous:
            raise ValueError('Native session changed before this report; refusing a stale session change')
        claim(root, directory, data, target, home)
        row = {'schema': 1, 'id': uid(), 'at': now(), 'harness': data['harness'], 'home': home,
               'launch': launch_id, 'from': previous, 'to': target, 'reason': reason}
        with (directory / 'sessions.jsonl').open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        data.update(native_session_id=target, native_session_home=home, updated_at=row['at'])
        write(directory / 'properties.json', data)
    return data, row
