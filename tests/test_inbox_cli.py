"""Readable inbox output, non-consuming history, and output-before-read commits."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest, ROOT
from multiverse import cli, store, universe
from multiverse.tools import inbox as inbox_tool


class InboxCliTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        store.write(self.directory / 'properties.json', {'id': self.agent, 'name': 'reader'})
        store.write(self.directory / 'runtime.json', {'launch_id': 'current'})
        self.enterContext(patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current'}))

    def message(self, text, sender=None, **kwargs):
        return store.append_message(self.directory, text, sender or {'name': 'a person'}, **kwargs)

    def output(self, *flags):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(sys, 'argv',
                ['mv2', '-u', str(self.root), 'inbox', '--agent', self.agent, *flags]):
            cli.main()
        return output.getvalue()

    def test_sender_blocks_resolve_native_agent_name_and_cross_universe_and_user(self):
        sender = str(uuid4())
        store.write(self.root / 'state/agents' / sender / 'properties.json', {'id': sender, 'name': 'renamed-worker'})
        other = universe.create(self.work / 'other', 'Other universe', workdir=self.work)
        foreign = str(uuid4())
        store.write(other / 'state/agents' / foreign / 'properties.json', {'id': foreign, 'name': 'reviewer'})
        long_text = '第一行\n' + 'full text ' * 1500 + '\nfinal line'
        self.message(long_text, {'agent_id': sender, 'name': 'agent', 'universe': str(self.root)})
        self.message('foreign message', {'agent_id': foreign, 'universe': str(other)})
        personal = self.message('person message', {'name': 'Person Name'})
        self.assertEqual(self.output(), f'Message from renamed-worker:\n{long_text}\n\n'
                         'Message from reviewer (in universe Other universe):\nforeign message\n\n'
                         'Message from user:\nperson message\n')
        self.assertNotIn(personal['id'], self.output('--all'))
        self.assertEqual(self.output(), 'No unread messages.\n')

    def test_json_keeps_existing_shape_and_peek_does_not_consume(self):
        message = self.message('first')
        expected = {key: message[key] for key in ('at', 'from', 'text', 'delivery')}
        peek = json.loads(self.output('--peek', '--json'))
        self.assertEqual(peek['messages'], [expected])
        self.assertEqual(set(peek), {'messages', 'cursor'})
        self.assertFalse((self.directory / 'inbox.cursor').exists())
        self.assertEqual(self.output('--peek'), 'Message from user:\nfirst\n')
        self.assertEqual(json.loads(self.output('--json')), peek)
        self.assertEqual(store.read(self.directory / 'inbox.cursor'), peek['cursor'])

    def test_unread_pages_stay_at_fifty_but_readable_history_and_last_are_not_capped(self):
        for index in range(105):
            self.message(f'message {index:03d}')
        self.message('native history', delivery='follow-up')
        first = self.output()
        self.assertEqual(first.count('Message from user:'), 50)
        self.assertIn('message 049', first)
        self.assertNotIn('message 050', first)
        position = (self.directory / 'inbox.cursor').read_bytes()
        all_text = self.output('--all')
        self.assertEqual(all_text.count('Message from user:'), 106)
        self.assertIn('native history', all_text)
        last = self.output('--all', '--last', '100')
        self.assertEqual(last.count('Message from user:'), 100)
        self.assertTrue(last.startswith('Message from user:\nmessage 006\n'))
        self.assertTrue(last.endswith('native history\n'))
        self.assertEqual(len(json.loads(self.output('--all', '--last', '100', '--json'))['messages']), 100)
        page = json.loads(self.output('--all', '--json', '--after', '0'))
        self.assertEqual(len(page['messages']), 50)
        self.assertTrue(page['has_more'])
        self.assertIn('id', page['messages'][0])
        self.assertEqual((self.directory / 'inbox.cursor').read_bytes(), position)

    def dated_messages(self):
        records = [self.message('old'), self.message('middle'), self.message('new')]
        timestamps = ['2026-10-03T10:00:00+00:00', '2026-10-03T11:00:00+00:00', '2026-10-03T12:00:00+00:00']
        for record, timestamp in zip(records, timestamps):
            record['at'] = timestamp
        (self.directory / 'inbox.jsonl').write_text(''.join(json.dumps(record) + '\n' for record in records))

    def test_since_filters_time_before_last_and_normalizes_iso_timezones(self):
        self.dated_messages()
        expected = 'Message from user:\nmiddle\n\nMessage from user:\nnew\n'
        for timestamp in ('2026-10-03T11:00:00Z', '2026-10-03T13:00:00+02:00', '2026-10-03T11:00:00'):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.output('--all', '--since', timestamp), expected)
        self.assertEqual(self.output('--all', '--since', '2026-10-03T11:00:00Z', '--last', '1'),
                         'Message from user:\nnew\n')
        self.assertEqual(self.output('--all', '--since', '2026-10-04T00:00:00Z'), 'No unread messages.\n')
        self.assertFalse((self.directory / 'inbox.cursor').exists())

    def test_relative_since_and_json_paging_over_an_empty_filtered_page(self):
        self.dated_messages()
        now = datetime(2026, 10, 3, 12, 30, tzinfo=timezone.utc)
        with patch.object(inbox_tool, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            self.assertEqual(self.output('--all', '--since', '30m'), 'Message from user:\nnew\n')
            self.assertIn('middle', self.output('--all', '--since', '2h'))
            self.assertNotIn('old', self.output('--all', '--since', '2h'))
            for duration, seconds in (('1s', 1), ('1d', 86400), ('1w', 604800), ('0.5h', 1800)):
                self.assertEqual(inbox_tool.since_time(duration), now - timedelta(seconds=seconds))
        empty = json.loads(self.output('--all', '--json', '--after', '0', '--since', '2026-10-04T00:00:00Z'))
        self.assertEqual(empty['messages'], [])
        self.assertFalse(empty['has_more'])
        self.assertFalse((self.directory / 'inbox.cursor').exists())

    def test_invalid_combinations_never_consume(self):
        self.message('remain unread')
        for flags in (('--last', '1'), ('--since', '30m'), ('--all', '--last', '0'),
                      ('--all', '--last', '-1'), ('--all', '--since', 'yesterday'),
                      ('--all', '--since', '-2h'), ('--after', '0'), ('--all', '--after', '0'),
                      ('--all', '--json', '--after', '0', '--last', '1'), ('--peek', '--all')):
            with self.subTest(flags=flags), self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                    patch('sys.stderr', io.StringIO()):
                self.output(*flags)
        self.assertFalse((self.directory / 'inbox.cursor').exists())
        self.assertEqual(len(store.inbox(self.directory, peek=True)['messages']), 1)

    def test_failed_write_or_flush_leaves_existing_cursor_unchanged(self):
        self.message('already read')
        self.output()
        position = (self.directory / 'inbox.cursor').read_bytes()
        self.message('must survive')
        for method in ('write', 'flush'):
            for machine in (False, True):
                stream = io.StringIO()
                with self.subTest(method=method, json=machine), patch.object(stream, method, side_effect=BrokenPipeError()), \
                        redirect_stdout(stream), self.assertRaises(BrokenPipeError):
                    inbox_tool.cli_run(self.root, agent=self.agent, json=machine)
                self.assertEqual((self.directory / 'inbox.cursor').read_bytes(), position)
        self.assertEqual(self.output(), 'Message from user:\nmust survive\n')

    def test_callback_flushes_before_cursor_commit_and_failed_reader_cannot_be_overtaken(self):
        message = self.message('serialize output and consume')
        presenting, release, second_started = threading.Event(), threading.Event(), threading.Event()
        def fail_output(result):
            self.assertEqual(result['messages'], [message])
            self.assertFalse((self.directory / 'inbox.cursor').exists())
            presenting.set()
            self.assertTrue(release.wait(5))
            raise BrokenPipeError()
        def second_read():
            second_started.set()
            return store.inbox(self.directory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(store.inbox, self.directory, present=fail_output)
            try:
                self.assertTrue(presenting.wait(5))
                second = pool.submit(second_read)
                self.assertTrue(second_started.wait(5))
                self.assertFalse(second.done())
            finally:
                release.set()
            with self.assertRaises(BrokenPipeError):
                first.result(timeout=5)
            self.assertEqual(second.result(timeout=5)['messages'], [message])

    def test_cli_and_tool_module_both_use_readable_output_while_python_run_returns_dict(self):
        self.message('entrypoint')
        arguments = ['-u', str(self.root), '--agent', self.agent, '--peek']
        for executable in ([sys.executable, str(ROOT / 'bin/mv2'), *arguments[:2], 'inbox', *arguments[2:]],
                           [sys.executable, '-m', 'multiverse.tools.inbox', *arguments]):
            result = subprocess.run(executable, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'Message from user:\nentrypoint\n')
        self.assertIsInstance(inbox_tool.run(self.root, agent=self.agent, peek=True), dict)

    def test_stale_cli_launch_does_not_output_or_consume(self):
        self.message('for replacement')
        output = io.StringIO()
        with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'old'}), redirect_stdout(output), \
                self.assertRaisesRegex(ValueError, 'Outdated launch'):
            inbox_tool.cli_run(self.root, agent=self.agent)
        self.assertEqual(output.getvalue(), '')
        self.assertFalse((self.directory / 'inbox.cursor').exists())

    def test_wait_ignores_native_delivery_and_reads_only_after_inbox_changes(self):
        scanned = threading.Event()
        original = store.inbox
        def scan(*args, **kwargs):
            scanned.set()
            return original(*args, **kwargs)
        with patch.object(store, 'inbox', side_effect=scan) as calls, ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(inbox_tool.run, self.root, agent=self.agent, wait=True, timeout='3s')
            self.assertTrue(scanned.wait(2))
            self.message('native only', delivery='steer')
            time.sleep(.35)
            self.assertFalse(waiting.done())
            # Unchanged polls use file stats, not reparsing the entire inbox.
            self.assertLessEqual(calls.call_count, 2)
            self.message('wake this read')
            result = waiting.result(timeout=3)
        self.assertEqual([message['text'] for message in result['messages']], ['wake this read'])
        self.assertEqual(store.inbox(self.directory)['messages'], [])

    def test_waiting_subprocess_flushes_and_consumes_when_a_message_arrives(self):
        process = subprocess.Popen([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root),
            'inbox', '--agent', self.agent, '--wait', '--timeout', '3s'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.wait(timeout=5))
        self.addCleanup(lambda: process.terminate() if process.poll() is None else None)
        self.message('arrived for waiting reader')
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)
        self.assertEqual(output, 'Message from user:\narrived for waiting reader\n')
        self.assertEqual(store.inbox(self.directory)['messages'], [])

    def test_wait_timeout_is_plain_or_json_and_never_advances_cursor(self):
        for machine in (False, True):
            output = io.StringIO()
            with self.subTest(json=machine), redirect_stdout(output), self.assertRaises(SystemExit) as ended:
                inbox_tool.cli_run(self.root, agent=self.agent, wait=True, timeout='0', json=machine)
            self.assertEqual(ended.exception.code, 1)
            if machine:
                result = json.loads(output.getvalue())
                self.assertEqual(result['messages'], [])
                self.assertTrue(result['timed_out'])
                self.assertFalse(result['ok'])
            else:
                self.assertEqual(output.getvalue(), 'Timed out waiting for unread messages.\n')
            self.assertFalse((self.directory / 'inbox.cursor').exists())
        for duration, seconds in (('30', 30), ('30s', 30), ('2m', 120), ('0.5h', 1800)):
            self.assertEqual(inbox_tool.timeout_seconds(duration), seconds)

    def test_wait_rejects_history_and_stale_launches_and_failed_output_stays_unread(self):
        for options in ({'all_messages': True}, {'after': 0}, {'last': 2}, {'since': '2h'}, {'timeout': 'bad'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                inbox_tool.run(self.root, agent=self.agent, wait=True, **options)
        with self.assertRaises(ValueError):
            inbox_tool.run(self.root, agent=self.agent, timeout='1s')
        scanned = threading.Event()
        original = store.inbox
        def scan(*args, **kwargs):
            scanned.set()
            return original(*args, **kwargs)
        with patch.object(store, 'inbox', side_effect=scan), ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(inbox_tool.run, self.root, agent=self.agent, wait=True, timeout='3s')
            self.assertTrue(scanned.wait(2))
            store.write(self.directory / 'runtime.json', {'launch_id': 'replacement'})
            with self.assertRaisesRegex(ValueError, 'Outdated launch'):
                waiting.result(timeout=3)
        store.write(self.directory / 'runtime.json', {'launch_id': 'current'})
        self.message('must survive wait output failure')
        stream = io.StringIO()
        with patch.object(stream, 'flush', side_effect=BrokenPipeError()), redirect_stdout(stream), \
                self.assertRaises(BrokenPipeError):
            inbox_tool.cli_run(self.root, agent=self.agent, wait=True, timeout='1s')
        self.assertFalse((self.directory / 'inbox.cursor').exists())
        self.assertIn('must survive', self.output())
