"""A startup exit is retryable only after exact harness negative evidence."""
from unittest.mock import Mock, patch
from multiverse.runtime import runner
from multiverse.store import uid, write
from multiverse.universe import mailbox
from test_multiverse import BaseTest


class FailedWakeTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state' / 'agents' / uid()
        self.launch = self.directory / 'launches' / uid()
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'wake',
            'harness': 'claude', 'workspace': str(self.work), 'native_session_id': uid()})
        write(self.directory / 'runtime.json', {'launch_id': self.launch.name, 'started_at': '2026-10-17T12:00:00+00:00'})
        self.launch.mkdir(parents=True)
        (self.launch / 'task.md').write_text('exact wake prompt')
        self.message = mailbox.append_message(self.directory, 'wake', {}, delivery='steer')
        self.env = {'MV2_UNIVERSE': str(self.root), 'CLAUDE_CONFIG_DIR': str(self.work / 'private-config')}

    def reset(self):
        mailbox.update_message(self.directory, self.message['id'], submission_state='submitting',
            wake_launch_id=self.launch.name, delivery_method='resume_prompt', attempts=[{'state': 'pending'}])

    def test_only_false_after_process_exit_allows_retry(self):
        for evidence, expected in ((True, 'unknown'), (None, 'unknown'), (False, 'failed'), (0, 'unknown')):
            with self.subTest(evidence=evidence):
                self.reset()
                adapter = Mock(startup_prompt_evidence=Mock(return_value=evidence))
                with patch.object(runner.__import__('multiverse.harnesses'), 'load') if False else                         patch('multiverse.harnesses.load', return_value=adapter):
                    runner.finish_failed_wake(self.launch, {'resume_message_id': self.message['id']},
                                              self.env, Mock(poll=Mock(return_value=3)), 'unknown option')
                _, tracking = mailbox.message_record(self.directory, self.message['id'])
                self.assertEqual(tracking['submission_state'], expected)
                self.assertIn('unknown option', tracking['attempts'][-1]['reason'])
                session, prompt, since, env = adapter.startup_prompt_evidence.call_args.args
                self.assertEqual(prompt, 'exact wake prompt')
                self.assertEqual(env, self.env)
                self.assertEqual(session.launch_id, self.launch.name)

    def test_live_process_or_bad_hook_cannot_establish_nonconsumption(self):
        for live, outcome in ((True, False), (False, RuntimeError('unreadable evidence'))):
            self.reset()
            hook = Mock(return_value=outcome) if not isinstance(outcome, Exception) else Mock(side_effect=outcome)
            with patch('multiverse.harnesses.load', return_value=Mock(startup_prompt_evidence=hook)):
                runner.finish_failed_wake(self.launch, {'resume_message_id': self.message['id']},
                                          self.env, Mock(poll=Mock(return_value=None if live else 3)), 'startup failed')
            self.assertEqual(mailbox.message_record(self.directory, self.message['id'])[1]['submission_state'], 'unknown')
            if live:
                hook.assert_not_called()
