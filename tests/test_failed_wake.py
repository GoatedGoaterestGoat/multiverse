"""A startup exit is retryable only after exact harness negative evidence."""
import json
import os
import subprocess
import sys
from unittest.mock import Mock, patch
from multiverse.harnesses.claude import ClaudeAdapter, project_key
from multiverse.runtime import lifecycle, native, runner
from multiverse.store import read, uid, write
from multiverse.tools import deliver
from multiverse.universe import agents, mailbox
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
        # Each table row starts a new attempt, including any once-only final report.
        write(self.directory / 'inbox-state.json', {})
        (self.directory / 'sessions.jsonl').unlink(missing_ok=True)
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
                 ('outside lineage', False), ('outside lineage', True),
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
                try:
                    runner.finish_failed_wake(self.launch, request, self.env,
                                             Mock(poll=Mock(return_value=2)), 'unknown option')
                    self.assertEqual(mailbox.message_record(self.directory, self.message['id'])[1]['submission_state'], 'unknown')
                    lines = []
                    self.assertFalse(deliver.run(self.root, agent='wake', check=True, _present=lines.append)['ok'])
                    self.assertIn('--check', lines[-1])
                    self.assertIn(str(project), lines[-1])
                    self.assertIn('restore read access', lines[-1])
                    with patch.object(native, 'send_or_wake', side_effect=AssertionError('unreadable evidence cannot replay')):
                        deliver.run(self.root, agent='wake', retry=True)
                finally:
                    project.chmod(0o700)
                if case in ('legacy', 'legacy replaced', 'live', 'wrong request', 'missing task'):
                    mailbox.update_message(self.directory, self.message['id'], wake_prompt_context=None)
                    # Old launch requests did not retain ambient HOME or CLAUDE_CONFIG_DIR.
                    write(self.launch / 'request.json', {**request, 'env': {
                        key: value for key, value in self.env.items() if key not in ('HOME', 'CLAUDE_CONFIG_DIR')}})
                if 'replaced' in case or case == 'outside lineage':
                    replacement = uid()
                    if case == 'replaced':
                        # A retry may follow a recorded /clear, never an unrelated replacement.
                        agents.rebind_session(self.root, 'wake', properties['native_session_id'], replacement,
                                              'clear', self.launch.name, home=str(config))
                    else:
                        write(self.directory / 'properties.json', {**properties, 'native_session_id': replacement,
                            'native_session_home': str(self.work / 'another-home')})
                    write(self.directory / 'runtime.json', {**facts, 'launch_id': uid(), 'started_at': '2099-01-01T00:00:00+00:00'})
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
                if case == 'outside lineage' and consumed is False:
                    with self.assertRaisesRegex(ValueError, 'outside.*lineage'):
                        lifecycle._wake_prompt(self.root, self.directory,
                                               read(self.directory / 'properties.json'), self.message['id'])
                def retry_prompt(root, reference, message_id, *, retry):
                    self.assertTrue(retry)
                    # Exercise the real wake eligibility/retarget boundary; only the launch is fake.
                    text, selected = lifecycle._wake_prompt(root, self.directory,
                                                           read(self.directory / 'properties.json'), message_id)
                    self.assertIn(self.message['text'], text)
                    self.assertEqual(selected['target_session'], read(self.directory / 'properties.json')['native_session_id'])
                    receipt = {'state': 'ok', 'ok': True, 'value': {'transcript_confirmed': True}}
                    mailbox.update_message(self.directory, message_id, submission_state='accepted',
                                           attempts=[receipt], native_receipt=receipt['value'])
                    return receipt
                with patch.object(native, 'send_or_wake', side_effect=retry_prompt) as resend:
                    retried = deliver.run(self.root, agent='wake', retry=True)
                    repeated = deliver.run(self.root, agent='wake', retry=True)
                self.assertEqual(resend.call_count, 1 if consumed is False and case != 'outside lineage' else 0)
                if case == 'outside lineage' and consumed is False:
                    self.assertTrue(retried['ok'])
                    self.assertEqual(retried['finalized'], 1)
                    self.assertIn('outside', retried['messages'][0]['reason'])
                    self.assertTrue(repeated['ok'])
                    self.assertEqual(repeated['finalized'], 1)
                if consumed is True:
                    self.assertIn('confirmed', lines[-1])
                elif consumed is False:
                    self.assertIn('unknown option', lines[-1])
                    self.assertIn('--retry', lines[-1])

    def test_a_started_wake_is_settled_only_by_its_own_prompt_evidence(self):
        """A started wake is confirmed once its prompt is recorded and called lost only after its launch provably
        ended without it: a live launch can still hold the prompt (behind a resume dialog), so calling it lost
        there invites a duplicate, while never checking it lets a lost wake go unnoticed."""
        config = self.work / 'private-config'
        project = config / 'projects' / project_key(os.path.realpath(self.work))  # as Claude names the launch's folder
        project.mkdir(parents=True)
        self.addCleanup(project.chmod, 0o700)
        properties = {**read(self.directory / 'properties.json'), 'native_session_home': str(config)}
        write(self.directory / 'properties.json', properties)
        write(self.directory / 'runtime.json', {**read(self.directory / 'runtime.json'), 'state': 'running'})
        env = {**self.env, 'HOME': str(self.work)}
        transcript = project / (properties['native_session_id'] + '.jsonl')
        prompt = (self.launch / 'task.md').read_text()
        row = json.dumps({'type': 'user', 'timestamp': '2026-10-17T12:00:01+00:00', 'message': {'content': prompt}}) + '\n'
        finished = subprocess.Popen([sys.executable, '-c', 'pass'])
        finished.wait()
        cleanup = self.launch / 'cleanup.json'
        cases = [('recorded', row, False, False, 'accepted', None),
                 ('recorded after its launch ended', row, True, False, 'accepted', None),
                 ('absent while its launch runs', '', False, False, 'accepted', 'no confirmation yet'),
                 ('absent after its launch ended', '', True, False, 'cancelled', 'lost when the agent stopped'),
                 ('unreadable after its launch ended', '', True, True, 'accepted', str(project)),
                 ('kept no evidence (an older wake)', '', True, False, 'accepted', '')]
        for case, text, ended, unreadable, expected, line in cases:
            with self.subTest(case=case):
                self.reset()
                mailbox.update_message(self.directory, self.message['id'], target_session=properties['native_session_id'])
                snapshot = runner.started_wake_snapshot(self.launch, env, self.message['id'])
                self.assertIsNotNone(snapshot)
                mailbox.finish_wake(self.directory, self.launch.name, self.message['id'], True,
                                    prompt_context=None if case.startswith('kept no') else snapshot)
                transcript.write_text(text)
                if ended:
                    write(cleanup, {'schema': 1, 'launch_id': self.launch.name, 'state': 'complete',
                                    'pid': finished.pid, 'child_pid': finished.pid})
                else:
                    cleanup.unlink(missing_ok=True)
                if unreadable:
                    project.chmod(0)
                first, second = [], []
                try:
                    with patch.object(native, 'send_or_wake', side_effect=AssertionError('a wake is never resent')), \
                            patch.dict(os.environ, {'HOME': str(self.work / 'foreign'), 'CLAUDE_CONFIG_DIR': str(self.work / 'foreign')}):
                        checked = deliver.run(self.root, agent='wake', check=True, _present=first.append)
                        deliver.run(self.root, agent='wake', check=True, _present=second.append)
                        deliver.run(self.root, agent='wake', retry=True)
                finally:
                    project.chmod(0o700)
                tracking = mailbox.message_record(self.directory, self.message['id'])[1]
                self.assertEqual(tracking['submission_state'], expected)
                settled = ['wake: no messages to check.']
                if line is None:  # confirmed once, then settled
                    self.assertTrue(checked['ok'])
                    self.assertIs(tracking['native_receipt']['transcript_confirmed'], True)
                    self.assertEqual((first, second), (['wake: 1 confirmed.'], settled))
                elif line == '':  # an older wake stays out of --check
                    self.assertEqual((first, second), (settled, settled))
                    self.assertIsNone(tracking['native_receipt']['transcript_confirmed'])
                else:
                    detail = [text for text in first if line in text]
                    self.assertEqual(len(detail), 1, first)
                    self.assertIn('Do not resend' if expected == 'accepted' else 'Send a new message', detail[0])
                    # A loss is reported once; an unsettled wake is asked again at every check.
                    if expected == 'cancelled':
                        self.assertEqual(second, settled)
                    else:
                        self.assertEqual([text for text in second if line in text], detail)
        with self.subTest(case='stored and ended just after the first read'):
            # The launch stores the prompt and exits between a negative read and the end proof:
            # the proof taken before the read still saw it running, so that read can't mean lost.
            self.reset()
            mailbox.update_message(self.directory, self.message['id'], target_session=properties['native_session_id'])
            snapshot = runner.started_wake_snapshot(self.launch, env, self.message['id'])
            mailbox.finish_wake(self.directory, self.launch.name, self.message['id'], True, prompt_context=snapshot)
            transcript.write_text('')
            cleanup.unlink(missing_ok=True)
            real, raced = ClaudeAdapter.startup_prompt_evidence, []

            def racing(adapter, *args):
                answer = real(adapter, *args)
                if not raced:
                    raced.append(answer)
                    transcript.write_text(row)
                    write(cleanup, {'schema': 1, 'launch_id': self.launch.name, 'state': 'complete',
                                    'pid': finished.pid, 'child_pid': finished.pid})
                return answer
            first, second = [], []
            with patch.object(ClaudeAdapter, 'startup_prompt_evidence', racing), \
                    patch.object(native, 'send_or_wake', side_effect=AssertionError('a wake is never resent')):
                deliver.run(self.root, agent='wake', check=True, _present=first.append)
                deliver.run(self.root, agent='wake', check=True, _present=second.append)
            self.assertEqual(raced, [False])
            self.assertTrue(any('no confirmation yet' in text for text in first), first)
            self.assertEqual(second, ['wake: 1 confirmed.'])
            tracking = mailbox.message_record(self.directory, self.message['id'])[1]
            self.assertEqual(tracking['submission_state'], 'accepted')
            self.assertIs(tracking['native_receipt']['transcript_confirmed'], True)
        with patch('multiverse.harnesses.load', return_value=object()):
            self.assertIsNone(runner.started_wake_snapshot(self.launch, env, self.message['id']))
