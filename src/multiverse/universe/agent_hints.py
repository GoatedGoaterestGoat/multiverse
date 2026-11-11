"""Best-effort related-universe advice after a local agent lookup fails; never select a target."""
import json
import os
from pathlib import Path
import shlex
import stat
import time

from ..display import clean
from ..store import open_regular
from . import records


BUDGET = .2
ENTRIES = 4096
BYTES = 4 * 1024 * 1024
FILE_BYTES = 64 * 1024


class Search:
    def __init__(self):
        self.deadline = time.monotonic() + BUDGET
        self.entries, self.bytes = ENTRIES, BYTES
        self.limited = False

    def ready(self):
        if time.monotonic() >= self.deadline or self.entries <= 0 or self.bytes <= 0:
            self.limited = True
            return False
        return True

    def folders(self, path):
        try:
            if path.is_symlink():
                return
            with os.scandir(path) as entries:
                for entry in entries:
                    if not self.ready():
                        return
                    self.entries -= 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            return

    def record(self, path):
        if not self.ready():
            return None
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > min(FILE_BYTES, self.bytes):
                return None
            limit = min(FILE_BYTES, self.bytes)
            with open_regular(path) as stream:
                raw = stream.read(limit + 1)
            self.bytes -= len(raw)
            if len(raw) > limit:
                return None
            value = json.loads(raw.decode('utf-8'))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, RecursionError):
            return None


def _matches(search, scope, reference):
    try:
        linked = (scope / 'state').is_symlink()
    except OSError:
        return []
    if linked:
        return []
    matches, names, ids = [], [], []
    for directory in search.folders(scope / 'state' / 'agents'):
        value = search.record(directory / 'properties.json')
        if value is None or any(key in value and not isinstance(value[key], str)
                                and (value[key] is not None or key in records.IDENTITY_FIELDS)
                                for key in records.TEXT_FIELDS):
            continue
        identity, name = value.get('id', directory.name), value.get('name')
        item = (name or directory.name, identity or directory.name)
        if reference in (identity, directory.name):
            ids.append(item)
        if reference == name:
            names.append(item)
        if reference == name or identity.startswith(reference) or directory.name.startswith(reference):
            matches.append(item)
    return ids or names or matches


def hint(root, reference, option='-u'):
    """Read only this universe's structural tree, ignoring broken neighbours and linked directories."""
    try:
        if not isinstance(reference, str) or not reference:
            return ''
        root = Path(root).resolve()
        search, top = Search(), root
        while top.parent.name == 'subverses' and search.ready():
            candidate = top.parent.parent
            try:
                regular = stat.S_ISREG((candidate / 'universe.json').lstat().st_mode)
            except OSError:
                break
            if not regular:
                break
            top = candidate
        pending, found = [top], []
        while pending and search.ready():
            scope = pending.pop()
            # A damaged parent's record must not hide its healthy children.
            pending.extend(search.folders(scope / 'subverses'))
            if scope == root:
                continue
            record = search.record(scope / 'universe.json')
            if (not record or type(record.get('schema')) is not int or record['schema'] != 1
                    or not all(isinstance(record.get(key), str) and record[key] for key in ('id', 'workspace'))):
                continue
            for name, identity in _matches(search, scope, reference):
                found.append((scope, name, identity))
        if not found:
            return ''
        suggestions = []
        for scope, name, identity in sorted(set(found)):
            if scope in root.parents:
                place = 'parent universe'
            else:
                # The path alternates structural directories and literal subverse names.
                label = '/'.join(scope.relative_to(top).parts[1::2])
                place = f'subverse {clean(label)}'
            try:
                path = str(scope.relative_to(Path.cwd()))
            except ValueError:
                path = str(scope)
            if path.startswith('-'):
                path = './' + path  # Shell quoting alone cannot make argparse accept a dash-leading value.
            # Do not offer a command with an altered path containing terminal controls.
            if clean(path) != path:
                continue
            suggestions.append(f'{clean(name)} ({clean(identity)}) in {place}: add {option} {shlex.quote(path)}')
        if not suggestions:
            return ''
        suffix = ' (search limit reached; more matches may exist)' if search.limited else ''
        return 'Related matches: ' + '; '.join(suggestions) + suffix + '.'
    except (OSError, ValueError, RuntimeError):
        return ''  # Optional advice never replaces the original missing-agent error.
