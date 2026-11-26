"""Wake submission evidence, concurrent startup input, and failure recovery."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native
from multiverse.universe import mailbox
from multiverse.harnesses.types import Capability, NativeResult
from multiverse.universe.mailbox import inbox, message_record, update_message
from multiverse.store import lock, now, write
from multiverse.tools import deliver, kill, send
from test_multiverse import BaseTest


class WakeTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.adapter = Mock()
        self.adapter.send.return_value = NativeResult('native_queued')
        self.adapter.stop.return_value = NativeResult('ok')
        self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', side_effect=lambda props, facts: {
            'live': 'alive' if facts['state'] in ('starting', 'running') else facts['state']}))
        self.start = self.enterContext(patch.object(lifecycle, 'start', side_effect=self.register))
        self.new_agent('wake')

    def new_agent(self, name):
        """A dormant agent of its own, and an adapter and start that have seen nothing yet."""
        self.agent, self.native_id = str(uuid4()), str(uuid4())
        self.old_launch, self.new_launch, self.replacement_launch = (str(uuid4()) for _ in range(3))
        self.directory = self.root / 'state/agents' / self.agent
        write(self.directory / 'properties.json', {'id': self.agent, 'name': name, 'task': 'original',
            'harness': 'codex', 'workspace': str(self.work), 'native_session_id': self.native_id})
        write(self.directory / 'runtime.json', {'launch_id': self.old_launch, 'state': 'dormant'})
        self.adapter.reset_mock()
        self.adapter.capabilities.return_value = {'wake': Capability('supported')}
        self.start.reset_mock()

    def register(self, root, *, resume, resume_message_id, socket_path=None):
        # Acquiring the real file lock proves send did not retain it across start.
        with lock(self.directory / '.launch.lock'):
            message, tracking = message_record(self.directory, resume_message_id)
            self.assertEqual(message['target_session'], self.native_id)
            self.assertEqual(resume, self.agent)
            write(self.directory / 'runtime.json', {'launch_id': self.new_launch, 'state': 'starting', 'startup_delivery': 'waiting',
                'tmux': {'socket': None, 'session': '$1', 'pane': '%1'}})
            update_message(self.directory, resume_message_id, submission_state='submitting',
                delivery_method='resume_prompt', wake_launch_id=self.new_launch,
                attempts=[*tracking.get('attempts', []), {'state': 'unknown'}])
        return {'runtime': agents.runtime(self.directory)}

    def messages(self):
        return inbox(self.directory, all_messages=True, limit=None)['messages']

    def test_concurrent_sends_wake_once_then_drain_in_order_without_resending_prompt(self):
        """16 concurrent sends to a stopped agent start exactly one launch and drain in order without resending the
        wake prompt; fragile because it relies on the launch lock being released before start, and a break
        double-launches or reorders."""
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: send.run(self.root, agent=self.agent, text=str(n)), range(16)))
        self.assertTrue(all(r['state'] == 'pending' and r['ok'] for r in results), results)
        self.start.assert_called_once()
        self.adapter.send.assert_not_called()
        messages = self.messages()
        first = messages[0]
        self.assertEqual(first['tracking']['submission_state'], 'submitting')
        self.assertTrue(all(m['tracking']['submission_state'] == 'pending' for m in messages[1:]))
        mailbox.finish_wake(self.directory, self.new_launch, first['id'], True)
        self.assertEqual(self.messages()[0]['tracking']['submission_state'], 'accepted')
        self.assertIsNone(self.messages()[0]['tracking']['native_receipt']['transcript_confirmed'])
        # Pre-binding recovery checks must not attempt a native call.
        checked = deliver.run(self.root, agent=self.agent, check=True)
        self.assertEqual(len(checked['messages']), 15)  # An accepted CLI wake prompt has no correlatable receipt.
        self.adapter.get_delivery.assert_not_called()
        agents.update_runtime(self.directory, self.new_launch, state='running')
        native.flush_startup(self.root, self.directory, self.new_launch)
        self.assertEqual([call.args[1].id for call in self.adapter.send.call_args_list], [m['id'] for m in messages[1:]])
        native.flush_startup(self.root, self.directory, self.new_launch)
        self.assertEqual(self.adapter.send.call_count, 15)
        self.assertEqual(native.deliver(self.root, self.agent, first['id'], retry=True)['state'], 'ok')
        self.assertEqual(native.deliver(self.root, self.agent, first['id'], check=True)['state'], 'unknown')
        self.assertTrue(all(m['tracking']['submission_state'] == 'accepted' for m in self.messages()))

    def test_every_wake_ending_leaves_messages_retryable_exactly_once(self):
        """Each way a wake launch ends (uncertain prompt, failed start, startup failure, kill, a runner that died
        before flushing), or never starts (inbox-only, unsupported), leaves its messages where --retry neither
        duplicates nor loses them; fragile because send, the runner, kill and check write that state from different
        processes."""
        with self.subTest('an uncertain wake prompt'):
            self.new_agent('uncertain')
            agents.update_runtime(self.directory, self.old_launch, state='stopped')
            send.run(self.root, agent=self.agent, text='wake')
            message = self.messages()[0]
            agents.update_runtime(self.directory, self.new_launch, state='dormant')
            self.assertEqual(native.deliver(self.root, self.agent, message['id'], retry=True)['state'], 'unknown')
            self.assertEqual(deliver.run(self.root, agent=self.agent, retry=True)['messages'], [])
            self.start.assert_called_once()
            self.adapter.send.assert_not_called()
        with self.subTest('a process that failed to start'):
            self.new_agent('failed-start')
            send.run(self.root, agent=self.agent, text='wake')
            message = self.messages()[0]
            mailbox.finish_wake(self.directory, self.new_launch, message['id'], False,
                                'Executable failed before startup')
            self.assertEqual(self.messages()[0]['tracking']['submission_state'], 'failed')
            agents.update_runtime(self.directory, self.new_launch, state='dormant')
            self.assertTrue(deliver.run(self.root, agent=self.agent, retry=True)['ok'])
            self.assertEqual(self.start.call_count, 2)
            self.adapter.send.assert_not_called()
        with self.subTest('a startup failure'):
            self.new_agent('startup-failure')
            send.run(self.root, agent=self.agent, text='wake')
            send.run(self.root, agent=self.agent, text='queued')
            native.flush_startup(self.root, self.directory, self.new_launch, 'Permissions rejected')
            queued = self.messages()[-1]
            self.assertEqual(queued['tracking']['submission_state'], 'failed')
            self.assertIn('Permissions rejected', queued['tracking']['attempts'][-1]['reason'])
            self.adapter.send.assert_not_called()
            agents.update_runtime(self.directory, self.new_launch, state='running', startup_delivery='ready')
            self.assertTrue(deliver.run(self.root, agent=self.agent, retry=True)['ok'])
            self.assertEqual(self.adapter.send.call_args.args[1].text, 'queued')
        with self.subTest('a runner that died before flushing'):
            self.new_agent('runner-died')
            send.run(self.root, agent=self.agent, text='wake')
            send.run(self.root, agent=self.agent, text='unsubmitted')
            write(self.directory / 'runtime.json', {'launch_id': self.replacement_launch, 'state': 'running'})
            deliver.run(self.root, agent=self.agent, check=True)
            self.assertEqual(self.messages()[-1]['tracking']['submission_state'], 'failed')
            self.adapter.get_delivery.assert_not_called()
            self.adapter.send.assert_not_called()
        with self.subTest('inbox-only messages and an unsupported wake'):
            self.new_agent('manual')
            manual = send.run(self.root, agent=self.agent, text='manual', delivery='inbox-only')
            self.assertEqual(manual['state'], 'queued')
            self.start.assert_not_called()
            self.adapter.capabilities.return_value = {'wake': Capability('unavailable')}
            result = send.run(self.root, agent=self.agent, text='unsupported')
            self.assertFalse(result['ok'])
            self.assertEqual(self.messages()[-1]['tracking']['submission_state'], 'failed')
            self.start.assert_not_called()

    def test_stopping_a_starting_agent_leaves_unsubmitted_messages_retryable(self):
        """Kill and startup shutdown must preserve never-submitted input for exactly one explicit retry."""
        for path in ('live kill', 'dormant kill', 'startup flush'):
            with self.subTest(path=path):
                self.new_agent(path.replace(' ', '-'))
                send.run(self.root, agent=self.agent, text='wake')
                send.run(self.root, agent=self.agent, text='held follow-up', delivery='follow-up')
                wake, held = self.messages()
                ended = self.new_launch
                mailbox.finish_wake(self.directory, ended, wake['id'], True)
                self.assertEqual(held['tracking']['submission_state'], 'pending')

                def observe(_, facts):
                    state = facts['state']
                    return {'live': 'alive' if state == 'starting' else state}

                if path == 'startup flush':
                    agents.update_runtime(self.directory, ended, state='ending', ending_at=now())
                    native.flush_startup(self.root, self.directory, ended)
                    agents.update_runtime(self.directory, ended, state='dormant')
                else:
                    backend = Mock()
                    backend.signal_child.side_effect = lambda *_: agents.update_runtime(
                        self.directory, ended, state='dormant')
                    failure = OSError('Native service unavailable') if path == 'dormant kill' else None
                    if failure is not None:
                        agents.update_runtime(self.directory, ended, state='dormant')
                    # A dormant agent has no pane, including after its ending intent is written.
                    view = (lambda *_: {'live': 'dormant'}) if failure is not None else observe
                    with patch.object(lifecycle, 'observe', side_effect=view), \
                            patch.object(lifecycle, 'owned', return_value=(backend, '%1')), \
                            patch.object(self.adapter, 'stop', Mock(return_value=NativeResult('ok'), side_effect=failure)):
                        stopped = kill.run(self.root, agent=self.agent)
                    expected = 'unknown' if failure is not None else 'ok'
                    self.assertEqual(stopped['native_cancellation']['state'], expected)

                self.assertEqual(self.messages()[0]['tracking']['submission_state'], 'accepted')
                self.assertEqual(self.messages()[1]['tracking']['submission_state'], 'failed')
                if path != 'startup flush':
                    self.assertEqual(stopped['native_cancellation']['failed_startup_messages'], 1)
                    self.assertEqual(stopped['native_cancellation']['retryable_startup_messages'], 1)
                    self.assertEqual(stopped['native_cancellation']['startup_messages_in_inbox'], 1)
                lines = []
                checked = deliver.run(self.root, agent=self.agent, check=True, _present=lines.append)
                self.assertEqual([item['state'] for item in checked['messages']], ['error'])
                self.assertTrue(any('--retry' in line for line in lines), lines)
                self.adapter.get_delivery.assert_not_called()
                self.adapter.send.assert_not_called()
                self.new_launch = self.replacement_launch
                retried = deliver.run(self.root, agent=self.agent, retry=True)
                self.assertTrue(retried['ok'])
                self.assertEqual(len(retried['messages']), 1)
                self.assertEqual(self.start.call_count, 2)
                self.assertEqual(self.start.call_args.kwargs['resume_message_id'], held['id'])
                native.flush_startup(self.root, self.directory, ended)
                self.assertEqual(agents.runtime(self.directory)['launch_id'], self.new_launch)
                self.assertEqual(deliver.run(self.root, agent=self.agent, retry=True)['messages'], [])
                self.adapter.send.assert_not_called()

