"""Conversation ownership and append-first session lineage. No provider discovery."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

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



def _different_homes(left, right):
    """Only disjoint recorded paths AND their present aliases prove separate storage."""
    try:
        left, right = _stored_home(left), _stored_home(right)
        return left != right and canonical_home(left) != canonical_home(right)
    except (OSError, ValueError, RuntimeError):
        return False  # Unknown storage cannot authorize a duplicate claim.


FRESH_FIELDS = ('native_session_id', 'native_session_home', 'native_session_provider',
                'native_storage_environment', 'native_environment', 'native_service_identity',
                'agent_identity', 'agent_environment', 'native_fresh_pending')


def _next_home(row):
    return row.get('replacement', {}).get('native_session_home') if row.get('kind') == 'fresh-start' else row['home']


def claims(data, rows):
    """A retirement changes current storage, never ownership of earlier IDs."""
    return {(data.get('native_session_id'), data.get('native_session_home')),
            *((row['from'], row['home']) for row in rows),
            *((row['to'], _next_home(row)) for row in rows)}


def _recorded_homes(data):
    # Before explicit retirement, every transition used this one saved home.
    # Retirement expands the scope index durably before appending its row.
    homes = data.get('native_session_homes', [data.get('native_session_home')])
    if not isinstance(homes, list):
        return [None]
    try:
        for home in homes:
            if home is not None:
                _stored_home(home)
    except ValueError:
        return [None]
    return [data.get('native_session_home'), *homes]


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
                if (not isinstance(row, dict) or row.get('schema') not in (1, 2)
                        or not {'home', 'from', 'to'} <= row.keys()
                        or not all(isinstance(row.get(key), str) and row[key] for key in
                                   ('id', 'at', 'harness', 'launch', 'reason'))):
                    raise ValueError('invalid row')
                kind = row.get('kind') if row['schema'] == 2 else None
                if row['schema'] == 1 and row.get('kind') is not None:
                    raise ValueError('invalid legacy transition kind')
                if row['schema'] == 2 and kind not in ('fresh-start', 'bind'):
                    raise ValueError('invalid transition kind')
                if kind == 'bind' and (row.get('from') is not None or not rows
                        or rows[-1].get('kind') != 'fresh-start' or rows[-1]['to'] is not None):
                    raise ValueError('binding has no pending fresh conversation')
                for key in ('id', 'from', 'to'):
                    if row.get(key) is None and key != 'id' and (kind == 'fresh-start' or kind == 'bind' and key == 'from'):
                        continue
                    if normalize_session_id(row[key]) != row[key]:
                        raise ValueError('noncanonical identity')
                if (row.get('home') is not None or row['schema'] == 1) and _stored_home(row['home']) != row['home']:
                    raise ValueError('invalid scope')
                if row['id'] in identities:
                    raise ValueError('invalid scope or repeated transition')
                if rows and rows[-1]['to'] != row['from']:
                    raise ValueError('disconnected transitions')
                if kind == 'fresh-start':
                    replacement = row.get('replacement')
                    if (row['reason'] != 'missing-conversation' or not isinstance(replacement, dict)
                            or normalize_session_id(row['launch']) != row['launch']
                            or set(replacement) != set(FRESH_FIELDS)
                            or replacement['native_session_id'] != row.get('to')
                            or type(replacement['native_fresh_pending']) is not bool
                            or replacement['native_fresh_pending'] is not True
                            or replacement['agent_identity'] is not None or replacement['agent_environment'] is not None
                            or row.get('to') is not None and row['to'] == row['from']):
                        raise ValueError('invalid fresh-start replacement')
                    if _next_home(row) is not None:
                        _stored_home(_next_home(row))
                    storage = replacement['native_storage_environment']
                    if not isinstance(storage, dict) or any(value is not None and not isinstance(value, str) for value in storage.values()):
                        raise ValueError('invalid fresh-start storage')
                if rows and kind != 'fresh-start' and row['home'] != _next_home(rows[-1]):
                    raise ValueError('storage changes without retirement')
                identities.add(row['id'])
                rows.append(row)
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f'Invalid session journal {path}: {error}') from error
    return rows


def effective(directory, data):
    """Validate a journal and compute its authoritative pointer, without writes."""
    rows = journal(directory)
    if rows:
        last = rows[-1]
        if any(row['harness'] != data.get('harness') for row in rows):
            raise ValueError('Invalid session journal: provider changes within a lineage')
        if data.get('native_session_id') not in (last['from'], last['to']):
            raise ValueError('Invalid session journal: pointer conflicts with its latest transition')
        if (last.get('kind') == 'fresh-start' and data.get('native_session_transition') != last['id']
                and data.get('native_session_id') == last['from']):
            # Append-before-pointer recovery also clears the old account and
            # stable identity. Never reconstruct a new scope from today's shell.
            data = {**data, **last['replacement'], 'native_session_transition': last['id']}
        if data.get('native_session_home') not in (None, _next_home(last)):
            raise ValueError('Invalid session journal: storage conflicts with its latest transition')
        data = {**data, 'native_session_id': last['to'], 'native_session_home': _next_home(last)}
        if last.get('kind') == 'bind':
            data['native_fresh_pending'] = False
            if data.get('agent_identity'):
                data['agent_identity'] = {**data['agent_identity'], 'native_id': last['to']}
    return data, rows


def recover(directory, data):
    updated, rows = effective(directory, data)
    repaired = bool(rows and updated != data)
    if repaired:
        if data.get('native_session_id') != rows[-1]['to']:
            updated['updated_at'] = rows[-1]['at']
        write(directory / 'properties.json', updated)
    return updated, rows[-1] if repaired else None


def pending_fresh(directory, data):
    """A property marker alone cannot authorize spawn preparation on resume."""
    if data.get('native_fresh_pending') is not True:
        return False
    rows = journal(directory)
    if (not rows or rows[-1].get('kind') != 'fresh-start'
            or rows[-1]['id'] != data.get('native_session_transition')
            or rows[-1]['to'] != data.get('native_session_id')):
        raise ValueError('Pending fresh conversation has no matching retirement journal; repair the agent records before resuming')
    row = rows[-1]
    facts = records.runtime(directory / 'runtime.json')
    launches = {row['launch']}
    if facts.get('launch_id') not in (None, row.get('previous_launch')):
        if normalize_session_id(facts['launch_id']) != facts['launch_id']:
            raise ValueError('Fresh conversation launch identity is invalid; repair runtime.json before resuming')
        launches.add(facts['launch_id'])
        if facts.get('state') not in ('starting', 'failed') or any(
                key in facts for key in ('pid', 'exit_code', 'usage', 'last_usage', 'native_bridge', 'native_service')):
            raise ValueError('The fresh conversation may already have started; verify its native ID and bind it before resuming')
    for launch in launches:
        for name in ('request.json', 'ready', 'cleanup.json', 'roles'):
            try:
                (directory / 'launches' / launch / name).lstat()
            except FileNotFoundError:
                continue
            raise ValueError('The fresh conversation launch was already published; verify its native ID and bind it before resuming')
    return True


def unbound_fresh(directory, data):
    """Only a validated retirement with no subsequent binding proves there is no current native ID."""
    current, rows = effective(directory, data)
    return bool(rows and current.get('native_session_id') is None
                and rows[-1].get('kind') == 'fresh-start' and rows[-1]['to'] is None
                and current.get('native_session_transition') == rows[-1]['id'])


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


def damaged_mentions(directory, value):
    """Missing files cannot claim an ID; surviving unreadable evidence still can."""
    paths = [directory / name for name in ('properties.json', 'runtime.json', 'sessions.jsonl')]
    try:
        folder = directory / 'launches'
        launches = [] if stat.S_ISREG(folder.stat().st_mode) else list(folder.iterdir())
    except FileNotFoundError:
        launches = []
    for launch in launches:
        try:
            mode = launch.stat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            continue  # A regular file cannot contain a launch request.
        paths.append(launch / 'request.json')
    mentioned = False
    for path in paths:
        try:
            mentioned = records.mentions(path, value) or mentioned
        except FileNotFoundError:
            continue
    return mentioned


def claim(root, directory, data, native_id, home=None, *, operation=None):
    """Caller holds namespace. Earlier generations remain owned by their agent."""
    own_provider = provider(root, data)
    own_directory = directory.resolve()
    own_home = canonical_home(home) if home is not None else data.get('native_session_home')
    from .agents import orphaned, record_paths
    for scope in tree(top(root)):
        for path in record_paths(scope):
            if path.parent.resolve() == own_directory:
                continue
            try:
                other = records.properties(path)
            except records.RecordError as error:
                owner = f'agent {path.parent.name}'
                journal = path.parent / 'sessions.jsonl'
                try:
                    if orphaned(path.parent):
                        continue  # Only plugin leftovers; no current or historical claim.
                    known = (error.data or {}).get('native_session_id')
                    mentioned = damaged_mentions(path.parent, native_id) or known == native_id
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
                if all(_different_homes(own_home, home) for home in _recorded_homes(other)):
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
            owned = claims(other, rows)
            identities = {identity for identity, _ in owned}
            other_provider = provider(scope, other) if native_id in identities else None
            if native_id not in identities or (own_provider and other_provider and other_provider != own_provider):
                continue
            homes = {home for identity, home in owned if identity == native_id}
            if all(_different_homes(own_home, other_home) for other_home in homes):
                continue
            if operation == 'bind':
                raise SessionConflict(f'Cannot bind session {native_id}: it belongs to another agent '
                                      f'({other.get("name", path.parent.name)}). Use that agent, or bind an '
                                      'unclaimed session ID to this one')
            raise SessionConflict(f'This terminal switched to another agent\'s session ({other.get("name", "another agent")}); '
                                  'return to this agent\'s own conversation before sending messages')


def _append(directory, row):
    line = json.dumps(row, ensure_ascii=False) + '\n'
    if len(line) > 16384:
        raise ValueError('Session transition is too large to record safely')
    with (directory / 'sessions.jsonl').open('a') as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def retire(root, directory, previous, prepared, launch_id, *, previous_launch=None):
    """Caller holds launch.lock and has proved the old conversation absent.

    Persist the new selection before changing the pointer. Reported/file
    harnesses stay unbound until their own new native identity arrives.
    """
    with namespace(root), lock(directory / '.properties.lock'):
        data, _ = recover(directory, read(directory / 'properties.json'))
        if data.get('native_session_id') != previous:
            raise ValueError('Native session changed before its missing conversation could be retired')
        replacement = {key: prepared.get(key) for key in FRESH_FIELDS}
        replacement['native_environment'] = prepared.get('native_environment') or {}
        replacement['agent_identity'] = replacement['agent_environment'] = None
        replacement['native_fresh_pending'] = True
        if replacement['native_session_id']:
            claim(root, directory, prepared, replacement['native_session_id'], replacement['native_session_home'])
        row = {'schema': 2, 'kind': 'fresh-start', 'id': uid(), 'at': now(), 'harness': data['harness'],
               'home': data.get('native_session_home'), 'launch': launch_id, 'from': previous,
               'to': replacement['native_session_id'], 'reason': 'missing-conversation', 'replacement': replacement,
               'previous_launch': previous_launch}
        if row['home'] is not None:
            _stored_home(row['home'])
        if replacement['native_session_home'] is not None:
            _stored_home(replacement['native_session_home'])
        # Claims may exclude a disjoint scope without opening an unreadable
        # journal. Reserve the replacement scope first, while the pointer is
        # still old, so an append/pointer crash cannot hide either namespace.
        homes = {*_recorded_homes(data), replacement['native_session_home']}
        data['native_session_homes'] = sorted(homes, key=lambda home: home or '')
        write(directory / 'properties.json', data)
        _append(directory, row)
        data.update({key: prepared[key] for key in ('model', 'workspace', 'updated_at') if key in prepared},
                    **replacement, native_session_transition=row['id'])
        write(directory / 'properties.json', data)
    return data, row


def record_initial_binding(directory, data, target, launch_id):
    """Caller holds launch, namespace and properties locks; finish a retired lineage."""
    rows = journal(directory)
    if not rows or rows[-1].get('kind') != 'fresh-start' or rows[-1]['to'] is not None:
        return
    _append(directory, {'schema': 2, 'kind': 'bind', 'id': uid(), 'at': now(), 'harness': data['harness'],
                       'home': data.get('native_session_home'), 'launch': launch_id or rows[-1]['launch'],
                       'from': None, 'to': target, 'reason': 'fresh-conversation'})


def require_unretired(directory, target):
    if any(row.get('kind') == 'fresh-start' and row['from'] == target for row in journal(directory)):
        raise ValueError('This native session ID was retired after its conversation was missing; bind the new conversation ID')


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
        require_unretired(directory, target)
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
        _append(directory, row)
        data.update(native_session_id=target, native_session_home=home, updated_at=row['at'])
        write(directory / 'properties.json', data)
    return data, row
