"""Batch isolation, redacted failure reasons, and delivery advice that follows the agent's state."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sys
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace

from test_multiverse import BaseTest
from multiverse import cli, harnesses, store
from multiverse.universe import mailbox
from multiverse.runtime import lifecycle, native, storage_adoption
from multiverse.harnesses.types import NativeResult
from multiverse.tools import list_agents, send
from multiverse.tools._messaging import failure_reason

LOAD = harnesses.load  # the real loader, for an agent whose harness core itself classifies


class MessagingCliTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directories = {}
        for name in ('first', 'second', 'third'):
            directory = self.root / 'state/agents' / str(uuid4())
            self.directories[name] = directory
            store.write(directory / 'properties.json', {'id': directory.name, 'name': name,
                'harness': 'claude', 'workspace': str(self.work), 'native_session_id': str(uuid4())})
            store.write(directory / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'running',
                'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}})
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
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), \
                patch.object(sys, 'argv', ['mv2', '-u', str(self.root), action, *arguments]):
            try:
                cli.main()
            except SystemExit as error:
                self.assertEqual(error.code, code)
            else:
                self.assertEqual(code, 0, output.getvalue())
        self.stderr = errors.getvalue()
        return output.getvalue()

    def history(self, name):
        return mailbox.inbox(self.directories[name], all_messages=True, limit=None)['messages']

    def texts(self, name, text):
        return [record for record in self.history(name) if record['text'] == text]

    def test_a_batch_send_reaches_every_valid_recipient_once(self):
        """A comma batch delivers to every valid recipient once, with aliases deduplicated and stdin read once, even
        when one recipient fails or is unknown; fragile because the loop shares one body and error path, so an
        exception silently skips later recipients and a duplicate alias double-sends."""
        body = 'entire body\nsecond line\n'
        stream = io.StringIO(body)
        with patch.object(send.sys, 'stdin', stream), patch.object(stream, 'read', wraps=stream.read) as read_input:
            output = self.output('send', f"first, {self.directories['first'].name}, second,third", '--queue-only')
            self.assertEqual(read_input.call_count, 1)
        named = [name for line in output.splitlines() for name in self.directories if f' {name}' in line]
        self.assertEqual(named, ['first', 'second', 'third'])
        for name in self.directories:
            self.assertEqual([record['text'] for record in self.history(name)], [body])
        self.assertEqual(self.calls, [])

        def behavior(session):
            if session.agent_id == self.directories['second'].name:
                return NativeResult('error', 'connection refused')
            return NativeResult('native_queued')
        self.behavior = behavior
        output = self.output('send', 'first,second,third', '--text', 'secret', code=1)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertNotIn('second', output)
        self.assertIn('third', output)
        self.assertIn('connection refused', self.stderr)
        self.assertIn('mv2 deliver second --retry', self.stderr)
        self.assertEqual(len(self.calls), 3)
        for name in self.directories:
            self.assertEqual(len(self.texts(name, 'secret')), 1)
        self.assertEqual(self.texts('second', 'secret')[0]['tracking']['submission_state'], 'failed')

        self.behavior = NativeResult('native_queued')
        result = json.loads(self.output('send', 'first,missing,third', '--text', 'body', '--json', code=1))
        self.assertFalse(result['ok'])
        self.assertEqual([entry['agent'] for entry in result['recipients']], ['first', 'missing', 'third'])
        self.assertFalse(result['recipients'][1]['saved'])
        self.assertEqual(len(self.calls), 5)
        output = self.output('send', 'first,missing,third', '--text', 'private', code=1)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn('first', output)
        self.assertIn('third', output)
        self.assertEqual(len(self.stderr.splitlines()), 1)
        self.assertIn('missing', self.stderr)
        for target in ('first,', ',first', 'first,,second'):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'comma'):
                send.run(self.root, agent=target, text='not saved')
        self.assertEqual([len(self.texts('first', text)) for text in ('body', 'private', 'not saved')], [1, 1, 0])

    def test_failure_reasons_hide_message_text_and_ids_without_mangling_words(self):
        """Native failure reasons are shown with the message text and ids removed and every other word intact; fragile
        because redaction substitutes arbitrary user substrings like a or is inside native text, and the garbled
        result still reads as a sentence, while a truncated or redacted wake refusal can hide the exact file and remedy."""
        secret = '多行 PRIVATE BODY\nquotes $(false) `false`'
        output = self.output('send', self.directories['first'].name, '--text', secret)
        for value in (secret, self.directories['first'].name, 'native_queued', 'secret-turn', 'secret-message'):
            self.assertNotIn(value, output)
        record = json.loads(self.output('send', 'second', '--text', secret, '--json'))
        self.assertEqual(record['text'], secret)
        self.assertEqual(record['state'], 'native_queued')
        self.assertEqual(set(record), {'at', 'from', 'text', 'delivery', 'state', 'saved', 'native_delivery', 'ok'})
        self.assertEqual(record['native_delivery']['reason'], self.behavior.reason)
        self.assertNotIn('agent', record)
        session_id = str(uuid4())
        self.behavior = NativeResult('error', f'No conversation found with session ID {session_id}')
        self.assertEqual(self.output('send', 'first', '--text', 'private', code=1), '')
        self.assertIn('No conversation found', self.stderr)
        self.assertNotIn(session_id, self.stderr)
        self.assertIn('mv2 deliver first --retry', self.stderr)
        self.behavior = NativeResult('error', 'Rejected private')
        self.output('send', 'first', '--text', 'private', code=1)
        self.assertNotIn('private', self.stderr)
        reason = 'This agent is launch-only: its harness sets inbox_bridge to false'
        for text in ('hi', 'is', 'a', 'agent', 'needs'):
            with self.subTest(text=text):
                self.assertEqual(failure_reason(reason, text), reason)
        body = 'long private message with several words'
        result = failure_reason('Rejected ' + body + '; more detail', body)
        self.assertEqual(result, 'Rejected the message')
        self.assertNotIn('messthe', result)
        with self.subTest('wake adoption refusal keeps its complete path and remedy'):
            directory = self.directories['third']
            environment = {'HOME': str(self.work), 'CLAUDE_CONFIG_DIR': str(self.work / 'missing-claude-storage')}
            backend = SimpleNamespace(environment=lambda key: environment.get(key))
            try:
                storage_adoption.discover(LOAD(self.root, 'claude'), store.read(directory / 'properties.json'),
                    store.read(directory / 'runtime.json'), directory, {}, environment, backend)
            except ValueError as error:
                reason = str(error)
            else:
                self.fail('The missing legacy conversation should refuse adoption')
            self.assertGreater(len(reason), 220)
            def refuse(root, reference, message_id, **kwargs):
                return native._wake_failure(directory, message_id, ValueError(reason + '; reflected input: ' + secret))
            with patch.object(native, 'send_or_wake', side_effect=refuse):
                self.output('send', 'third', '--text', secret, code=1)
            for text in (str(directory / 'properties.json'), reason,
                         'mv2 remove ' + directory.name, 'then spawn a new agent'):
                self.assertIn(text, self.stderr)
            self.assertNotIn(secret, self.stderr)
            self.assertEqual(self.history('third')[-1]['tracking']['submission_state'], 'failed')
            with patch.object(native, 'finalize_launch_only', return_value=None):
                self.output('deliver', 'third', '--check', code=1)
            self.assertIn(reason, self.stderr)

    def test_advice_follows_the_agents_state(self):
        """Delivery advice follows state: live and broken gets kill then resume, stopped gets resume without kill,
        launch-only gets inbox-only or attach with no restart; fragile because agents read and follow the advice, so
        wrong advice causes a kill-and-resume loop or a duplicate after uncertain acceptance (14bc474, LC-1)."""
        self.behavior = NativeResult('unavailable', 'Claude has not published the inbox key for this launch')
        self.assertEqual(self.output('send', 'first', '--text', 'private', code=1), '')
        self.assertLess(self.stderr.index('mv2 kill first'), self.stderr.index('mv2 resume first'))
        self.assertIn('mv2 deliver first --retry', self.stderr)
        self.assertEqual([record['tracking']['submission_state'] for record in self.history('first')], ['failed'])

        receipt = {'state': 'error', 'reason': 'Could not wake agent: executable unavailable'}
        with patch.object(native, 'send_or_wake', return_value=receipt), \
                patch.object(native, 'observe', return_value={'live': 'stopped'}):
            self.assertEqual(self.output('send', 'second', '--text', 'private', code=1), '')
        self.assertIn('resume it', self.stderr)
        self.assertNotIn('mv2 kill', self.stderr)

        self.behavior = NativeResult('unavailable', 'This agent is launch-only: its harness sets inbox_bridge to false',
                                     {'launch_only': True})
        for delivery in ('steer', 'follow-up'):
            with self.subTest(delivery=delivery):
                self.output('send', 'third', '--delivery', delivery, '--text', 'private', code=1)
                self.assertIn('launch-only', self.stderr)
                self.assertIn('--delivery inbox-only', self.stderr)
                self.assertIn('mv2 attach third', self.stderr)
                for advice in ('mv2 kill', 'resume it', 'still starting'):
                    self.assertNotIn(advice, self.stderr)
        self.assertEqual(len(self.history('third')), 2)
        retried = self.output('deliver', 'third', '--retry')  # a final refusal: nothing to retry, exit 0
        self.assertEqual(self.stderr, '')
        self.assertNotIn('resume it', retried)
        with patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            listing = list_agents.run(self.root, wide=True)
        self.assertIn('mv2 deliver first --retry', listing)  # only where a retry can help
        self.assertNotIn('mv2 deliver third --retry', listing)

        with self.subTest('uncertain acceptance tells the sender to check, never restart or retry'):
            self.behavior = TimeoutError('connection lost after submission')
            self.assertEqual(self.output('send', 'first', '--text', 'uncertain body', code=1), '')
            self.assertIn('mv2 deliver first --check', self.stderr)
            for advice in ('mv2 kill', 'mv2 resume', 'resume it', '--retry'):
                self.assertNotIn(advice, self.stderr)
            self.assertEqual(self.history('first')[-1]['tracking']['submission_state'], 'unknown')

        store.write(self.root / 'harnesses/custom/harness.json',
                    {'schema': 1, 'spawn': [sys.executable, '-c', 'pass'], 'resume': [sys.executable, '-c', 'pass']})
        directory = self.root / 'state/agents' / str(uuid4())
        store.write(directory / 'properties.json', {'id': directory.name, 'name': 'custom', 'harness': 'custom',
                                                    'workspace': str(self.work)})
        store.write(directory / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'stopped'})
        with patch.object(harnesses, 'load', LOAD), patch.object(native, 'observe', return_value={'live': 'stopped'}):
            self.output('send', 'custom', '--text', 'private', code=1)  # a harness that cannot wake with a message
        self.assertIn('--delivery inbox-only', self.stderr)
        self.assertNotIn('resume it', self.stderr)
