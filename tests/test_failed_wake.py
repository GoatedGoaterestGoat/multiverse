"""A startup exit is retryable only after exact harness negative evidence."""
import json
import os
import sys
from unittest.mock import Mock, patch
from multiverse.harnesses.claude import project_key
from multiverse.runtime import runner
from multiverse.store import read, uid, write
from multiverse.tools import deliver
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
            wake_launch_id=self.launch.name, delivery_method='resume_prompt', wake_prompt_context=None,
            transcript_confirmed_at=None, attempts=[{'state': 'pending'}])

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

    def test_check_revisits_failed_wake_evidence_without_replaying_the_prompt(self):
        """An unreadable startup transcript must not strand a wake or authorize replay against a later launch's evidence."""
        config = self.work / 'private-config'
        project = config / 'projects' / project_key(os.path.realpath(self.work))  # as Claude names the launch's folder
        project.mkdir(parents=True)
        self.addCleanup(project.chmod, 0o700)
        properties = read(self.directory / 'properties.json')
        properties['native_session_home'] = str(config)
        write(self.directory / 'properties.json', properties)
        facts = {**read(self.directory / 'runtime.json'), 'state': 'failed', 'exit_code': 2}
        write(self.directory / 'runtime.json', facts)
        self.env.update(HOME=str(self.work), MV2_CLI=sys.executable,
                        MV2_AGENT_ID=self.directory.name, MV2_LAUNCH_ID=self.launch.name)
        request = {'resume_message_id': self.message['id'], 'env': self.env, 'unset_env': []}
        write(self.launch / 'request.json', request)
        transcript = project / (properties['native_session_id'] + '.jsonl')
        prompt = (self.launch / 'task.md').read_text()
        cases = [('saved', False), ('saved', True), ('replaced', False), ('replaced', True),
                 ('legacy', False), ('legacy', True), ('legacy replaced', None), ('live', None),
                 ('wrong request', None), ('missing task', None), ('damaged context', None), ('unfinished', None)]
        for case, consumed in cases:
            with self.subTest(case=case, consumed=consumed):
                write(self.directory / 'properties.json', properties)
                write(self.directory / 'runtime.json', facts)
                write(self.launch / 'request.json', request)
                (self.launch / 'task.md').write_text(prompt)
                self.reset()
                mailbox.update_message(self.directory, self.message['id'], target_session=properties['native_session_id'])
                transcript.write_text(json.dumps({'type': 'user', 'timestamp': facts['started_at'],
                    'message': {'content': prompt}}) + '\n' if consumed else '')
                project.chmod(0)
                runner.finish_failed_wake(self.launch, request, self.env,
                                         Mock(poll=Mock(return_value=2)), 'unknown option')
                self.assertEqual(mailbox.message_record(self.directory, self.message['id'])[1]['submission_state'], 'unknown')
                lines = []
                self.assertFalse(deliver.run(self.root, agent='wake', check=True, _present=lines.append)['ok'])
                self.assertIn('--check', lines[-1])
                self.assertIn(str(project), lines[-1])
                self.assertIn('restore read access', lines[-1])
                project.chmod(0o700)
                if case in ('legacy', 'legacy replaced', 'live', 'wrong request', 'missing task'):
                    mailbox.update_message(self.directory, self.message['id'], wake_prompt_context=None)
                    # Old launch requests did not retain ambient HOME or CLAUDE_CONFIG_DIR.
                    write(self.launch / 'request.json', {**request, 'env': {
                        key: value for key, value in self.env.items() if key not in ('HOME', 'CLAUDE_CONFIG_DIR')}})
                if 'replaced' in case:
                    write(self.directory / 'runtime.json', {**facts, 'launch_id': uid(), 'started_at': '2099-01-01T00:00:00+00:00'})
                    write(self.directory / 'properties.json', {**properties, 'native_session_id': uid(),
                        'native_session_home': str(self.work / 'another-home')})
                    (self.launch / 'task.md').write_text('a different prompt')
                elif case == 'live':
                    write(self.directory / 'runtime.json', {**facts, 'state': 'running', 'exit_code': None,
                        'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}})
                elif case == 'wrong request':
                    write(self.launch / 'request.json', {**request, 'resume_message_id': uid()})
                elif case == 'missing task':
                    (self.launch / 'task.md').unlink()
                elif case == 'damaged context':
                    mailbox.update_message(self.directory, self.message['id'], wake_prompt_context={'session': {}})
                elif case == 'unfinished':
                    transcript.write_text('{"type":')
                before = [(self.directory / name).read_bytes() for name in ('properties.json', 'runtime.json')]
                with patch('multiverse.runtime.native.send_or_wake', side_effect=AssertionError('check must never send')), \
                        patch('multiverse.runtime.native.session_snapshot', side_effect=AssertionError('check must not rebind')) as reconcile, \
                        patch.dict(os.environ, {'HOME': str(self.work / 'foreign'), 'CLAUDE_CONFIG_DIR': str(self.work / 'foreign')}):
                    result = deliver.run(self.root, agent='wake', check=True, _present=lines.append)
                reconcile.assert_not_called()
                self.assertEqual(before, [(self.directory / name).read_bytes() for name in ('properties.json', 'runtime.json')])
                tracking = mailbox.message_record(self.directory, self.message['id'])[1]
                self.assertEqual(tracking['submission_state'], 'unknown' if consumed is None else 'accepted' if consumed else 'failed')
                self.assertEqual(result['ok'], consumed is True)
                self.assertIs(tracking['native_receipt']['transcript_confirmed'], consumed)
                with patch('multiverse.runtime.native.send_or_wake', return_value={'state': 'pending', 'ok': True}) as resend:
                    deliver.run(self.root, agent='wake', retry=True)
                self.assertEqual(resend.call_count, 1 if consumed is False else 0)
                if consumed is True:
                    self.assertIn('confirmed', lines[-1])
                elif consumed is False:
                    self.assertIn('unknown option', lines[-1])
                    self.assertIn('--retry', lines[-1])
