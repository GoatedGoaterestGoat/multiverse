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

from ...store import retry_missing


LIMIT = 256 << 10
ANSWER_LIMIT = 64 << 10
CLAIM_LIMIT = 4096


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
    pattern = r'[A-Za-z0-9_-]{1,128}\.answer\.json' if answers else r'(?:index|[A-Za-z0-9_-]{1,128}\.(?:open|result|input))\.json'
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


@dataclass(frozen=True)
class InputClaim:
    disposition: str
    won: bool = False
    cause: str | None = None

    def outcome(self):
        if self.disposition == 'person':
            return {'state': 'person', 'cause': 'person', 'reason':
                    'This exact question was answered by native input before mv2 reserved its input; no mv2 answer was sent.'}
        if self.disposition == 'closed':
            return {'state': 'gone', 'cause': self.cause or 'closed', 'reason':
                    'This question closed before mv2 reserved its input; no mv2 answer was sent.'}
        if self.disposition == 'input':
            reason = 'An ordinary answer input was already reserved; its delivery cannot be confirmed. '
        else:
            reason = ('The exclusive question/input claim cannot be verified. Another answer may already be '
                      'reserved, or its publication may still be in progress. ')
        return {'state': 'unknown', 'cause': 'unconfirmed', 'reason':
                reason + 'Read its result; do not send this answer again.'}


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
            # Input claims are exclusive publications, not replace-written records.
            return (read_at(folder, name) if name.endswith('.input.json') else
                    retry_missing(lambda: read_at(folder, name)))[0]

    def claim_input(self, id, value):
        return self.claim_decision(id, value, 'input')

    def input_claim(self, id, native_id):
        name = identifier(id) + '.input.json'
        try:
            with self.folder() as folder:
                value = read_at(folder, name, CLAIM_LIMIT)[0]
            if (set(value) != {'schema', 'launch_id', 'native_id', 'id', 'input_id', 'disposition', 'cause'}
                    or type(value['schema']) is not int or value['schema'] != 1
                    or value['launch_id'] != self.launch or value['native_id'] != native_id or value['id'] != id
                    or value['input_id'] != 'prompt-' + id or value['disposition'] not in ('input', 'person', 'closed', 'uncertain')
                    or value['cause'] not in (None, 'person', 'closed', 'switched', 'stopped', 'connection_lost',
                                              'replaced', 'unconfirmed', 'turn_ended', 'interrupted')
                    or value['disposition'] == 'input' and value['cause'] is not None
                    or value['disposition'] == 'person' and value['cause'] != 'person'):
                raise ValueError('Invalid async claim metadata')
            return InputClaim(value['disposition'], cause=value['cause'])
        except FileNotFoundError:
            return InputClaim('unclaimed')
        except (OSError, ValueError, TypeError, KeyError):
            return InputClaim('unknown')

    def claim_decision(self, id, value, disposition):
        """Linearize input admission and observed closure with one metadata-only CAS."""
        id = identifier(id)
        native = value.get('native_id')
        if not isinstance(native, str) or not native or '\0' in native or len(native) > 4096:
            raise ValueError('Invalid async claim scope')
        cause = 'person' if disposition == 'person' else value.get('cause') if disposition in ('closed', 'uncertain') else None
        metadata = {'schema': 1, 'launch_id': self.launch, 'native_id': native, 'id': id,
                    'input_id': 'prompt-' + id, 'disposition': disposition, 'cause': cause}
        raw = json.dumps(metadata, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > CLAIM_LIMIT or disposition not in ('input', 'person', 'closed', 'uncertain'):
            raise ValueError('The async claim is not bounded')
        name, temporary = id + '.input.json', '.claim-' + str(uuid4())
        won = False
        with self.folder(create=True) as folder:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
            try:
                with os.fdopen(fd, 'wb', closefd=False) as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                observed = os.fstat(fd)
                try:
                    os.link(temporary, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
                    current = os.stat(name, dir_fd=folder, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
                        raise ValueError('The async claim changed during publication')
                    won = True
                except FileExistsError:
                    pass
            finally:
                os.close(fd)
                try:
                    os.unlink(temporary, dir_fd=folder)
                except FileNotFoundError:
                    pass
        if won:
            return InputClaim(disposition, won=True, cause=cause)
        claim = self.input_claim(id, native)
        return claim if claim.disposition != 'unclaimed' else InputClaim('unknown')

    def settle(self, name, value, *, closing=False):
        """Claim closure before exposing a terminal outcome or a closed catalog."""
        desired = ('person' if value['state'] == 'person' else
                   'uncertain' if value['state'] in ('unknown', 'submitted') else 'closed')
        claim = self.claim_decision(value['id'], value, desired)
        if claim.won:
            settled = value
        elif claim.disposition == 'input':
            settled = {**value, **claim.outcome(), 'person_answer': None, 'saved_rule': False}
            if value['state'] == 'person' and not closing:
                settled.update(state='submitted', cause='unconfirmed', reason=
                    'Native input was observed after mv2 reserved its ordinary answer input. '
                    'Read its delivery result; do not send it again.')
        else:
            settled = {**value, **claim.outcome(), 'person_answer': None, 'saved_rule': False}
            if claim.disposition == 'person':
                try:
                    prior = self.read(name)
                    if (prior.get('id') == value['id'] and prior.get('launch_id') == self.launch
                            and prior.get('native_id') == value['native_id'] and prior.get('state') == 'person'):
                        settled = prior
                except (OSError, ValueError):
                    pass
        self.write(name, settled)
        return settled, claim.disposition

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
        # A closed snapshot must never become visible before its arbitration marker.
        jobs.sort(key=lambda item: item[0] != 'settle')
        settled = []
        discarded = [name for action, name, _ in jobs if action == 'discard']
        if discarded:
            with self.publication_lock:
                if self.closing.is_set() and not final:
                    return settled
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
                    return settled
                if action == 'write':
                    self.write(name, value)
                elif action == 'settle':
                    result, disposition = self.settle(name, value, closing=final)
                    settled.append((value['id'], result, disposition))
                elif action == 'remove':
                    self.remove(name)
                elif action == 'discard':
                    continue
                else:
                    raise ValueError('Unknown prompt publication action')
        return settled

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
