"""Launch-exact attachment cleanup and metadata-only role records.

These files describe mv2's own processes and connections, never native turns,
prompts or outside clients. No service is stopped to complete a handover.
"""
import os
import fcntl
import logging
from datetime import datetime, timezone
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path
import time

from ..store import existing_lock, lock, now, read, uid, write
from ..universe import agents
from .process import START, LINUX_START, valid_start, boot_time, process_started, process_snapshot, process_command


def begin(launch, started):
    write(launch / 'cleanup.json', {'schema': 1, 'launch_id': launch.name, 'state': 'active',
                                   'pid': os.getpid(), 'pid_started': started, 'at': now()})


def record_child(launch, pid, started):
    """Future launches retain their sampled child identity if runtime.json is lost."""
    record = read(launch / 'cleanup.json')
    write(launch / 'cleanup.json', {**record, 'child_pid': pid, 'child_started': started, 'child_at': now()})


def finish(launch, errors=()):
    record = read(launch / 'cleanup.json')
    write(launch / 'cleanup.json', {**record, 'state': 'failed' if errors else 'complete',
                                   'completed_at': now(), 'errors': list(errors)})


CLOSED_CONNECTION_LIMIT = 100
PENDING_ROLE_SECONDS = 60
_UNSET = object()


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


def connection_logger(launch, *, started=_UNSET):
    """Only endpoint/role/timing metadata; keep at most 100 closed connections.

    One connection-journal lock serializes updates, pruning and observation. Active, pending and unknown
    records are never pruned. Read-only recovery checks use their enclosing
    monitor/worker role rather than a durable record per short connection.
    """
    started = process_started(os.getpid()) if started is _UNSET else started
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
                             'pid': os.getpid(), 'pid_started': started, 'at': previous.get('at') or now(),
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
    def __init__(self, launch, name, *, pending=False, started=_UNSET):
        if not pending and started is _UNSET:
            started = process_started(os.getpid())
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
                write(self.path, {**current, 'state': 'active', 'pid': pid, 'pid_started': started, 'at': now()})

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


def _at(value):
    try:
        value = datetime.fromisoformat(value)
        return value.timestamp() if value.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _start_time(value):
    if isinstance(value, str) and (token := LINUX_START.fullmatch(value)):
        try:
            if Path('/proc/sys/kernel/random/boot_id').read_text().strip() == token[1]:
                boot = boot_time()
                return boot + int(token[2]) / os.sysconf('SC_CLK_TCK') if boot is not None else None
        except (OSError, ValueError):
            pass
        return None
    if not isinstance(value, str) or not START.fullmatch(value):
        return None
    try:
        _, month, day, clock, year = value.split()
        hour, minute, second = map(int, clock.split(':'))
        return datetime(int(year), 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split().index(month) + 1,
                        int(day), hour, minute, second, tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None


def _observation(pid, observations):
    if pid not in observations:
        try:
            os.kill(pid, 0)  # Observation only. Never signal a cleanup process.
        except ProcessLookupError:
            observations[pid] = (True, None)
        except OverflowError:
            observations[pid] = (False, None)  # Damaged saved PID, not proof of exit.
        except PermissionError:
            observations[pid] = (False, process_snapshot(pid))
        else:
            observations[pid] = (False, process_snapshot(pid))
    return observations[pid]


def _gone(pid, started=None, *, recorded_at=None, before_boot=False, observations=None):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if before_boot and (started is None or isinstance(started, str) and START.fullmatch(started)):
        return True  # A legacy launch cannot have survived this machine's reboot.
    absent, snapshot = _observation(pid, observations if observations is not None else {})
    if absent or (snapshot and snapshot[1][0] in ('Z', 'X')):
        return True
    if not snapshot:
        return False
    if started is not None:
        # Corrupt tokens are not proof of a different process.
        if not valid_start(started) or not valid_start(snapshot[0]):
            return False
        if bool(LINUX_START.fullmatch(started)) != bool(LINUX_START.fullmatch(snapshot[0])):
            return False  # A clock-derived legacy token is not comparable with ticks.
        return snapshot[0] != started
    boundary, current = _at(recorded_at), _start_time(snapshot[0])
    return boundary is not None and current is not None and current > boundary


def _before_boot(facts):
    started = _at(facts.get('started_at'))
    if started is None:
        return False
    # New Linux identities already name their boot; never let a wall-clock
    # adjustment override a matching tick identity.
    if any(isinstance(facts.get(key), str) and LINUX_START.fullmatch(facts[key])
           for key in ('pid_started', 'runner_started')):
        return False
    boot = boot_time()
    return boot is not None and started < boot


@lru_cache(maxsize=128)
def _command(pid, started):
    # Command name only, never arguments, environment or conversation content.
    return process_command(pid) or 'unknown (process inspection unavailable)'


def _blocked(label, path, pid, started, observations):
    valid_pid = isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
    snapshot = observations.get(pid, (False, None))[1] if valid_pid else None
    current = snapshot[0] if snapshot else 'unknown (process inspection unavailable)'
    command = _command(pid, current) if snapshot else process_command(pid) or 'unknown'
    timestamp = _start_time(current)
    started_text = (datetime.fromtimestamp(timestamp, timezone.utc).strftime('started %a %b %d %H:%M:%S %Y UTC')
                    if timestamp is not None else 'start time unavailable')
    why = ('its recorded process identity is still live' if started and started == current else
           'mv2 cannot prove that this process is unrelated to the old launch')
    if not snapshot:
        why = 'process inspection unavailable; ' + why
    return (f'The previous terminal is still closing its {label}: PID {pid}, command {command}, {started_text}; '
            f'{why}. Record: {path}. '
            'Inspect that PID and the record; wait for the verified old process to exit, then retry. '
            'If the identity is unavailable or the record is damaged, restore process inspection or repair '
            'the record only after verifying the old process has exited. Back up the named record, then remove '
            'its stale PID and start-token fields (or remove a stale cleanup/role record) and retry; '
            'never remove live or unverified process evidence')


def recorded_process_problem(directory, facts):
    """A damaged generation cannot erase the runner/child evidence it retains.

    Observation only: PID reuse and zombies use the same proof as teardown;
    malformed or unavailable identity never authorizes a signal or a restart.
    """
    observations = {}
    boundary = facts.get('stopped_at') or facts.get('dormant_at') or (facts.get('observed_at')
                                         if type(facts.get('exit_code')) is int else None)
    before_boot = _before_boot(facts)
    for key, started_key, label in (('runner_pid', 'runner_started', 'recorded runner'),
                                     ('pid', 'pid_started', 'recorded child process')):
        if key not in facts:
            continue
        pid, started = facts[key], facts.get(started_key)
        if type(pid) is not int or pid <= 0:
            return f'Cannot prove the {label} has exited: {key} is invalid in {directory / "runtime.json"}'
        if not _gone(pid, started, recorded_at=boundary, before_boot=before_boot, observations=observations):
            return _blocked(label, directory / 'runtime.json', pid, started, observations)
    return None


def attested_running(facts):
    """Positive runner/child identity only; never turn an unknown PID into proof.

    The runner can briefly outlive an exited child while cleaning up. That
    overlap is not evidence that a missing terminal record is inconsistent.
    """
    observations = {}
    if 'pid' in facts and _gone(facts['pid'], facts.get('pid_started'), observations=observations):
        return None
    for key, start, label in (('pid', 'pid_started', 'child'),
                              ('runner_pid', 'runner_started', 'runner')):
        pid, token = facts.get(key), facts.get(start)
        if type(pid) is not int or pid <= 0 or not valid_start(token):
            continue
        absent, snapshot = _observation(pid, observations)
        if (not absent and snapshot and snapshot[0] == token
                and snapshot[1] and snapshot[1][0] not in ('Z', 'X')):
            return f'recorded {label} PID {pid} is still alive with its matching process start time'
    return None


def problem(directory, facts):
    launch_id = facts.get('launch_id')
    if not launch_id:
        return None
    launch = directory / 'launches' / launch_id
    path = launch / 'cleanup.json'
    observations = {}  # One identity sample per PID per scan, never a cross-launch cache.
    before_boot = _before_boot(facts)
    def gone(record, boundary):
        return _gone(record.get('pid'), record.get('pid_started'), recorded_at=boundary,
                     before_boot=before_boot, observations=observations)
    # stopped_at/observed exit postdate process creation; started_at does not.
    exited = isinstance(facts.get('exit_code'), int) and not isinstance(facts.get('exit_code'), bool)
    exit_at = facts.get('stopped_at') or facts.get('dormant_at') or (facts.get('observed_at') if exited else None)
    if path.exists():
        try:
            record = read(path)
        except (OSError, ValueError):
            return f'The previous terminal cleanup record cannot be read: {path}; repair it before retrying'
        if not isinstance(record, dict) or record.get('launch_id') != launch_id or record.get('schema') != 1:
            return f'The previous terminal has an invalid cleanup record: {path}; repair it before retrying'
        complete = record.get('state') == 'complete'
        if not complete and not gone(record, record.get('at')):
            return _blocked('native connections', path, record.get('pid'), record.get('pid_started'), observations)
        child = {'pid': record.get('child_pid'), 'pid_started': record.get('child_started')}
        if not complete and 'child_pid' in record and not gone(child, record.get('child_at')):
            return _blocked('child process', path, child['pid'], child.get('pid_started'), observations)
        if not complete and facts.get('pid') and not gone(facts, exit_at):
            return _blocked('child process', directory / 'runtime.json', facts['pid'], facts.get('pid_started'), observations)
    else:
        runner = {'pid': facts.get('runner_pid'), 'pid_started': facts.get('runner_started')}
        if runner['pid'] and not gone(runner, exit_at):
            return _blocked('older runner (no cleanup record)', directory / 'runtime.json', runner['pid'], runner['pid_started'], observations)
        if runner['pid'] and facts.get('pid') and not gone(facts, exit_at):
            return _blocked('older child (no cleanup record)', directory / 'runtime.json', facts['pid'], facts.get('pid_started'), observations)
    try:
        with _connections_lock(launch):
            for path in (launch / 'roles').glob('*.json'):
                try:
                    role = read(path)
                except FileNotFoundError:
                    raise
                except (OSError, ValueError):
                    return f'The previous terminal connection record cannot be read: {path}; repair it before retrying'
                if not isinstance(role, dict) or role.get('launch_id') != launch_id or role.get('schema') != 1:
                    return f'The previous terminal has an invalid connection record: {path}; repair it before retrying'
                if role.get('state') == 'closed':
                    continue
                at = _at(role.get('at'))
                if (role.get('state') == 'pending' and role.get('pid') is None and at is not None
                        and datetime.now(timezone.utc).timestamp() - at >= PENDING_ROLE_SECONDS):
                    continue  # An unclaimed worker cannot own a connection; late claims recheck launch.lock.
                if not gone(role, role.get('at')):
                    return _blocked(str(role.get('role', 'native connection')), path,
                                    role.get('pid'), role.get('pid_started'), observations)
    except FileNotFoundError:
        if launch.exists():
            raise
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
