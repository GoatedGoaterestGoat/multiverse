"""Continue an owned accepted queue head on idle, without submitting input again."""
from contextlib import contextmanager
import fcntl
import threading

from ..harnesses.types import NativeResult
from ..store import now
from ..universe import agents, mailbox
from . import native


@contextmanager
def _try_lock(directory, name):
    # Match delivery's launch -> delivery -> inbox order, but never wait for
    # another operation or recreate a removed agent directory.
    with (directory / name).open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class QueueContinuation:
    """One runner-owned worker. The status loop supplies idle notifications.

    A cheap inbox-state stat catches acceptance written after the idle event.
    Completed tokens suppress repeated checks; token capture precedes the scan
    so newer acceptance can never be swallowed by post-scan bookkeeping.
    """
    def __init__(self, agent_dir, launch_id, root):
        self.agent_dir, self.launch_id, self.root = agent_dir, launch_id, root
        self.closed = False
        self.started = False
        self.pending = self.active = self.completed = None
        self.checked = None
        self.mutex = threading.Lock()
        self.wakeup = threading.Event()
        self.thread = threading.Thread(target=self._run, name='native-queue-continuation', daemon=True)

    def start(self):
        with self.mutex:
            if self.closed or self.started:
                return
            self.started = True
            self.thread.start()

    def close(self):
        with self.mutex:
            self.closed = True
            self.pending = None
            self.wakeup.set()
            started = self.started
        if started:
            self.thread.join(6)  # Adapter RPC is bounded to five seconds.
            if self.thread.is_alive():
                raise TimeoutError('Accepted queue continuation is still closing')

    def _eligible(self, properties, facts):
        return (facts.get('launch_id') == self.launch_id and facts.get('state') == 'running'
                and facts.get('status') == 'idle' and facts.get('startup_checked') is True
                and not facts.get('ending_at') and not facts.get('native_refusal')
                and not facts.get('binding_conflict') and properties.get('native_session_id')
                and facts.get('startup_delivery') not in ('waiting', 'failed')
                and (facts.get('native_relay') or {}).get('state') == 'ready'
                and (facts.get('attachment') or {}).get('readiness') in ('binding', 'legacy-binding'))

    def notify(self, facts=None, properties=None):
        with self.mutex:
            if self.closed:
                return False
        try:
            facts = agents.runtime(self.agent_dir) if facts is None else facts
            if properties is None:
                from ..universe.records import properties as read_properties
                properties = read_properties(self.agent_dir / 'properties.json')
            if not self._eligible(properties, facts):
                return False
            version = facts.get('native_status_version', 0)
            if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                return False
            stat = (self.agent_dir / 'inbox-state.json').stat()
            token = (properties['native_session_id'], version, stat.st_dev, stat.st_ino,
                     stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        except (OSError, ValueError, TypeError, KeyError):
            return False
        with self.mutex:
            if self.closed or token in (self.completed, self.active, self.pending):
                return False
            self.pending = token
            self.wakeup.set()
        return True

    def _accepted(self, session):
        accepted = {}
        queue_ids = set()
        for message in mailbox.inbox(self.agent_dir, limit=None, all_messages=True)['messages']:
            tracking = message['tracking']
            if (message.get('schema') != 2 or message.get('delivery') != 'follow-up'
                    or tracking.get('submission_state') != 'accepted'
                    or tracking.get('delivery_method') != 'native'
                    or (tracking.get('target_session') or message.get('target_session')) != session.native_id):
                continue
            attempts = tracking.get('attempts') or []
            if not attempts or not isinstance(attempts[-1], dict):
                continue
            attempt = attempts[-1]
            if attempt.get('launch_id') != self.launch_id or attempt.get('native_session_id') != session.native_id:
                continue
            receipt = tracking.get('native_receipt')
            if (not isinstance(receipt, dict) or receipt.get('transcript_confirmed') is True
                    or not isinstance(receipt.get('queued_submission_id'), str) or not receipt['queued_submission_id']):
                continue
            queued = receipt['queued_submission_id']
            if queued in queue_ids:
                raise ValueError('Accepted receipts contain a duplicate native queue ID')
            queue_ids.add(queued)
            accepted[message['id']] = queued
        return accepted

    def _diagnostic(self, result):
        value = {'state': result.state, 'reason': result.reason, 'value': result.value}
        def changes(facts):
            if facts.get('ending_at') or facts.get('state') != 'running':
                return None
            previous = facts.get('queue_continuation') or {}
            if value == {key: previous.get(key) for key in value}:
                return None
            return {'queue_continuation': {**value, 'at': now()}}
        agents.mutate_runtime(self.agent_dir, self.launch_id, changes)

    def _work(self, token):
        try:
            with _try_lock(self.agent_dir, '.launch.lock'), _try_lock(self.agent_dir, '.delivery.lock'):
                with self.mutex:
                    if self.closed:
                        return True
                _, adapter, session, executor = native.context(self.root, self.agent_dir.name, reconcile=True)
                if (not self._eligible(executor.properties, executor.facts) or session.native_id != token[0]
                        or executor.facts.get('native_status_version', 0) != token[1]):
                    return True
                hook = native.adapter_hook(adapter, 'continue_queued')
                if hook is None:
                    return True
                accepted = self._accepted(session)
                if not accepted:
                    return True
                selection = (token[0], token[1], tuple(sorted(accepted.items())))
                if selection == self.checked:
                    return True  # Receipt-only bookkeeping is not new accepted work.
                current = agents.runtime(self.agent_dir)
                if (not self._eligible(executor.properties, current)
                        or current.get('native_status_version', 0) != token[1]):
                    return True
                executor.facts = current
                executor.check_session()
                with self.mutex:
                    if self.closed:
                        return True
                    self.checked = selection  # Claim before closing; ambiguous starts never repeat blindly.
                result = hook(session, accepted, executor)
                if not isinstance(result, NativeResult):
                    raise ValueError('Queue continuation returned no native result')
                self._diagnostic(result)  # Never changes message acceptance or receipts.
                return True
        except BlockingIOError:
            return False  # The next idle stat can retry acquiring the locks.
        except Exception as error:
            # Transport/ambiguous start preserves acceptance. A later status or
            # accepted-state change may re-inventory, never resend text.
            try:
                self._diagnostic(NativeResult('error', str(error)))
            except (OSError, ValueError, TypeError):
                pass
            return True

    def _run(self):
        while True:
            self.wakeup.wait()
            with self.mutex:
                if self.closed:
                    return
                token, self.pending = self.pending, None
                self.active = token
                self.wakeup.clear()
            if token is None:
                continue
            completed = self._work(token)
            with self.mutex:
                if completed:
                    self.completed = token
                self.active = None
