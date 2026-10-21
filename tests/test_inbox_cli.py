"""Non-consuming inbox reads, output-before-read commits, and waiting for inbox messages."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
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
from multiverse import cli, store
from multiverse.universe import mailbox
from multiverse.tools import doctor, report
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
        return mailbox.append_message(self.directory, text, sender or {'name': 'a person'}, **kwargs)

    def output(self, *flags):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(sys, 'argv',
                ['mv2', '-u', str(self.root), 'inbox', '--agent', self.agent, *flags]):
            cli.main()
        return output.getvalue()

    def test_reads_that_must_not_consume_never_move_the_cursor(self):
        """--peek, --all, --last, --since, invalid combinations, wait timeouts, report's unread count and doctor never
        move the read cursor, and an unread page consumes only its 50; fragile because every new read option is a
        new way to advance it, and a break silently marks messages read."""
        cursor = self.directory / 'inbox.cursor'
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
            self.assertFalse(cursor.exists())
        for duration, seconds in (('30', 30), ('30s', 30), ('2m', 120), ('0.5h', 1800)):
            self.assertEqual(inbox_tool.timeout_seconds(duration), seconds)
        self.message('native messages are not in the unread page', delivery='steer')
        unread = [self.message(f'message {index:03d}') for index in range(105)]
        self.message('native history', delivery='follow-up')
        invalid = (('--last', '1'), ('--since', '30m'), ('--all', '--last', '0'), ('--all', '--last', '-1'),
                   ('--all', '--since', 'yesterday'), ('--all', '--since', '-2h'), ('--after', '0'),
                   ('--all', '--after', '0'), ('--all', '--json', '--after', '0', '--last', '1'), ('--peek', '--all'),
                   ('--peek', '--after', '0'))
        for start in (0, 50, 100):
            page, remaining = unread[start:start + 50], len(unread) - start
            position = cursor.read_bytes() if cursor.exists() else None
            def unchanged(read):
                self.assertEqual(cursor.read_bytes() if cursor.exists() else None, position, read)
            peek = json.loads(self.output('--peek', '--json'))
            self.assertEqual(set(peek), {'messages', 'cursor'})
            self.assertEqual(peek['messages'], [{key: message[key] for key in ('at', 'from', 'text', 'delivery')}
                                                for message in page])
            unchanged('--peek --json')
            self.assertEqual(self.output('--peek').count('Message from user:'), len(page))
            unchanged('--peek')
            self.assertEqual(self.output('--all').count('Message from user:'), 107)
            unchanged('--all')
            last = json.loads(self.output('--all', '--last', '100', '--json'))['messages']
            self.assertEqual(len(last), 100)
            self.assertEqual([message['text'] for message in last[:1] + last[-1:]], ['message 006', 'native history'])
            unchanged('--all --last')
            history = json.loads(self.output('--all', '--json', '--after', '0'))
            self.assertEqual(len(history['messages']), 50)
            self.assertTrue(history['has_more'])
            self.assertIn('id', history['messages'][0])
            unchanged('--all --json --after')
            self.assertIn('native history', self.output('--all', '--since', '1h'))
            unchanged('--all --since')
            for flags in invalid:
                with self.subTest(flags=flags), self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                        patch('sys.stderr', io.StringIO()):
                    self.output(*flags)
                unchanged(flags)
            for options in ({'all_messages': True}, {'after': 0}):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, '--peek cannot be combined'):
                    mailbox.inbox(self.directory, peek=True, **options)
            for options in ({'all_messages': True}, {'after': 0}, {'last': 2}, {'since': '2h'}, {'timeout': 'bad'}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    inbox_tool.run(self.root, agent=self.agent, wait=True, **options)
            with self.assertRaises(ValueError):
                inbox_tool.run(self.root, agent=self.agent, timeout='1s')
            unchanged('invalid options')
            with patch('subprocess.Popen') as spawned, patch('subprocess.run') as ran:
                line = report.cli_run(self.root, agent=self.agent, phase='working')
            spawned.assert_not_called()  # The count is taken in this process.
            ran.assert_not_called()
            self.assertIn(f'{remaining} unread inbox message', line)
            self.assertNotIn('address', line)
            unchanged('report')
            # Done ends the task, so the line sends the agent to what waits instead of letting it stop.
            line = report.cli_run(self.root, agent=self.agent, phase='done')
            self.assertIn(f'{remaining} unread inbox messages: read them now with mv2 inbox and address them', line)
            unchanged('report done')
            doctor.run(self.root)
            unchanged('doctor')
            self.assertEqual(json.loads(self.output('--json')), peek)
            self.assertEqual(store.read(cursor), peek['cursor'])
        self.assertEqual(json.loads(self.output('--peek', '--json'))['messages'], [])
        self.assertNotIn('unread', report.cli_run(self.root, agent=self.agent, phase='working'))
        # A cursor past the end of the log makes the inbox unreadable: the report still succeeds, without a count.
        cursor.write_text('999999')
        self.assertNotIn('unread', report.cli_run(self.root, agent=self.agent, phase='done'))
        self.assertEqual(store.read(self.directory / 'properties.json')['phase'], 'done')
        self.assertEqual(cursor.read_text(), '999999')

    def test_a_message_is_consumed_only_after_its_output_succeeded(self):
        """A message is consumed only after its output was written and flushed, and a concurrent reader cannot
        overtake a failed one; fragile because of the order of output, flush and cursor commit, and a break silently
        loses messages on a broken pipe."""
        self.message('already read')
        self.output()
        position = (self.directory / 'inbox.cursor').read_bytes()
        self.message('must survive')
        for method in ('write', 'flush'):
            for machine in (False, True):
                for wait in (False, True):
                    stream = io.StringIO()
                    with self.subTest(method=method, json=machine, wait=wait), \
                            patch.object(stream, method, side_effect=BrokenPipeError()), redirect_stdout(stream), \
                            self.assertRaises(BrokenPipeError):
                        inbox_tool.cli_run(self.root, agent=self.agent, json=machine, wait=wait,
                                           timeout='1s' if wait else None)
                    self.assertEqual((self.directory / 'inbox.cursor').read_bytes(), position)
        self.assertIn('must survive', self.output())
        position = (self.directory / 'inbox.cursor').read_bytes()
        message = self.message('serialize output and consume')
        presenting, release, second_started = threading.Event(), threading.Event(), threading.Event()
        def fail_output(result):
            self.assertEqual(result['messages'], [message])
            self.assertEqual((self.directory / 'inbox.cursor').read_bytes(), position)
            presenting.set()
            self.assertTrue(release.wait(5))
            raise BrokenPipeError()
        def second_read():
            second_started.set()
            return mailbox.inbox(self.directory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(mailbox.inbox, self.directory, present=fail_output)
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

    def test_wait_ignores_native_delivery_and_reads_only_after_inbox_changes(self):
        """inbox --wait wakes only on inbox-delivered messages, never on natively delivered steers, and a waiting
        reader whose launch was replaced reads nothing; a break makes agents read steers twice, which nobody
        notices."""
        scanned = threading.Event()
        original = mailbox.inbox
        def scan(*args, **kwargs):
            scanned.set()
            return original(*args, **kwargs)
        with patch.object(mailbox, 'inbox', side_effect=scan) as calls, ThreadPoolExecutor(max_workers=1) as pool:
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
        self.assertEqual(mailbox.inbox(self.directory)['messages'], [])
        scanned.clear()
        with patch.object(mailbox, 'inbox', side_effect=scan), ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(inbox_tool.run, self.root, agent=self.agent, wait=True, timeout='3s')
            self.assertTrue(scanned.wait(2))
            with store.lock(self.directory / '.launch.lock'):  # Resume publishes its replacement between polls.
                store.write(self.directory / 'runtime.json', {'launch_id': 'replacement'})
                self.message('for the replacement')
            with self.assertRaisesRegex(ValueError, 'Outdated launch'):
                waiting.result(timeout=3)
        store.write(self.directory / 'runtime.json', {'launch_id': 'current'})
        self.assertEqual([message['text'] for message in mailbox.inbox(self.directory)['messages']],
                         ['for the replacement'])
        process = subprocess.Popen([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root),
            'inbox', '--agent', self.agent, '--wait', '--timeout', '3s'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.wait(timeout=5))
        self.addCleanup(lambda: process.terminate() if process.poll() is None else None)
        self.message('arrived for waiting reader')
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)
        self.assertIn('arrived for waiting reader', output)
        self.assertEqual(mailbox.inbox(self.directory)['messages'], [])
