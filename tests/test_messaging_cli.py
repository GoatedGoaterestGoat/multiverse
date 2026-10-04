"""Readable delivery evidence, batch isolation and unchanged machine results."""
from contextlib import redirect_stdout
import io
import json
import os
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest, ROOT
from multiverse import cli, harnesses, native, store
from multiverse.harnesses.types import NativeResult
from multiverse.tools import deliver, send
from multiverse.tools._messaging import delivery_line


class MessagingCliTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directories = {}
        for name in ('first', 'second', 'third'):
            directory = self.root / 'state/agents' / str(uuid4())
            self.directories[name] = directory
            store.write(directory / 'properties.json', {'id': directory.name, 'name': name,
                'harness': 'claude', 'workspace': str(self.work), 'native_session_id': str(uuid4())})
            store.write(directory / 'runtime.json', {'launch_id': 'launch', 'state': 'running'})
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.behavior = NativeResult('native_queued', 'Native safe-boundary steering accepted; consumption unconfirmed',
                                     {'turn_id': 'secret-turn', 'client_user_message_id': 'secret-message'})
        self.calls = []
        owner = self
        class Adapter:
            def send(self, session, message, executor):
                owner.calls.append((session.agent_id, message.text, message.delivery))
                result = owner.behavior(session) if callable(owner.behavior) else owner.behavior
                if isinstance(result, Exception):
                    raise result
                return result
            def get_delivery(self, session, message_id, executor):
                return owner.behavior
        self.enterContext(patch.object(harnesses, 'load', return_value=Adapter()))

    def output(self, action, *arguments, code=0):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(sys, 'argv', ['mv2', '-u', str(self.root), action, *arguments]):
            try:
                cli.main()
            except SystemExit as error:
                self.assertEqual(error.code, code)
            else:
                self.assertEqual(code, 0, output.getvalue())
        return output.getvalue()

    def history(self, name):
        return store.inbox(self.directories[name], all_messages=True, limit=None)['messages']

    def test_send_does_not_echo_text_or_internal_identifiers_and_json_is_original(self):
        secret = '多行 PRIVATE BODY\nquotes $(false) `false`'
        output = self.output('send', self.directories['first'].name, '--text', secret)
        self.assertEqual(output, 'Sent to first; it reads this at its next pause.\n')
        for value in (secret, self.directories['first'].name, 'native_queued', 'secret-turn', 'secret-message'):
            self.assertNotIn(value, output)
        record = json.loads(self.output('send', 'second', '--text', secret, '--json'))
        self.assertEqual(record['text'], secret)
        self.assertEqual(record['state'], 'native_queued')
        self.assertEqual(set(record), {'at', 'from', 'text', 'delivery', 'state', 'saved', 'native_delivery', 'ok'})
        self.assertEqual(record['native_delivery']['reason'], self.behavior.reason)
        self.assertNotIn('agent', record)

    def test_batch_reads_input_once_preserves_order_and_deduplicates_aliases(self):
        body = 'entire body\nsecond line\n'
        stream = io.StringIO(body)
        with patch.object(send.sys, 'stdin', stream), patch.object(stream, 'read', wraps=stream.read) as read_input:
            output = self.output('send', f"first, {self.directories['first'].name}, second,third", '--queue-only')
            self.assertEqual(read_input.call_count, 1)
        self.assertEqual(output, ''.join(f"Saved to {name}'s inbox; it sees it at its next check.\n"
                                         for name in ('first', 'second', 'third')))
        for name in self.directories:
            self.assertEqual([record['text'] for record in self.history(name)], [body])
        self.assertEqual(self.calls, [])

    def test_batch_continues_after_definite_failure_and_retains_each_message(self):
        def behavior(session):
            return NativeResult('error', 'connection refused') if session.agent_id == self.directories['second'].name else self.accepted()
        self.behavior = behavior
        output = self.output('send', 'first,second,third', '--text', 'secret', code=1)
        self.assertEqual(len(output.splitlines()), 3)
        self.assertIn('Not delivered to second: connection refused. Saved;', output)
        self.assertIn('mv2 deliver second --retry', output)
        self.assertTrue(output.endswith('Sent to third; it reads this at its next pause.\n'))
        self.assertEqual(len(self.calls), 3)
        for name in self.directories:
            self.assertEqual(len(self.history(name)), 1)
        self.assertEqual(self.history('second')[0]['tracking']['submission_state'], 'failed')

    @staticmethod
    def accepted():
        return NativeResult('native_queued')

    def test_batch_json_reports_unknown_recipient_without_blocking_valid_ones(self):
        result = json.loads(self.output('send', 'first,missing,third', '--text', 'body', '--json', code=1))
        self.assertFalse(result['ok'])
        self.assertEqual([entry['agent'] for entry in result['recipients']], ['first', 'missing', 'third'])
        self.assertFalse(result['recipients'][1]['saved'])
        self.assertEqual(len(self.calls), 2)
        for target in ('first,', ',first', 'first,,second'):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'comma'):
                send.run(self.root, agent=target, text='not saved')
        self.assertEqual(len(self.history('first')), 1)

    def test_uncertain_submission_explains_check_and_never_retries_it(self):
        self.behavior = TimeoutError('connection lost')
        output = self.output('send', 'first', '--text', 'private', code=1)
        self.assertIn('cannot yet tell whether it received', output)
        self.assertIn('mv2 deliver first --check before sending again', output)
        self.assertNotIn('acceptance unknown', output)
        self.behavior = self.accepted()
        self.assertEqual(self.output('deliver', 'first', '--retry'), 'first: no messages to retry.\n')
        self.assertEqual(len(self.calls), 1)
        self.behavior = NativeResult('unknown', 'No receipt found')
        checked = self.output('deliver', 'first', '--check', code=1)
        self.assertIn('cannot yet tell', checked)
        self.assertEqual(len(self.calls), 1)

    def test_retry_and_check_show_evidence_without_message_text(self):
        self.behavior = NativeResult('error', 'rejected before submission')
        self.output('send', 'first', '--text', 'private message', code=1)
        self.behavior = NativeResult('native_queued', 'Accepted by native after-turn queue')
        retried = self.output('deliver', 'first', '--retry')
        self.assertEqual(retried, 'Sent to first; it reads this at its next pause.\n')
        self.assertNotIn('private message', retried)
        self.behavior = NativeResult('native_queued', 'Message remains in native queue', {'transcript_confirmed': False})
        self.assertEqual(self.output('deliver', 'first', '--check'), 'first: message is queued for its next turn. Do not resend.\n')
        self.behavior = self.accepted()
        send.run(self.root, agent='first', text='another private message')
        self.behavior = NativeResult('native_queued', 'Message observed in native thread history', {'transcript_confirmed': True})
        self.assertEqual(self.output('deliver', 'first', '--check'), 'first: message confirmed in its conversation.\n')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.output('deliver', 'first', '--check'), 'first: no messages to check.\n')

    def test_accepted_without_receipt_is_not_a_failure_and_json_preserves_reason(self):
        self.behavior = self.accepted()
        send.run(self.root, agent='first', text='private')
        self.behavior = NativeResult('unknown', 'No delivery receipt from the multiverse mod yet')
        output = self.output('deliver', 'first', '--check')
        self.assertEqual(output, 'first: message accepted; no confirmation yet. Do not resend.\n')
        result = json.loads(self.output('deliver', 'first', '--check', '--json'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['messages'][0]['text'], 'private')
        self.assertEqual(result['messages'][0]['state'], 'unknown')
        self.assertEqual(result['messages'][0]['reason'], self.behavior.reason)
        self.assertEqual(len(self.calls), 1)

    def test_wake_starting_idle_and_after_turn_have_distinct_summaries(self):
        cases = [({'state': 'pending', 'reason': 'Resuming with this message as the interactive starting prompt'},
                  'first was stopped; resuming it with your message.'),
                 ({'state': 'pending', 'reason': 'Saved for native delivery once this launch is ready'},
                  'first is starting; your message will be delivered when ready.'),
                 ({'state': 'native_queued', 'reason': 'Accepted and started from the native idle queue',
                    'value': {'turn_id': 'internal', 'queued_submission_id': 'internal'}},
                  'first started a turn with your message.'),
                 ({'state': 'native_queued', 'reason': 'Accepted by native after-turn queue',
                    'value': {'queued_submission_id': 'internal'}},
                  'first has your message queued for its next turn.')]
        for receipt, expected in cases:
            with self.subTest(receipt=receipt), patch.object(native, 'send_or_wake', return_value=receipt):
                self.assertEqual(self.output('send', 'first', '--text', 'private'), expected + '\n')
        self.assertIn('has not started', delivery_line('first', {'state': 'native_queued', 'value': {'idle_start_error': 'secret'}}))
        self.assertEqual(self.calls, [])

    def test_failure_diagnostic_hides_ids_and_body_but_keeps_reason_and_action(self):
        session_id = str(uuid4())
        self.behavior = NativeResult('error', f'No conversation found with session ID {session_id}')
        output = self.output('send', 'first', '--text', 'private', code=1)
        self.assertIn('No conversation found', output)
        self.assertNotIn(session_id, output)
        self.assertIn('mv2 deliver first --retry', output)
        self.behavior = NativeResult('error', 'Rejected private')
        self.assertNotIn('private', self.output('send', 'first', '--text', 'private', code=1))

    def test_standalone_and_compatibility_cli_have_same_readable_and_json_output(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith('MV2_')}
        env['PYTHONPATH'] = str(ROOT / 'src')
        for standalone in (True, False):
            entry = ['-m', 'multiverse.tools.send'] if standalone else [str(ROOT / 'bin/mv2')]
            command = [sys.executable, *entry, '-u', str(self.root), *([] if standalone else ['send']),
                       'first', '--queue-only', '--text', 'private']
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "Saved to first's inbox; it sees it at its next check.\n")
            result = subprocess.run([*command, '--json'], env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(json.loads(result.stdout)['text'], 'private')
            entry = ['-m', 'multiverse.tools.deliver'] if standalone else [str(ROOT / 'bin/mv2')]
            result = subprocess.run([sys.executable, *entry, '-u', str(self.root),
                *([] if standalone else ['deliver']), 'first', '--check'], env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'first: no messages to check.\n')
