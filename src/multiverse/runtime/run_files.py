"""Private one-shot files and content-free cleanup journals. Never repair an agent's directory."""
from contextlib import contextmanager
import errno
import fcntl
import json
from itertools import islice
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from uuid import UUID, uuid4

from .process import process_snapshot, start_matches, valid_start

JOURNAL_BYTES = 128 << 10
MAX_SWEEP = 128
NAMESPACE_SECONDS = 1
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z')
FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def identity(info):
    return [info.st_dev, info.st_ino]


def component(name):
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in ('.', '..'):
        raise ValueError('Invalid run namespace')
    return name


def relative(name):
    if (not isinstance(name, str) or not name or len(name) > 512 or '\0' in name
            or name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError('Run files need a safe relative path')
    return PurePosixPath(name)


def _open_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Run storage must be absolute')
    descriptor = os.open('/', FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _mkdir(path, made):
    """Walk non-following descriptors; remember only directories this run created."""
    path = Path(path)
    descriptor, current = os.open('/', FLAGS), Path('/')
    try:
        for part in path.parts[1:]:
            current /= part
            created = False
            try:
                child = os.open(part, FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if created:
                made.append({'path': str(current), 'identity': identity(os.fstat(descriptor))})
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(path):
    parent = _open_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Run journal is not a regular file')
        data = stream.read(JOURNAL_BYTES + 1)
    if len(data) > JOURNAL_BYTES:
        raise ValueError('Run journal is too large')
    record = json.loads(data)
    if not isinstance(record, dict):
        raise ValueError('Run journal is not an object')
    return record


def save(directory, record):
    data = json.dumps(record, ensure_ascii=True, separators=(',', ':')).encode()
    if len(data) > JOURNAL_BYTES:
        raise ValueError('Run cleanup metadata is too large')
    descriptor = _open_directory(directory)
    name = '.journal-' + uuid4().hex
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, 'run.json', src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(name, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


@contextmanager
def _namespace_lock(directory, *, create=False):
    """Serialize only namespace publication/removal; the persistent run root is the lock, not a new file."""
    base = directory.parents[2]
    descriptor = _mkdir(base, []) if create else _open_directory(base)
    try:
        deadline = time.monotonic() + NAMESPACE_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('Run namespace is busy') from None
                time.sleep(.01)
        yield
    finally:
        os.close(descriptor)


def create(directory, record):
    with _namespace_lock(directory, create=True):
        parent = _mkdir(directory.parent, [])
        try:
            plugin = _open_directory(directory.parent.parent)
            try:
                record['namespaces'] = [
                    {'path': str(directory.parent.parent), 'identity': identity(os.fstat(plugin))},
                    {'path': str(directory.parent), 'identity': identity(os.fstat(parent))},
                ]
            finally:
                os.close(plugin)
            os.mkdir(directory.name, 0o700, dir_fd=parent)  # Never reuse even an empty run directory.
            fd = os.open(directory.name, FLAGS, dir_fd=parent)
            record['directory_identity'] = identity(os.fstat(fd))
            os.close(fd)
        finally:
            os.close(parent)
    record.update(schema=1, phase='preparing', files=[], directories=[])
    save(directory, record)  # No content can precede this journal.


def write_file(directory, record, path, content):
    made = []
    parent = _mkdir(path.parent, made)
    home = Path(record['store']) if record.get('store') else None
    record['directories'].extend(item for item in made
                                 if _inside(Path(item['path']), directory)
                                 or home is not None and _inside(Path(item['path']), home))
    save(directory, record)
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, 'wb') as stream:
            record['files'].append({'path': str(path), 'identity': identity(os.fstat(stream.fileno()))})
            save(directory, record)  # Record the empty file's identity before publishing its contents.
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(parent)


def _inside(path, parent):
    return path != parent and parent in path.parents


def _validate(directory, record):
    if (record.get('schema') != 1 or record.get('phase') not in ('preparing', 'running', 'ended')
            or record.get('phase') == 'running' and not isinstance(record.get('child'), dict)
            or not isinstance(record.get('universe'), str) or not Path(record['universe']).is_absolute()
            or record.get('run_id') != directory.name
            or str(UUID(directory.name)) != directory.name
            or record.get('directory') != str(directory)
            or not isinstance(record.get('files'), list) or not isinstance(record.get('directories'), list)
            or len(record['files']) > 16 or len(record['directories']) > 128):
        raise ValueError('Invalid run cleanup metadata')
    namespaces = record.get('namespaces', [])  # Older journals cannot authorize pruning their parents.
    if not isinstance(namespaces, list) or len(namespaces) not in (0, 2):
        raise ValueError('Invalid run namespaces')
    for item, path in zip(namespaces, (directory.parent.parent, directory.parent)):
        if (not isinstance(item, dict) or item.get('path') != str(path)
                or not isinstance(item.get('identity'), list) or len(item['identity']) != 2
                or not all(type(n) is int and n >= 0 for n in item['identity'])):
            raise ValueError('Invalid run namespace ownership')
    home = record.get('store')
    home = Path(home) if isinstance(home, str) and home else None
    if home is not None and (not home.is_absolute() or '..' in home.parts or home == Path('/')):
        raise ValueError('Invalid recorded run store')
    declared = record.get('declared')
    if not isinstance(declared, list) or len(declared) > 16:
        raise ValueError('Invalid run file declarations')
    targets = set()
    for item in declared:
        root = directory if item.get('root') == 'run' else home if item.get('root') == 'store' else None
        rel = relative(item.get('path'))
        if root is None or (root == directory and rel.parts[0] == 'run.json'):
            raise ValueError('Invalid run file root')
        if item['root'] == 'store' and rel.name != directory.name + '.jsonl':
            raise ValueError('Invalid run session file')
        targets.add(root / rel)
    for entry in record['files'] + record['directories']:
        if (not isinstance(entry, dict) or not isinstance(entry.get('path'), str)
                or not isinstance(entry.get('identity'), list) or len(entry['identity']) != 2
                or not all(type(n) is int and n >= 0 for n in entry['identity'])):
            raise ValueError('Invalid run ownership metadata')
        path = Path(entry['path'])
        if path in (directory, home) or not path.is_absolute() or '..' in path.parts:
            raise ValueError('Invalid run cleanup path')
        if entry in record['files']:
            if path not in targets:
                raise ValueError('Unclaimed run file')
        elif not any(path in target.parents and (_inside(path, directory) or home and _inside(path, home))
                     for target in targets):
            raise ValueError('Unclaimed run directory')
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or identity(info) != record.get('directory_identity'):
        raise ValueError('Run directory was replaced')


def _unlink(entry, *, directory=False, shared_parent=False):
    path = Path(entry['path'])
    try:
        parent = _open_directory(path.parent)
    except FileNotFoundError:
        return
    try:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if not kind(info.st_mode) or identity(info) != entry['identity']:
            raise ValueError('A run cleanup target was replaced')
        if directory:
            try:
                os.rmdir(path.name, dir_fd=parent)  # Only empty directories; never recursive deletion.
            except OSError as error:
                if not shared_parent or error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
        else:
            os.unlink(path.name, dir_fd=parent)
    finally:
        os.close(parent)


def cleanup(directory, record):
    """Only after producers ended. Refuse substitutions and retain the journal on an incomplete cleanup."""
    try:
        _validate(directory, record)
    except FileNotFoundError:
        return True
    for entry in record['files']:
        _unlink(entry)
    store = Path(record['store']) if record.get('store') else None
    parents = [store / relative(item['path']).parent for item in record['declared'] if item['root'] == 'store']
    for entry in reversed(record['directories']):
        # Another run may now use a store ancestor (e.g. projects/). Its nonempty
        # directory is left alone; a run's own final folder still needs a complete cleanup.
        shared = any(Path(entry['path']) in parent.parents for parent in parents)
        _unlink(entry, directory=True, shared_parent=shared)
    with _namespace_lock(directory):
        descriptor = _open_directory(directory)
        try:
            if set(os.listdir(descriptor)) != {'run.json'}:
                raise ValueError('Unrecorded files remain in the run directory')
            os.unlink('run.json', dir_fd=descriptor)
        finally:
            os.close(descriptor)
        _unlink({'path': str(directory), 'identity': record['directory_identity']}, directory=True)
        for entry in reversed(record.get('namespaces', [])):
            try:
                _unlink(entry, directory=True, shared_parent=True)
            except (OSError, ValueError):
                break  # Shared parents are best effort; never remove a replacement or anything nonempty.
    return True


def ended(record):
    """True: the recorded process ended; False: it still runs; None: cannot establish either."""
    if not isinstance(record, dict) or type(record.get('pid')) is not int or record['pid'] <= 0:
        return None
    pid, started = record['pid'], record.get('started')
    if not valid_start(started):
        return None
    snapshot = process_snapshot(pid)
    if snapshot is not None:
        token, state = snapshot
        return not start_matches(started, pid, observed=token) or state.startswith('Z')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return None
    return None


def _entries(path):
    with os.scandir(path) as entries:
        yield from islice(entries, MAX_SWEEP)


def _candidates(base, deadline):
    count = 0
    try:
        for plugin in _entries(base):
            if time.monotonic() >= deadline:
                return
            if not plugin.is_dir(follow_symlinks=False):
                continue
            for owner in _entries(plugin.path):
                if time.monotonic() >= deadline or count >= MAX_SWEEP:
                    return
                if not owner.is_dir(follow_symlinks=False):
                    continue
                for run in _entries(owner.path):
                    if time.monotonic() >= deadline or count >= MAX_SWEEP:
                        return
                    count += 1
                    if run.is_dir(follow_symlinks=False):
                        yield Path(run.path)
    except FileNotFoundError:
        return


def leftovers(base, *, root=None, sweep=False, deadline=None):
    """Read-only by default. An unknown process or journal never authorizes file deletion."""
    deadline = time.monotonic() + 1 if deadline is None else deadline
    problems = []
    try:
        candidates = _candidates(base, deadline)
        for directory in candidates:
            try:
                record = _read(directory / 'run.json')
                _validate(directory, record)
                if root is not None and record.get('universe') != str(Path(root).resolve()):
                    continue
                # Once ended, only the supervisor and native child could still touch these files.
                # The caller may remain alive indefinitely in a dashboard or plugin worker.
                producers = ('supervisor',) if record['phase'] == 'ended' else ('owner', 'supervisor')
                states = [ended(record.get(key)) for key in producers]
                if record.get('child') is not None:
                    states.append(ended(record['child']))
                if False in states:
                    continue
                if all(state is True for state in states) and sweep:
                    cleanup(directory, record)
                    continue
                reason = 'its processes cannot be proven stopped' if None in states else 'its run ended before cleanup'
            except (OSError, ValueError, TypeError, KeyError):
                reason = 'its cleanup records or owned files cannot be verified'
            problems.append(f'One-shot run leftover {directory}: {reason}; inspect it before removing any files')
    except OSError:
        problems.append(f'One-shot run storage {base} cannot be read; restore access before cleanup')
    return problems
