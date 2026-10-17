"""Launch-exact attachment cleanup and metadata-only role records.

These files describe mv2's own processes and connections, never native turns,
prompts or outside clients. No service is stopped to complete a handover.
"""
import os
import fcntl
import logging
from contextlib import contextmanager
from pathlib import Path
import time

from ..store import existing_lock, lock, now, read, uid, write
from ..universe import agents
from .process import process_started


def begin(launch, started):
    write(launch / 'cleanup.json', {'schema': 1, 'launch_id': launch.name, 'state': 'active',
                                   'pid': os.getpid(), 'pid_started': started, 'at': now()})


def finish(launch, errors=()):
    record = read(launch / 'cleanup.json')
    write(launch / 'cleanup.json', {**record, 'state': 'failed' if errors else 'complete',
                                   'completed_at': now(), 'errors': list(errors)})


CLOSED_CONNECTION_LIMIT = 100


@contextmanager
def _connections_lock(launch):
    # Create only the lock file, never a removed launch/agent directory. This
    # also supports older launches whose journal lock did not exist yet.
    with (launch / '.connections.lock').open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _prune_connections(launch):
    closed = []
    for path in (launch / 'roles').glob('connection-*.json'):
        try:
            value = read(path)
        except (OSError, ValueError):
            continue  # Unknown evidence must never be discarded as closed.
        if (isinstance(value, dict) and value.get('schema') == 1 and value.get('launch_id') == launch.name
                and value.get('state') == 'closed' and isinstance(value.get('closed_at'), str)):
            closed.append((value['closed_at'], path))
    for _, path in sorted(closed)[:-CLOSED_CONNECTION_LIMIT]:
        path.unlink(missing_ok=True)
        path.with_suffix('.lock').unlink(missing_ok=True)


def connection_logger(launch):
    """Only endpoint/role/timing metadata; keep at most 100 closed connections.

    One connection-journal lock serializes updates, pruning and observation. Active, pending and unknown
    records are never pruned. Read-only recovery checks use their enclosing
    monitor/worker role rather than a durable record per short connection.
    """
    warned = False
    def record(value):
        nonlocal warned
        identifier = value['id']
        if not isinstance(identifier, str) or len(identifier) != 36 or '/' in identifier:
            raise ValueError('Invalid connection record identity')
        path = launch / 'roles' / ('connection-' + identifier + '.json')
        try:
            with _connections_lock(launch):
                previous = read(path) if path.exists() else {}
                write(path, {**previous, 'schema': 1, 'launch_id': launch.name, 'role': value['role'],
                             'endpoint': value['endpoint'], 'state': 'closed' if value['state'] == 'closed' else 'active',
                             'pid': os.getpid(), 'at': previous.get('at') or now(),
                             **({'closed_at': now()} if value['state'] == 'closed' else {})})
                if value['state'] == 'closed':
                    _prune_connections(launch)
        except FileNotFoundError:
            if launch.is_dir():
                raise
            # Optional diagnostics must not prevent passive native observation.
            # The enclosing role/runner remains the teardown proof. Never
            # recreate a removed directory to record a late connection close.
            if not warned:
                logging.getLogger(__name__).warning(
                    'Connection metadata skipped for launch %s: launch directory is missing', launch.name)
                warned = True
    return record


class Role:
    """One owned launch role; no core lock is ever acquired by this ledger."""
    def __init__(self, launch, name, *, pending=False, started=None):
        self.path = launch / 'roles' / (uid() + '.json')
        self.record = {'schema': 1, 'launch_id': launch.name, 'role': name,
                       'state': 'pending' if pending else 'active', 'at': now(),
                       'pid': None if pending else os.getpid(),
                       'pid_started': None if pending else started}
        with lock(self.path.with_suffix('.lock')):
            write(self.path, self.record)

    def claimed(self, pid, started):
        with existing_lock(self.path.with_suffix('.lock')):
            current = read(self.path)
            if current['state'] != 'closed':
                write(self.path, {**current, 'state': 'active', 'pid': pid, 'pid_started': started})

    def close(self):
        try:
            with existing_lock(self.path.with_suffix('.lock')):
                current = read(self.path)
                if current['state'] != 'closed':
                    write(self.path, {**current, 'state': 'closed', 'closed_at': now()})
        except FileNotFoundError:
            return  # Removal must never recreate an old agent directory.

    @classmethod
    def existing(cls, path):
        role = object.__new__(cls)
        role.path = Path(path)
        role.record = read(role.path)
        return role


def _gone(pid, started=None):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # Observation only. Never signal a cleanup process.
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return bool(started and (current := process_started(pid)) and current != started)


def problem(directory, facts):
    launch_id = facts.get('launch_id')
    if not launch_id:
        return None
    launch = directory / 'launches' / launch_id
    path = launch / 'cleanup.json'
    if path.exists():
        record = read(path)
        if not isinstance(record, dict) or record.get('launch_id') != launch_id or record.get('schema') != 1:
            return 'The previous terminal has an invalid cleanup record'
        complete = record.get('state') == 'complete'
        gone = _gone(record.get('pid'), record.get('pid_started'))
        if not complete and not gone:
            return 'The previous terminal is still closing its native connections'
        if not complete and facts.get('pid') and not _gone(facts['pid'], facts.get('pid_started')):
            return 'The previous terminal child has not finished exiting'
    elif facts.get('runner_pid') and not _gone(facts['runner_pid'], facts.get('runner_started')):
        return 'The previous terminal is still closing (older runner has no cleanup record)'
    elif not path.exists() and facts.get('runner_pid') and facts.get('pid') and not _gone(facts['pid'], facts.get('pid_started')):
        return 'The previous terminal child is still running (older runner has no cleanup record)'
    # A completed runner closes all its in-process roles before publishing the
    # marker. A separately spawned recovery worker also needs to have drained.
    try:
        with _connections_lock(launch):
            for path in (launch / 'roles').glob('*.json'):
                role = read(path)
                if not isinstance(role, dict) or role.get('launch_id') != launch_id or role.get('schema') != 1:
                    return 'The previous terminal has an invalid connection record'
                if role.get('state') != 'closed' and not _gone(role.get('pid'), role.get('pid_started')):
                    return 'The previous terminal is still closing its ' + str(role.get('role', 'native connection'))
    except FileNotFoundError:
        if launch.exists():
            raise  # A role disappeared without a serialized, proven-close prune.
        # Legacy fixtures/records may have no launch directory at all. Existing
        # runner/child checks above still require any recorded process to exit.
    return None


def wait(directory, launch_id, timeout):
    """No launch lock while waiting: the old runner needs it to drain input."""
    deadline = time.monotonic() + timeout
    while True:
        facts = agents.runtime(directory)
        if facts.get('launch_id') != launch_id:
            raise ValueError('Launch changed while waiting for terminal cleanup; refusing a replacement')
        reason = problem(directory, facts)
        if reason is None:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(reason + '; wait briefly, then retry. Attachment cleanup is not complete.')
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
