"""Release a runner only after its exact launch parent finishes preparing the pane."""
import os
import time

from ..store import now, read, write
from .process import process_started, process_snapshot, valid_start, LINUX_START

TOTAL_TIMEOUT = 180
STALL_TIMEOUT = 60
LEGACY_TIMEOUT = 15


class Progress:
    """Coarse, metadata-only preparation stages; no arguments, environment or conversation data."""
    def __init__(self, launch):
        self.launch = launch
        self.record = {'schema': 1, 'launch_id': launch.name, 'parent_pid': os.getpid(),
                       'parent_started': process_started(os.getpid()), 'sequence': -1}
        self.mark('preparing launch')

    def mark(self, stage, *, state='preparing'):
        self.record.update(sequence=self.record['sequence'] + 1, stage=stage, state=state, at=now())
        write(self.launch / 'registration.json', self.record)

    def release(self):
        self.mark('releasing runner', state='complete')
        (self.launch / 'ready').touch()

    def fail(self):
        try:
            self.mark(self.record['stage'], state='failed')
        except OSError:
            pass  # Never hide the parent's original preparation error.


def _owner(record, launch):
    if (not isinstance(record, dict) or type(record.get('schema')) is not int or record['schema'] != 1
            or record.get('launch_id') != launch.name
            or type(record.get('parent_pid')) is not int or record['parent_pid'] <= 0
            or (record.get('parent_started') is not None and not isinstance(record['parent_started'], str))
            or type(record.get('sequence')) is not int or record['sequence'] < 0
            or not isinstance(record.get('stage'), str) or not record['stage']
            or record.get('state') not in ('preparing', 'complete', 'failed')):
        raise RuntimeError('Invalid launch registration record')
    return record['parent_pid'], record.get('parent_started')


def _parent_gone(owner):
    pid, started = owner
    snapshot = process_snapshot(pid)
    if snapshot is not None:
        current, state = snapshot
        if state[0] in ('Z', 'X'):
            return True
        if started is not None:
            if valid_start(started) and valid_start(current) and bool(LINUX_START.fullmatch(started)) == bool(LINUX_START.fullmatch(current)):
                return started != current  # Compare identities only in the same clock domain.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass  # Unknown identity/liveness is bounded by the same progress and total limits.
    return False


def wait_for_release(launch, *, total_timeout=TOTAL_TIMEOUT, stall_timeout=STALL_TIMEOUT):
    """Wait through progressing preparation, fail promptly on a dead parent, always stay bounded.

    Old/manual requests without a parent record retain their previous bounded wait.
    Neither liveness nor a progress update releases input: only the ready marker does.
    """
    started = last_progress = time.monotonic()
    owner, sequence, next_check = None, -1, started
    missing = object()
    while not (launch / 'ready').exists():
        observed = time.monotonic()
        try:
            record = read(launch / 'registration.json')
        except FileNotFoundError:
            record = missing
        if record is missing:
            if owner is not None:
                raise RuntimeError('Launch registration record disappeared')
            if observed - started >= LEGACY_TIMEOUT:
                raise RuntimeError('Spawn did not finish registering this pane')
        else:
            current_owner = _owner(record, launch)
            if owner is not None and current_owner != owner:
                raise RuntimeError('Launch registration parent changed')
            owner = current_owner
            if record['sequence'] < sequence:
                raise RuntimeError('Launch registration progress moved backwards')
            if record['sequence'] > sequence:
                sequence, last_progress = record['sequence'], observed
            stage = record['stage']
            if record['state'] == 'failed':
                raise RuntimeError(f'Launch preparation failed while {stage}')
            if observed >= next_check:
                if _parent_gone(owner):
                    # The parent can publish ready and exit after our stage
                    # snapshot. Its normal exit is not failed registration.
                    if (launch / 'ready').exists():
                        return
                    raise RuntimeError(f'Launch registration parent exited while {stage}')
                next_check = observed + 1
            if observed - started >= total_timeout:
                raise RuntimeError(f'Launch registration exceeded {total_timeout:g} seconds while {stage}')
            if observed - last_progress >= stall_timeout:
                raise RuntimeError(f'Launch registration stopped making progress while {stage}')
        time.sleep(.05)
