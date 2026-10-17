"""Bounded, read-only qualification for an idle native terminal attachment.

The detached worker outlives the old pane runner. It never starts a daemon,
submits a prompt, cancels native work or modifies message receipts.
"""
from datetime import datetime, timezone
from pathlib import Path
import os
import signal
import subprocess
import sys
import threading
import time

from .. import harnesses
from ..harnesses.types import NativeActivity, NativeResult, RpcSpec, SessionHandle
from ..store import existing_lock, now, read
from ..terminal.tmux import owned, matches
from ..universe import agents
from ..universe.mailbox import inbox
from . import lifecycle, rpc
from .native import recovery_unavailable
from . import attachments
from .process import process_started

PROOF_AGE = 15
BACKOFF = (0, 1, 2, 4, 8)
RESTART_WINDOW = 300
RESTART_LIMIT = 2


def age(stamp):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return float('inf')


def in_flight(directory, facts, native_id, since=None):
    if facts.get('startup_delivery') not in (None, 'ready') or not facts.get('startup_checked'):
        return 'Launch input is still binding or being delivered'
    for message in inbox(directory, all_messages=True, limit=None)['messages']:
        if message.get('delivery') == 'inbox-only':
            continue
        tracking = message['tracking']
        if tracking.get('target_session') not in (None, native_id):
            continue
        if tracking.get('submission_state') in ('pending', 'submitting', 'unknown'):
            return 'A message is still in flight or its submission is uncertain; check delivery before resuming'
        if tracking.get('submission_state') == 'accepted':
            attempts = tracking.get('attempts') or []
            cutoff = since or (facts.get('recovery_snapshot') or {}).get('at')
            try:
                accepted = datetime.fromisoformat(attempts[-1]['at'])
                checked = datetime.fromisoformat(cutoff)
                if accepted < checked:
                    continue
            except (IndexError, KeyError, TypeError, ValueError):
                pass
            return 'A message was accepted after the idle check; waiting for a fresh idle reading'
    return None


def eligibility(properties, facts):
    if facts.get('native_refusal'):
        return 'Native launch was refused; a new explicit resume is required'
    proof = facts.get('recovery_snapshot') or {}
    session = SessionHandle.from_records(properties, facts, Path('.'))
    if reason := recovery_unavailable(session):
        return reason
    if (facts.get('ending_at') or facts.get('state') != 'running'
            or not properties.get('native_session_id')):
        return 'Launch is ending, unbound or not running'
    if (facts.get('native_relay') or {}).get('detail', '').startswith('terminal connection closed'):
        return 'Native terminal exited; manual exits are not automatically resumed'
    if proof.get('safe') is not True:
        return proof.get('reason') or 'No recent proof of an idle thread without background processes'
    if (proof.get('native_id') != properties['native_session_id']
            or proof.get('service') != facts.get('native_service')
            or proof.get('workspace') != properties.get('workspace')
            or proof.get('status_version') != facts.get('native_status_version', 0)):
        return 'Native activity or session identity changed since the idle reading'
    if not 0 <= age(proof.get('at')) <= PROOF_AGE:
        return 'The last idle reading expired; safe recovery cannot be confirmed'
    recent = [entry for entry in facts.get('automatic_restarts', [])
              if 0 <= age(entry.get('at')) < RESTART_WINDOW]
    if len(recent) >= RESTART_LIMIT:
        return 'Automatic restart limit reached; check the native service and resume manually'
    return None


def check(properties, facts, directory):
    session = SessionHandle.from_records(properties, facts, directory)
    if reason := recovery_unavailable(session):
        return NativeResult('unavailable', reason)
    adapter = harnesses.load(directory.parents[2], properties['harness'])
    describe = getattr(adapter, 'attachment_recovery', None)
    specification = describe(session) if describe else None
    if specification is None:
        return NativeResult('unavailable', 'Adapter provides no attachment safety observation')
    if (not isinstance(specification, RpcSpec) or session.service.endpoint.transport != 'unix-websocket'
            or specification.socket_path != session.service.endpoint.address):
        return NativeResult('unavailable', 'Attachment observation targets a different or unsupported service')
    # Both production callers own a monitor/worker role until this bounded
    # read-only connection closes. It cannot retain a conversation subscription.
    result = rpc.execute(specification, timeout=3)
    observed = result.value
    if result.state != 'ok':
        return result
    if (not isinstance(observed, NativeActivity) or observed.native_id != session.native_id
            or observed.workspace != session.workspace or observed.service != session.service):
        return NativeResult('unavailable', 'Native activity has no exact service/session/workspace binding')
    if (observed.status != 'idle' or any(value is not False for value in (
            observed.active_work, observed.waiting, observed.queued_input, observed.background_work))):
        return NativeResult('unavailable', 'Native activity is busy, waiting or unknown')
    return result


class RecoveryMonitor:
    """Keep a recent pre-disconnect proof; native activity invalidates it."""
    def __init__(self, directory, launch_id, root):
        self.directory, self.launch_id, self.root = directory, launch_id, root
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='native-recovery-proof', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(5)
        if self.thread.is_alive():
            raise TimeoutError('Native recovery observation is still closing')

    def _run(self):
        while not self.stopped.wait(1):
            try:
                # Sends hold launch.lock through submission. Take the cutoff
                # under that lock before reading native state so an acceptance
                # racing this RPC cannot leave an apparently idle proof.
                with existing_lock(self.directory / '.launch.lock'):
                    facts = agents.runtime(self.directory)
                    if facts.get('launch_id') != self.launch_id or facts.get('ending_at'):
                        return
                    if (facts.get('native_relay') or {}).get('state') == 'lost':
                        return
                    if facts.get('state') != 'running' or facts.get('status') != 'idle':
                        continue
                    properties = read(self.directory / 'properties.json')
                    if recovery_unavailable(SessionHandle.from_records(properties, facts, self.directory)):
                        return
                    if not properties.get('native_session_id') or not facts.get('startup_checked'):
                        continue
                    version = facts.get('native_status_version', 0)
                    started_at = now()
                    if in_flight(self.directory, facts, properties['native_session_id'], since=started_at):
                        continue
                result = check(properties, facts, self.directory)
                with existing_lock(self.directory / '.launch.lock'):
                    current = agents.runtime(self.directory)
                    if (current.get('launch_id') != self.launch_id or current.get('state') != 'running'
                            or current.get('status') != 'idle' or current.get('ending_at')
                            or (current.get('native_relay') or {}).get('state') == 'lost'
                            or current.get('native_service') != facts.get('native_service')
                            or read(self.directory / 'properties.json').get('native_session_id') != properties['native_session_id']
                            or current.get('native_status_version', 0) != version):
                        continue
                    pending = in_flight(self.directory, current, properties['native_session_id'], since=started_at)
                    agents.update_runtime(self.directory, self.launch_id, recovery_snapshot={
                        'safe': result.state == 'ok' and not pending, 'reason': pending or result.reason,
                        'service': facts.get('native_service'), 'workspace': properties['workspace'],
                        'native_id': properties['native_session_id'], 'status_version': version, 'at': started_at})
                self.stopped.wait(3)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                # Failed observations cannot create proof; any old proof expires.
                self.stopped.wait(3)


def schedule(directory, launch_id):
    """Called only on primary relay loss; do not spawn workers for unsafe rows."""
    facts = agents.runtime(directory)
    if facts.get('launch_id') != launch_id or facts.get('ending_at'):
        return
    reason = eligibility(read(directory / 'properties.json'), facts)
    if reason:
        agents.record_recovery(directory, launch_id, 'skipped', 'Automatic reconnect skipped: ' + reason)
        return
    with existing_lock(directory / '.launch.lock'):
        facts = agents.runtime(directory)
        if facts.get('launch_id') != launch_id or facts.get('ending_at'):
            return
        if (facts.get('auto_recovery') or {}).get('state') in ('checking', 'restarting', 'resuming'):
            return
        properties = read(directory / 'properties.json')
        reason = eligibility(properties, facts) or in_flight(directory, facts, properties.get('native_session_id'))
        if reason:
            agents.record_recovery(directory, launch_id, 'skipped', 'Automatic reconnect skipped: ' + reason)
            return
        agents.record_recovery(directory, launch_id, 'checking', 'Checking for safe automatic reconnect', attempts=0)
        worker = attachments.Role(directory / 'launches' / launch_id, 'recovery worker', pending=True)
        agents.update_runtime(directory, launch_id, recovery_worker=str(worker.path.relative_to(directory)))
        try:
            subprocess.Popen([sys.executable, '-m', 'multiverse.runtime.recovery', str(directory), launch_id, str(worker.path)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as error:
            worker.close()
            agents.record_recovery(directory, launch_id, 'failed', f'Could not start automatic reconnect: {error}')


def frontend_children(pid):
    result = subprocess.run(['ps', '-axo', 'pid=,ppid='], capture_output=True, text=True, timeout=3, check=True)
    return any(len(fields := line.split()) == 2 and fields[1] == str(pid) for line in result.stdout.splitlines())


def run(directory, launch_id, role_path=None):
    try:
        with existing_lock(directory / '.launch.lock'):
            facts = agents.runtime(directory)
            if facts.get('launch_id') != launch_id or not (directory / 'properties.json').is_file():
                return
            role_path = role_path or (directory / facts['recovery_worker'] if facts.get('recovery_worker') else None)
            if role_path and Path(role_path).resolve().parent != (directory / 'launches' / launch_id / 'roles').resolve():
                raise ValueError('Recovery worker belongs to a different launch')
            worker = attachments.Role.existing(role_path) if role_path else attachments.Role(directory / 'launches' / launch_id, 'recovery worker')
            worker.claimed(os.getpid(), process_started(os.getpid()))
    except FileNotFoundError:
        return
    try:
        return _run(directory, launch_id, worker)
    finally:
        worker.close()


def _run(directory, launch_id, worker):
    root = directory.parents[2]
    def current():
        facts = agents.runtime(directory)
        if (facts.get('launch_id') != launch_id or facts.get('ending_at')
                or (facts.get('auto_recovery') or {}).get('state') != 'checking'):
            raise ValueError('Automatic reconnect cancelled or superseded')
        proof = facts.get('recovery_snapshot') or {}
        properties = read(directory / 'properties.json')
        if (proof.get('safe') is not True or proof.get('workspace') != properties.get('workspace')
                or proof.get('native_id') != properties.get('native_session_id')
                or proof.get('service') != facts.get('native_service')
                or recovery_unavailable(SessionHandle.from_records(properties, facts, directory))
                or proof.get('status_version') != facts.get('native_status_version', 0)):
            raise ValueError('Native activity invalidated the idle proof')
        return facts
    try:
        for attempt, delay in enumerate(BACKOFF, 1):
            if delay:
                time.sleep(delay)
            with existing_lock(directory / '.launch.lock'):
                facts = current()
                properties = read(directory / 'properties.json')
                pending = in_flight(directory, facts, properties.get('native_session_id'))
                if pending:
                    agents.record_recovery(directory, launch_id, 'skipped', 'Automatic reconnect skipped: ' + pending)
                    return
                agents.record_recovery(directory, launch_id, 'checking', 'Waiting for the existing native endpoint', attempts=attempt)
            try:
                observed = check(properties, facts, directory)
            except (OSError, RuntimeError, TimeoutError) as error:
                if attempt == len(BACKOFF):
                    agents.record_recovery(directory, launch_id, 'failed', f'Automatic reconnect timed out: {error}', attempts=attempt)
                continue
            if observed.state != 'ok':
                agents.record_recovery(directory, launch_id, 'skipped', 'Automatic reconnect skipped: ' + observed.reason)
                return
            with existing_lock(directory / '.launch.lock'):
                facts = current()
                properties = read(directory / 'properties.json')
                pending = in_flight(directory, facts, properties.get('native_session_id'))
                if pending:
                    agents.record_recovery(directory, launch_id, 'skipped', 'Automatic reconnect skipped: ' + pending)
                    return
                backend, pane = owned(properties, facts)
                if frontend_children(facts.get('pid')):
                    agents.record_recovery(directory, launch_id, 'skipped', 'Automatic reconnect skipped: terminal child processes are running')
                    return
                agents.record_recovery(directory, launch_id, 'restarting', 'Restarting the idle terminal after native relay loss')
                agents.update_runtime(directory, launch_id, state='ending', ending_at=now())
                backend.signal_child(properties, agents.runtime(directory), signal.SIGTERM)
            # Never wait under launch.lock: the old runner needs it to finish.
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                latest = agents.runtime(directory)
                if latest.get('launch_id') != launch_id or (latest.get('auto_recovery') or {}).get('state') != 'restarting':
                    return
                panes, error = backend.snapshot()
                if error:
                    raise RuntimeError(error)
                actual = panes.get(pane)
                if not matches(actual, properties, latest):
                    raise ValueError('Terminal ownership changed during automatic reconnect')
                if actual['pane_dead'] == '1':
                    break
                time.sleep(.1)
            else:
                raise TimeoutError('Idle terminal did not exit; wrapper retained for manual recovery')
            # Qualification has finished and all of its RPC contexts are closed.
            # Release this old-launch role before requesting the new attachment.
            worker.close()
            result = lifecycle.start(root, resume=directory.name, wait=True, wait_timeout=30,
                                  _recovery={'launch_id': launch_id, 'terminal': facts['tmux'],
                                             'service': facts['native_service'],
                                             'native_id': properties['native_session_id']})
            if not result['ready']:
                latest = agents.runtime(directory)
                if ((latest.get('auto_recovery') or {}).get('previous_launch_id') == launch_id
                        and latest.get('launch_id') == result['runtime']['launch_id']):
                    agents.record_recovery(directory, latest['launch_id'], 'failed',
                           'Automatic reconnect failed: ' + result.get('error', 'native launch did not become ready'))
            return
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # A concurrent manual stop/resume owns the replacement and its errors.
        facts = agents.runtime(directory)
        if (facts.get('launch_id') == launch_id
                and (facts.get('auto_recovery') or {}).get('state') in ('checking', 'restarting')):
            agents.record_recovery(directory, launch_id, 'failed', 'Automatic reconnect failed: ' + str(error))


def reconnected(directory, launch_id):
    facts = agents.runtime(directory)
    if (facts.get('auto_recovery') or {}).get('state') == 'resuming':
        agents.record_recovery(directory, launch_id, 'reconnected', 'Reconnected automatically after native relay loss')


if __name__ == '__main__':
    run(Path(sys.argv[1]), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
