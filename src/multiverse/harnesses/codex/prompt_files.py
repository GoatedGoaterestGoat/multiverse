"""Private, bounded prompt files; no native home or transport is opened here."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import threading
from uuid import uuid4


LIMIT = 256 << 10
ANSWER_LIMIT = 64 << 10


class PendingPublication(ValueError):
    """Core's exclusive publisher may still be removing its temporary hardlink."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid prompt file identity')
    return value


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Repeated prompt file field')
        result[key] = value
    return result


def filename(value, *, answers=False):
    pattern = r'[A-Za-z0-9_-]{1,128}\.answer\.json' if answers else r'(?:index|[A-Za-z0-9_-]{1,128}\.(?:open|result))\.json'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError('Invalid prompt record filename')
    return value


def read_at(folder, name, limit=LIMIT):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=folder)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1 or before.st_size > limit:
            raise ValueError('The prompt file is not a bounded, single-link regular file')
        if before.st_nlink > 1:
            raise PendingPublication('The prompt file is not yet a single-link publication')
        with os.fdopen(fd, 'rb', closefd=False) as source:
            raw = source.read(limit + 1)
        after = os.fstat(fd)
        if (len(raw) > limit or any(getattr(before, key) != getattr(after, key)
                for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))):
            raise ValueError('The prompt file changed during its read')
        try:
            value = json.loads(raw, object_pairs_hook=unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON constant')))
        except (ValueError, RecursionError):
            raise ValueError('The prompt file is not valid structured data') from None
        if not isinstance(value, dict):
            raise ValueError('The prompt file is not an object')
        return value, after
    finally:
        os.close(fd)


@dataclass(repr=False)
class Candidate:
    id: str
    answer: dict

    def __repr__(self):
        return '<Codex prompt answer>'  # Never render a secret during diagnostics.


class Files:
    def __init__(self, directory, launch):
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            raise ValueError('Prompt pickup needs the trusted agent directory')
        self.directory, self.launch = Path(directory), identifier(launch)
        self.publication_lock = threading.Lock()
        self.closing = threading.Event()

    def stop_publications(self):
        self.closing.set()

    @contextmanager
    def folder(self, *, create=False, answers=False):
        """Anchor every component to a descriptor; never recreate the agent itself."""
        opened = []
        try:
            current = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened.append(current)
            for name in (('prompts',) if answers else ('codex-prompts', self.launch)):
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                current = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                opened.append(current)
            yield current
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def read(self, name):
        filename(name)
        with self.folder() as folder:
            return read_at(folder, name)[0]

    def write(self, name, value):
        filename(name)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > LIMIT:
            raise ValueError('The prompt record exceeds its publication limit')
        with self.folder(create=True) as folder:
            temporary = '.write-' + str(uuid4())
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, name, src_dir_fd=folder, dst_dir_fd=folder)
            finally:
                try:
                    os.unlink(temporary, dir_fd=folder)
                except FileNotFoundError:
                    pass

    def remove(self, name, *, answers=False):
        filename(name, answers=answers)
        try:
            with self.folder(answers=answers) as folder:
                os.unlink(name, dir_fd=folder)
        except FileNotFoundError:
            pass

    def apply(self, jobs, *, final=False):
        jobs = list(jobs)
        discarded = [name for action, name, _ in jobs if action == 'discard']
        if discarded:
            with self.publication_lock:
                if self.closing.is_set() and not final:
                    return
                try:
                    with self.folder(answers=True) as folder:
                        for name in discarded:
                            filename(name, answers=True)
                            try:
                                os.unlink(name, dir_fd=folder)
                            except FileNotFoundError:
                                pass
                except FileNotFoundError:
                    pass
        for action, name, value in jobs:
            with self.publication_lock:
                if self.closing.is_set() and not final:
                    return
                if action == 'write':
                    self.write(name, value)
                elif action == 'remove':
                    self.remove(name)
                elif action == 'discard':
                    continue
                else:
                    raise ValueError('Unknown prompt publication action')

    def take(self, id):
        """Take exactly the exclusive core answer, removing secret bytes before send."""
        name = identifier(id) + '.answer.json'
        try:
            with self.folder(answers=True) as folder:
                value, observed = read_at(folder, name, ANSWER_LIMIT)
                current = os.stat(name, dir_fd=folder, follow_symlinks=False)
                if any(getattr(current, key) != getattr(observed, key)
                       for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
                    raise ValueError('The prompt answer changed before pickup')
                os.unlink(name, dir_fd=folder)
            return Candidate(id, value)
        except (FileNotFoundError, PendingPublication):
            return None
