"""An agent's reported dependency, identified independently of its mutable name."""
from itertools import chain
from pathlib import Path

from .. import universe
from . import agents, records


def select(root, reporter, reference, target_universe=None):
    reference = reference.strip()
    if reference in ('user', 'self'):
        if target_universe is not None:
            raise ValueError('--blocked-on-universe applies only to an agent')
        return {'kind': reference}
    scope = universe.exact(target_universe, root) if target_universe is not None else root
    name = reference.removeprefix('agent:').strip()
    if not name or ',' in name:
        raise ValueError('--blocked-on names one agent, user, or self')
    directory = agents.directory(scope, name)
    target = records.properties(directory / 'properties.json')
    if directory.resolve() == reporter.resolve():
        return {'kind': 'self'}
    return {'kind': 'agent', 'id': target['id'], 'name': target['name'], 'universe': str(scope)}


def current(properties):
    """Damaged advisory data never upgrades a message or breaks a listing."""
    value = properties.get('blocked_on')
    if properties.get('phase') != 'blocked' or not isinstance(value, dict):
        return None
    if value.get('kind') in ('user', 'self'):
        return {'kind': value['kind']}
    if (value.get('kind') == 'agent'
            and all(isinstance(value.get(key), str) and value[key] for key in ('id', 'name', 'universe'))
            and '/' not in value['id'] and '\\' not in value['id'] and value['id'] not in ('.', '..')
            and Path(value['universe']).is_absolute()):
        return {key: value[key] for key in ('kind', 'id', 'name', 'universe')}
    return None


def matches(properties, sender):
    value = current(properties)
    return bool(value and value['kind'] == 'agent' and isinstance(sender, dict)
                and not sender.get('plugin') and not sender.get('core')
                and value['id'] == sender.get('agent_id')
                and value['id'] != properties.get('id'))


def _top(root):
    while parent := universe.parent(root):
        root = parent
    return root


def _find(scope, identity):
    # Normally one record read. Also cover a legacy folder named differently from its saved ID.
    direct = scope / 'state' / 'agents' / identity / 'properties.json'
    paths = [direct] if direct.exists() else agents.record_paths(scope)
    for path in paths:
        try:
            value = records.properties(path)
            if value.get('id') == identity:
                return value
        except (OSError, ValueError):
            continue
    return None


def display(root, properties, cache=None):
    value = current(properties)
    if not value or value['kind'] != 'agent':
        return value
    saved = Path(value['universe'])
    key = (str(root), str(saved), value['id'])
    cache = {} if cache is None else cache
    if key not in cache:
        found = None
        try:
            checked = set()
            scopes = (scope for top in dict.fromkeys((_top(root), _top(saved))) for scope in universe.tree(top))
            for scope in chain((saved,), scopes):
                if scope in checked:
                    continue
                checked.add(scope)
                if target := _find(scope, value['id']):
                    found = (scope, target['name'])
                    break
        except (OSError, ValueError, KeyError):
            pass
        cache[key] = found
    found = cache[key]
    scope, name = found if found else (saved, value['name'])
    if scope == root:
        place = None
    else:
        try:
            place = '/'.join(part for part in scope.relative_to(root).parts if part != 'subverses')
        except ValueError:
            place = str(scope)
    return {'kind': 'agent', 'id': value['id'], 'name': name, 'universe': place, 'gone': found is None}
