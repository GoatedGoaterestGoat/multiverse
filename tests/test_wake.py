"""Wake submission evidence, concurrent startup input, and failure recovery."""
from concurrent.futures import ThreadPoolExecutor
import os
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native
from multiverse.universe import mailbox
from multiverse.harnesses.types import Capability, NativeResult
from multiverse.universe.mailbox import inbox, message_record, update_message
from multiverse.store import lock, read, write
from multiverse.tools import deliver, send
from test_multiverse import BaseTest, TerminalBase, until


class WakeTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.native_id = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        write(self.directory / 'properties.json', {'id': self.agent, 'name': 'wake', 'task': 'original',
            'harness': 'codex', 'workspace': str(self.work), 'native_session_id': self.native_id})
        write(self.directory / 'runtime.json', {'launch_id': 'old', 'state': 'dormant'})
        self.adapter = Mock()
        self.adapter.capabilities.return_value = {'wake': Capability('supported')}
        self.adapter.send.return_value = NativeResult('native_queued')
        self.adapter.stop.return_value = NativeResult('ok')
        self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', side_effect=lambda props, facts: {
            'live': 'alive' if facts['state'] in ('starting', 'running') else facts['state']}))
        self.start = self.enterContext(patch.object(lifecycle, 'start', side_effect=self.register))

    def register(self, root, *, resume, resume_message_id, socket_path=None):
        # Acquiring the real file lock proves send did not retain it across start.
        with lock(self.directory / '.launch.lock'):
            message, tracking = message_record(self.directory, resume_message_id)
            self.assertEqual(message['target_session'], self.native_id)
            self.assertEqual(resume, self.agent)
            write(self.directory / 'runtime.json', {'launch_id': 'new', 'state': 'starting', 'startup_delivery': 'waiting'})
            update_message(self.directory, resume_message_id, submission_state='submitting',
                delivery_method='resume_prompt', wake_launch_id='new',
                attempts=[*tracking.get('attempts', []), {'state': 'unknown'}])
        return {'runtime': agents.runtime(self.directory)}

    def messages(self):
        return inbox(self.directory, all_messages=True, limit=None)['messages']

    def test_concurrent_sends_wake_once_then_drain_in_order_without_resending_prompt(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: send.run(self.root, agent=self.agent, text=str(n)), range(16)))
        self.assertTrue(all(r['state'] == 'pending' and r['ok'] for r in results), results)
        self.start.assert_called_once()
        self.adapter.send.assert_not_called()
        messages = self.messages()
        first = messages[0]
        self.assertEqual(first['tracking']['submission_state'], 'submitting')
        self.assertTrue(all(m['tracking']['submission_state'] == 'pending' for m in messages[1:]))
        mailbox.finish_wake(self.directory, 'new', first['id'], True)
        self.assertEqual(self.messages()[0]['tracking']['submission_state'], 'accepted')
        self.assertIsNone(self.messages()[0]['tracking']['native_receipt']['transcript_confirmed'])
        # Pre-binding recovery checks must not attempt a native call.
        checked = deliver.run(self.root, agent=self.agent, check=True)
        self.assertEqual(len(checked['messages']), 15)
        self.adapter.get_delivery.assert_not_called()
        agents.update_runtime(self.directory, 'new', state='running')
        native.flush_startup(self.root, self.directory, 'new')
        self.assertEqual([call.args[1].id for call in self.adapter.send.call_args_list], [m['id'] for m in messages[1:]])
        native.flush_startup(self.root, self.directory, 'new')
        self.assertEqual(self.adapter.send.call_count, 15)
        self.assertEqual(native.deliver(self.root, self.agent, first['id'], retry=True)['state'], 'ok')
        self.assertEqual(native.deliver(self.root, self.agent, first['id'], check=True)['state'], 'unknown')
        self.assertTrue(all(m['tracking']['submission_state'] == 'accepted' for m in self.messages()))

    def test_manual_messages_never_wake_and_unsupported_wake_is_saved(self):
        manual = send.run(self.root, agent=self.agent, text='manual', delivery='inbox-only')
        self.assertEqual(manual['state'], 'queued')
        self.start.assert_not_called()
        self.adapter.capabilities.return_value = {'wake': Capability('unavailable')}
        result = send.run(self.root, agent=self.agent, text='unsupported')
        self.assertFalse(result['ok'])
        self.assertEqual(self.messages()[-1]['tracking']['submission_state'], 'failed')
        self.start.assert_not_called()

    def test_stopped_wrapper_can_wake_and_uncertain_prompt_never_replays(self):
        agents.update_runtime(self.directory, 'old', state='stopped')
        send.run(self.root, agent=self.agent, text='wake')
        message = self.messages()[0]
        agents.update_runtime(self.directory, 'new', state='dormant')
        self.assertEqual(native.deliver(self.root, self.agent, message['id'], retry=True)['state'], 'unknown')
        self.assertEqual(deliver.run(self.root, agent=self.agent, retry=True)['messages'], [])
        self.start.assert_called_once()
        self.adapter.send.assert_not_called()

    def test_failed_process_start_can_be_retried_and_is_not_native_acceptance(self):
        send.run(self.root, agent=self.agent, text='wake')
        message = self.messages()[0]
        mailbox.finish_wake(self.directory, 'new', message['id'], False, 'Executable failed before startup')
        self.assertEqual(self.messages()[0]['tracking']['submission_state'], 'failed')
        agents.update_runtime(self.directory, 'new', state='dormant')
        self.assertTrue(deliver.run(self.root, agent=self.agent, retry=True)['ok'])
        self.assertEqual(self.start.call_count, 2)
        self.adapter.send.assert_not_called()

    def test_startup_failure_preserves_never_submitted_input_for_explicit_retry(self):
        send.run(self.root, agent=self.agent, text='wake')
        send.run(self.root, agent=self.agent, text='queued')
        native.flush_startup(self.root, self.directory, 'new', 'Permissions rejected')
        queued = self.messages()[-1]
        self.assertEqual(queued['tracking']['submission_state'], 'failed')
        self.assertIn('Permissions rejected', queued['tracking']['attempts'][-1]['reason'])
        self.adapter.send.assert_not_called()
        agents.update_runtime(self.directory, 'new', state='running', startup_delivery='ready')
        self.assertTrue(deliver.run(self.root, agent=self.agent, retry=True)['ok'])
        self.assertEqual(self.adapter.send.call_args.args[1].text, 'queued')

    def test_kill_cancels_startup_queue_and_a_replacement_never_inherits_it(self):
        send.run(self.root, agent=self.agent, text='wake')
        send.run(self.root, agent=self.agent, text='cancel me')
        stopped = native.stop(self.root, self.agent)
        self.assertEqual(stopped.value['cancelled_startup_messages'], 1)
        self.assertEqual(self.messages()[-1]['tracking']['submission_state'], 'cancelled')
        write(self.directory / 'runtime.json', {'launch_id': 'replacement', 'state': 'running'})
        native.flush_startup(self.root, self.directory, 'new')
        self.adapter.send.assert_not_called()
        self.assertEqual(agents.runtime(self.directory)['launch_id'], 'replacement')

    def test_check_recovers_queue_after_a_runner_died_before_flushing(self):
        send.run(self.root, agent=self.agent, text='wake')
        send.run(self.root, agent=self.agent, text='unsubmitted')
        write(self.directory / 'runtime.json', {'launch_id': 'replacement', 'state': 'running'})
        deliver.run(self.root, agent=self.agent, check=True)
        self.assertEqual(self.messages()[-1]['tracking']['submission_state'], 'failed')
        self.adapter.get_delivery.assert_not_called()
        self.adapter.send.assert_not_called()


class WakeRunnerTest(TerminalBase):
    def test_runner_records_failed_prompt_startup_without_claiming_acceptance(self):
        agent_id, native_id = str(uuid4()), str(uuid4())
        directory = self.root / 'state/agents' / agent_id
        write(directory / 'properties.json', {'id': agent_id, 'universe_id': 'test', 'name': 'failed-wake',
            'task': 'original task', 'harness': 'claude', 'workspace': str(self.work), 'native_session_id': native_id})
        broken = self.work / 'broken-native'
        broken.write_text('#!/nonexistent/interpreter\n')
        broken.chmod(0o755)
        config = self.root / 'harnesses/claude/harness.json'
        spec = read(config)
        spec['settings']['executable'] = str(broken)
        write(config, spec)
        with patch.dict(os.environ, {'MV2_TMUX_SOCKET': self.socket}):
            result = send.run(self.root, agent=agent_id, text='Preserve this prompt', sender={'name': 'user'})
        self.assertTrue(result['saved'])
        saved = inbox(directory, all_messages=True)['messages'][0]
        until(lambda: message_record(directory, saved['id'])[1].get('submission_state') == 'failed')
        tracking = message_record(directory, saved['id'])[1]
        self.assertEqual(tracking['delivery_method'], 'resume_prompt')
        self.assertEqual(tracking['attempts'][-1]['state'], 'error')
        self.assertIn('No such file or directory', tracking['attempts'][-1]['reason'])
        self.assertEqual(tracking['attempts'][-1]['reason'], agents.runtime(directory)['reason'])
        request = read(directory / 'launches' / tracking['wake_launch_id'] / 'request.json')
        self.assertEqual(request['argv'][-1], 'Message from user:\nPreserve this prompt')
        self.assertEqual(read(directory / 'properties.json')['task'], 'original task')
