"""Batch isolation, redacted failure reasons, and delivery advice that follows the agent's state."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
import shutil
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace

from test_multiverse import BaseTest, ROOT
from multiverse import cli, harnesses, store
from multiverse.universe import mailbox, relocate, templates
from multiverse.runtime import lifecycle, native, storage_adoption
from multiverse.harnesses.types import NativeResult
from multiverse.terminal import tmux
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

    def test_send_reminders_peek_once_and_match_the_recipient_not_its_name(self):
        """A reminder must survive renames and moves without consuming unread messages or confusing names and native-queued senders."""
        directory = self.directories['third']
        properties = store.read(directory / 'properties.json')
        store.write(directory / 'properties.json', {**properties, 'universe_id': store.read(self.root / 'universe.json')['id']})
        environment = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_DIR': str(directory),
                       'MV2_AGENT_ID': directory.name, 'MV2_LAUNCH_ID': store.read(directory / 'runtime.json')['launch_id']}
        first = {'agent_id': self.directories['first'].name, 'universe': str(self.root), 'name': 'old name'}
        second = {'agent_id': self.directories['second'].name, 'universe': str(self.root), 'name': 'second'}
        mailbox.append_message(directory, 'already read', first)
        mailbox.inbox(directory)
        mailbox.append_message(directory, 'held and read', second, intended='steer', focus='ended')
        mailbox.held(directory, 'ended')
        mailbox.append_message(directory, 'already in the native queue', second, delivery='follow-up')
        mailbox.append_message(directory, 'another universe', {**second, 'agent_id': str(uuid4()),
                                                             'universe': str(self.work / 'elsewhere')})
        for _ in range(51):
            mailbox.append_message(directory, 'same name is not the recipient', {'name': 'first'})
        mailbox.append_message(directory, 'answer me', first)
        with (directory / 'inbox.jsonl').open('ab') as stream:
            stream.write(b'{"id":')  # An unfinished append does not hide the complete rows before it.
        def snapshot():
            return {p.name: p.read_bytes() for p in directory.glob('inbox*') if p.is_file()}
        before = snapshot()
        with patch.dict(os.environ, environment):
            for delivery in ('steer', 'follow-up', 'inbox-only'):
                with self.subTest(delivery=delivery), patch.object(mailbox, 'inbox', wraps=mailbox.inbox) as peek:
                    output = self.output('send', f"first,{self.directories['first'].name},second",
                                         '--delivery', delivery, '--text', 'reply')
                    self.assertEqual(len(output.splitlines()), 2)
                    self.assertTrue(output.splitlines()[0].endswith('Reminder: you have unread messages from first.'))
                    self.assertNotIn('Reminder:', output.splitlines()[1])
                    peek.assert_called_once_with(directory, limit=None, peek=True, blocking=False)
                    self.assertEqual(snapshot(), before)
            self.behavior = NativeResult('error', 'fixture refusal')
            self.output('send', 'first', '--text', 'failed reply', code=1)
            self.assertTrue(self.stderr.strip().endswith('Reminder: you have unread messages from first.'))
            self.assertEqual(snapshot(), before)
            with self.subTest('a launch notice and the reminder stay separate'), \
                    patch.object(native, 'send_or_wake', return_value={'state': 'native_queued',
                                 'notices': ['Note: this launch starts it fresh under the same session ID']}):
                output = self.output('send', 'first', '--text', 'with a launch notice')
                self.assertTrue(output.endswith('same session ID\nReminder: you have unread messages from first.\n'), output)
            with patch.object(mailbox, 'inbox', side_effect=AssertionError('JSON and plugin sends do not peek')) as peek:
                result = json.loads(self.output('send', 'first', '--delivery', 'inbox-only', '--text', 'json', '--json'))
                self.assertEqual(result['state'], 'queued')
                lines = []
                send.run(self.root, agent='first', text='plugin', sender={'plugin': 'fixture'},
                         delivery='inbox-only', _present=lines.append)
                self.assertNotIn('Reminder:', lines[0])
                peek.assert_not_called()
            destination = templates.create(self.root / 'subverses/moved', 'moved', workdir=self.work)
            first_properties = store.read(self.directories['first'] / 'properties.json')
            with patch.object(relocate, 'observe', return_value={'live': 'dormant', 'reason': ''}):
                relocate.move_agent(self.root, 'first', destination)
            with self.subTest('the same recipient after a move'), patch.object(self, 'root', destination):
                output = self.output('send', 'first', '--delivery', 'inbox-only', '--text', 'after a move')
                self.assertTrue(output.strip().endswith('Reminder: you have unread messages from first.'), output)
            replacement = self.root / 'state/agents' / str(uuid4())
            store.write(replacement / 'properties.json', {**first_properties, 'id': replacement.name,
                        'native_session_id': str(uuid4())})
            with self.subTest('a different agent reusing the old name'):
                output = self.output('send', 'first', '--delivery', 'inbox-only', '--text', 'new agent')
                self.assertNotIn('Reminder:', output)
            self.assertEqual(snapshot(), before)
        with patch.object(mailbox, 'inbox', side_effect=AssertionError('A person does not peek')) as peek:
            self.assertNotIn('Reminder:', self.output('send', 'first', '--delivery', 'inbox-only', '--text', 'person'))
            peek.assert_not_called()

    def test_send_skips_a_busy_or_damaged_sender_inbox(self):
        """Optional reminder reads must not turn inbox lock contention or a damaged file into a blocked or failed send."""
        directory = self.directories['third']
        properties = store.read(directory / 'properties.json')
        store.write(directory / 'properties.json', {**properties, 'universe_id': store.read(self.root / 'universe.json')['id']})
        environment = {**os.environ, 'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_DIR': str(directory),
                       'MV2_AGENT_ID': directory.name, 'MV2_LAUNCH_ID': store.read(directory / 'runtime.json')['launch_id']}
        mailbox.append_message(directory, 'unread', {'agent_id': self.directories['first'].name,
                              'universe': str(self.root), 'name': 'first'})
        def send_child(reminder=False):
            child = subprocess.run([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root), 'send',
                                    'first', '--delivery', 'inbox-only', '--text', 'still sent'],
                                   env=environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn("Saved to first's inbox", child.stdout)
            self.assertEqual('Reminder:' in child.stdout, reminder)
        for name in ('.inbox.lock', '.launch.lock'):
            with self.subTest(lock=name), store.lock(directory / name):
                send_child(reminder=name == '.launch.lock')
        for name, damaged in (('inbox.cursor', b'bad'), ('inbox-state.json', b'[]'),
                              ('inbox.jsonl', b'[]\n'), ('inbox.jsonl', None), ('.inbox.lock', None)):
            with self.subTest(file=name, damage=damaged):
                path = directory / name
                original = path.read_bytes() if path.exists() else None
                path.unlink(missing_ok=True)
                try:
                    if damaged is None:
                        os.mkfifo(path)
                    else:
                        path.write_bytes(damaged)
                    send_child()
                finally:
                    path.unlink(missing_ok=True)
                    if original is not None:
                        path.write_bytes(original)
        send_child(reminder=True)
        acquire = mailbox.lock
        removed = False
        @contextmanager
        def remove_before_the_peek_lock(path, **options):
            nonlocal removed
            if path == directory / '.inbox.lock' and not removed:
                removed = True
                shutil.rmtree(directory)
            with acquire(path, **options):
                yield
        with patch.dict(os.environ, environment), \
                patch.object(mailbox, 'lock', side_effect=remove_before_the_peek_lock):
            output = self.output('send', 'first', '--delivery', 'inbox-only', '--text', 'after removal')
        self.assertIn("Saved to first's inbox", output)
        self.assertNotIn('Reminder:', output)
        self.assertTrue(removed)
        self.assertFalse(directory.exists(), 'Optional advice must not recreate a removed sender')

    def test_delivery_check_summarizes_confirmations_without_hiding_other_evidence(self):
        """Large checks must keep uncertain and failed messages visible without burying them in confirmed rows;
        presentation must never discard the complete JSON evidence or submit messages again."""
        answers = {}
        for text, answer in [('confirmed ' + str(index), NativeResult('ok', value={'transcript_confirmed': True}))
                             for index in range(3)] + [
                ('accepted', NativeResult('native_queued')), ('failed', NativeResult('error', 'definite refusal')),
                ('uncertain', NativeResult('unknown', 'acceptance unknown'))]:
            self.behavior = (NativeResult('error', 'definite refusal') if text == 'failed'
                             else TimeoutError('acceptance unknown') if text == 'uncertain' else NativeResult('native_queued'))
            self.output('send', 'first', '--text', text, code=1 if text in ('failed', 'uncertain') else 0)
            answers[self.history('first')[-1]['id']] = answer
        directory = self.directories['first']
        ledger = store.read(directory / 'inbox-state.json')
        with patch.object(harnesses.load(self.root, 'claude'), 'get_delivery',
                          side_effect=lambda session, message_id, executor: answers[message_id]):
            output = self.output('deliver', 'first', '--check', code=1)
            self.assertEqual(output.splitlines()[0],
                             'first: 3 confirmed, 1 accepted without confirmation, 1 not delivered, 1 uncertain.')
            self.assertNotIn('confirmed in its conversation', output)
            self.assertIn('accepted; no confirmation yet', output)
            self.assertIn('cannot yet tell', self.stderr)
            self.assertIn('definite refusal', self.stderr)
            store.write(directory / 'inbox-state.json', ledger)
            complete = json.loads(self.output('deliver', 'first', '--check', '--json', code=1))
            self.assertEqual(len(complete['messages']), 6)
            self.assertEqual(sum(message.get('transcript_confirmed') is True for message in complete['messages']), 3)
            self.assertFalse(complete['ok'])
        self.assertEqual(len(self.calls), 6)

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
                    store.read(directory / 'runtime.json'), directory, environment, [('recorded terminal server', backend, {})])
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
                         'mv2 resume ' + directory.name + ' --start-fresh',
                         'to keep this agent with a new conversation'):
                self.assertIn(text, self.stderr)
            self.assertNotIn(secret, self.stderr)
            self.assertEqual(self.history('third')[-1]['tracking']['submission_state'], 'failed')
            with patch.object(native, 'finalize_launch_only', return_value=None):
                self.output('deliver', 'third', '--check', code=1)
            self.assertIn(reason, self.stderr)

        with patch.object(tmux, '_launch_version', return_value='tmux 3.1c'):
            reason = str(tmux._launch_problem())
        for diagnostic in (reason, reason.removesuffix('.')):
            with self.subTest('tmux wake refusal has one sentence separator', diagnostic=diagnostic):
                def refuse(root, reference, message_id, **kwargs):
                    return native._wake_failure(self.directories['third'], message_id, ValueError(diagnostic))
                with patch.object(native, 'send_or_wake', side_effect=refuse):
                    self.output('send', 'third', '--text', 'wake message', code=1)
                    self.assertEqual(self.stderr.strip(),
                        f'Not delivered to third: Could not wake agent: {reason} Saved; fix the reason, '
                        'then mv2 deliver third --retry or --dismiss.')
                    self.assertEqual(self.history('third')[-1]['tracking']['attempts'][-1]['reason'],
                                     'Could not wake agent: ' + diagnostic)
                    record = json.loads(self.output('send', 'third', '--text', 'wake message', '--json', code=1))
                    self.assertEqual(record['native_delivery']['reason'], 'Could not wake agent: ' + diagnostic)

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
