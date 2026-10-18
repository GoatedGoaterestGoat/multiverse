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

    def test_a_failed_wake_is_retryable_only_on_exact_negative_evidence(self):
        """A failed wake becomes retryable only when the process has exited and its harness says False (not None, 0,
        a live process or a raising hook); fragile because it relies on each harness's startup_prompt_evidence hook,
        and a break silently replays a prompt the agent already took (14bc474)."""
        exited, live = Mock(poll=Mock(return_value=3)), Mock(poll=Mock(return_value=None))
        cases = [(True, exited, 'unknown'), (None, exited, 'unknown'), (False, exited, 'failed'),
                 (0, exited, 'unknown'), (False, live, 'unknown'), (RuntimeError('unreadable'), exited, 'unknown')]
        for evidence, process, expected in cases:
            with self.subTest(evidence=evidence, live=process is live):
                self.reset()
                hook = Mock(side_effect=evidence) if isinstance(evidence, Exception) else Mock(return_value=evidence)
                with patch('multiverse.harnesses.load', return_value=Mock(startup_prompt_evidence=hook)):
                    runner.finish_failed_wake(self.launch, {'resume_message_id': self.message['id']},
                                              self.env, process, 'unknown option')
                _, tracking = mailbox.message_record(self.directory, self.message['id'])
                self.assertEqual(tracking['submission_state'], expected)
                self.assertIn('unknown option', tracking['attempts'][-1]['reason'])
                if process is live:
                    hook.assert_not_called()
                    continue
                session, prompt, since, env = hook.call_args.args
                self.assertEqual(prompt, 'exact wake prompt')
                self.assertEqual(env, self.env)
                self.assertEqual(session.launch_id, self.launch.name)
