"""Best-effort related-universe advice after a local agent lookup fails; never select a target."""
import json
import os
from pathlib import Path
import shlex
import stat
import time

from ..display import clean
from ..store import MISSING_ATTEMPTS, MISSING_BUDGET, MISSING_DELAY, open_replaced, retry_missing
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
        self.absent = set()
        self.retry_rounds = MISSING_ATTEMPTS - 1
        self.retry_deadline = None

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

    def batch(self, paths):
        """Share one missing-name retry budget across this search, including different scopes."""
        pending = set(paths)
        if not pending or not self.ready():
            return
        def probe():
            for path in tuple(pending):
                if not self.ready():
                    return
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                except OSError:
                    pass  # The normal record reader skips unreadable neighbours.
                pending.remove(path)
        for attempt in range(MISSING_ATTEMPTS):
            probe()
            if not pending:
                break
            if not self.ready():
                break
            if self.retry_rounds == 0:
                # A later path first seen missing after the shared window is unverified, never a complete miss.
                if attempt + 1 < MISSING_ATTEMPTS:
                    self.limited = True
                break
            if self.retry_deadline is None:
                self.retry_deadline = min(self.deadline, time.monotonic() + MISSING_BUDGET)
            remaining = self.retry_deadline - time.monotonic()
            if remaining <= 0:
                self.limited = True
                break
            self.retry_rounds -= 1
            time.sleep(min(MISSING_DELAY, remaining))
        self.absent.update(pending)

    def record(self, path):
        if not self.ready() or path in self.absent:
            return None
        try:
            info = retry_missing(path.lstat, deadline=self.deadline)
            if not stat.S_ISREG(info.st_mode) or info.st_size > min(FILE_BYTES, self.bytes):
                return None
            limit = min(FILE_BYTES, self.bytes)
            with open_replaced(path, deadline=self.deadline) as stream:
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
    directories = list(search.folders(scope / 'state' / 'agents'))
    search.batch(directory / 'properties.json' for directory in directories)
    for directory in directories:
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


def _shown_path(scope):
    try:
        path = str(scope.relative_to(Path.cwd()))
    except ValueError:
        path = str(scope)
    if path.startswith('-'):
        path = './' + path  # Quoting alone cannot protect a dash-leading argparse value.
    return shlex.quote(path) if clean(path) == path else None


def hint(root, reference, option='-u'):
    """Read only this universe's structural tree, ignoring broken neighbours and linked directories."""
    try:
        if not isinstance(reference, str) or not reference:
            return ''
        root = Path(root).resolve()
        search, top = Search(), root
        top_known = top.parent.name != 'subverses'
        while top.parent.name == 'subverses' and search.ready():
            candidate = top.parent.parent
            try:
                regular = stat.S_ISREG(retry_missing((candidate / 'universe.json').lstat, deadline=search.deadline).st_mode)
            except OSError:
                break
            if not regular:
                top_known = True
                break
            top = candidate
            top_known = top.parent.name != 'subverses'
        if not top_known:
            search.limited = True
        pending, found = [top], []
        while pending and search.ready():
            scope = pending.pop()
            # A damaged parent's record must not hide its healthy children.
            children = list(search.folders(scope / 'subverses'))
            search.batch(child / 'universe.json' for child in children)
            pending.extend(children)
            if scope == root:
                continue
            record = search.record(scope / 'universe.json')
            if (not record or type(record.get('schema')) is not int or record['schema'] != 1
                    or not all(isinstance(record.get(key), str) and record[key] for key in ('id', 'workspace'))):
                continue
            for name, identity in _matches(search, scope, reference):
                found.append((scope, name, identity))
        if not found:
            if search.limited and not top_known:
                return ('The related-universe search could not verify its top universe. '
                        'From the related tree\'s top universe, run mv2 list --recursive, '
                        f'then use {option} PATH to select a matching universe explicitly.')
            listing = f"mv2 -u {_shown_path(top) or 'PATH'} list --recursive"
            return (f"The related-universe search reached its limit. Run {listing} to look through the related tree, "
                    f"then use {option} PATH to select a matching universe explicitly." if search.limited else '')
        suggestions = []
        for scope, name, identity in sorted(set(found)):
            if scope in root.parents:
                place = 'parent universe'
            else:
                # The path alternates structural directories and literal subverse names.
                label = '/'.join(scope.relative_to(top).parts[1::2])
                place = f'subverse {clean(label)}'
            path = _shown_path(scope)
            if path is None:
                continue
            suggestions.append(f'{clean(name)} ({clean(identity)}) in {place}: add {option} {path}')
        if not suggestions:
            return ''
        suffix = ' (search limit reached; more matches may exist)' if search.limited else ''
        return 'Related matches: ' + '; '.join(suggestions) + suffix + '.'
    except (OSError, ValueError, RuntimeError):
        return ''  # Optional advice never replaces the original missing-agent error.
