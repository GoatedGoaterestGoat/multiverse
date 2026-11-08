"""Focus: a focused agent refuses steer and follow-up messages and takes inbox-only ones."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest
from isolation import track
from multiverse import cli, harnesses, store
from multiverse.harnesses.types import AgentIdentitySpec, NativeEndpoint, NativeResult, NativeServiceHandle, ServiceIdentity
from multiverse.runtime import agent_identity, focus_note, native
from multiverse.terminal import statusline
from multiverse.tools import focus as focus_tool, listen, send, unfocus
from multiverse.universe import agents, focus, mailbox, templates


SPECIAL_FOCUS = '''import os,sys,threading
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from multiverse.universe import focus
from multiverse.terminal import statusline
directory=Path(sys.argv[2])
assert threading.active_count()==1
for reader,expected in ((focus.current,None),(statusline.focus_text,'')):
    descriptors=len(os.listdir('/dev/fd'))
    for _ in range(20):
        assert reader(directory)==expected
    remaining=len(os.listdir('/dev/fd'))
    assert remaining==descriptors,(reader.__name__,remaining,descriptors)
'''


class FocusRecordTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents' / str(uuid4())
        self.launch = str(uuid4())
        store.write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'asker'})
        store.write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'working'})

    def status(self, value):
        agents.update_runtime(self.directory, self.launch, status=value)

    def test_durations(self):
        """Focus lengths parse as mv2 documents them and refuse anything else; a loose parse would focus an agent for
        a length nobody chose."""
        self.assertEqual(focus.seconds('5m'), 300)
        self.assertEqual(focus.seconds(' 1h30m '), 5400)
        self.assertEqual(focus.seconds('90s'), 90)
        self.assertEqual(focus.seconds('1d'), 86400)
        for value in ('20', '5 m', '1h-5m', '0m', '2d', '5x', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                focus.seconds(value)

    def test_timed_focus_ends_at_its_time_and_refocus_keeps_since(self):
        """A timed focus ends by itself and a refocus keeps its start; a break leaves an agent refusing messages after
        its time, or miscounts what arrived during the focus."""
        record = focus.start(self.directory, duration=600)
        self.assertEqual(focus.current(self.directory), record)
        self.assertTrue(focus.ends(record).startswith('until '))
        self.assertIn('min left', focus.ends(record))
        later = focus.start(self.directory)
        self.assertEqual(later['since'], record['since'])
        self.assertIsNone(later['until'])
        self.assertEqual(focus.ends(later), 'until unfocused')
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        store.write(self.directory / focus.FILE, {**record, 'until': past})
        self.assertIsNone(focus.current(self.directory))
        # A new focus after an expired one starts a new period.
        self.assertNotEqual(focus.start(self.directory)['since'], record['since'])
        self.assertIsNotNone(focus.end(self.directory))
        self.assertIsNone(focus.end(self.directory))
        self.assertFalse((self.directory / focus.FILE).exists())

    def test_a_damaged_focus_never_blocks_and_never_raises(self):
        """Any damaged focus.json counts as no focus, and reading, ending or counting a turn against it never raises;
        otherwise a bad file refuses every steer to that agent with no way to see why, crashes the dashboard and
        list (a far-future until), fails every status update (deep nesting), or leaks a descriptor on every
        dashboard refresh (a directory)."""
        now = datetime.now(timezone.utc)
        valid = {'since': now.isoformat(), 'until': None, 'turns': 1, 'by': 'user'}
        with self.subTest('the record each case damages'):
            store.write(self.directory / focus.FILE, valid)
            self.assertIsNotNone(focus.current(self.directory))
            focus.end(self.directory)
        damaged = {
            'not json': '{not json',
            'not an object': '[1, 2]',
            'deeply nested': '[' * 100000 + ']' * 100000,   # within the size limit, past the parser's depth
            'too large': json.dumps({**valid, 'pad': 'x' * focus.READ_LIMIT}),
            'bad since': {**valid, 'since': 'not-a-time'},
            'no since': {key: value for key, value in valid.items() if key != 'since'},
            'bad until': {**valid, 'until': 'soon'},
            'far-future until': {**valid, 'until': '9999-12-31T23:59:59-14:00'},
            'until past the longest focus': {**valid, 'until': (now + timedelta(days=2)).isoformat()},
            # start writes 1 or 2 turns; a huge count would never run out.
            **{f'turns {value!r}': {**valid, 'turns': value} for value in (0, -1, 3, 10**30, 'one', {}, True, 1.5)},
            'no by': {key: value for key, value in valid.items() if key != 'by'},
            **{f'by {value!r}': {**valid, 'by': value} for value in ('somebody', ['user'])},
        }
        for label, value in damaged.items():
            with self.subTest(label):
                path = self.directory / focus.FILE
                if isinstance(value, str):
                    path.write_text(value)
                else:
                    store.write(path, value)
                self.assertIsNone(focus.current(self.directory))
                self.assertEqual(statusline.focus_text(self.directory), '')   # the tmux bar's own copy agrees
                self.status('working')
                self.status('idle')    # counts a turn end against the damaged record without raising
                self.assertIsNone(focus.end(self.directory))
                self.assertFalse(path.exists())
        for label in ('directory', 'fifo'):
            with self.subTest(label):
                path = self.directory / focus.FILE
                path.mkdir() if label == 'directory' else os.mkfifo(path)
                # The runner's sampler opens unrelated FDs in this process. Check each reader in a single-threaded
                # child instead, retaining exact FD counts and a timeout for a FIFO that would block.
                result = subprocess.run([sys.executable, '-I', '-c', SPECIAL_FOCUS,
                                         str(Path(__file__).resolve().parents[1] / 'src'), str(self.directory)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.status('working')
                self.status('idle')
                self.assertIsNone(focus.end(self.directory))
                path.rmdir() if label == 'directory' else self.assertFalse(path.exists())

    def test_turn_focus_set_mid_turn_lasts_through_the_next_turn(self):
        """A turn focus started while working outlasts that turn and ends with the next one, and only a working or
        waiting to idle change counts; a break ends the focus before the user answers."""
        focus.start(self.directory, turn=True)
        self.status('idle')        # the turn that asked the question ends
        self.assertIsNotNone(focus.current(self.directory))
        self.status('idle')        # an unchanged status is not a turn end
        self.status('working')     # the person answers
        self.status('waiting')     # a permission prompt inside that turn
        self.assertIsNotNone(focus.current(self.directory))
        self.status('idle')
        self.assertIsNone(focus.current(self.directory))
        self.assertFalse((self.directory / focus.FILE).exists())

    def test_turn_focus_set_while_idle_or_waiting_lasts_one_turn(self):
        """A turn focus the person starts while the agent is idle or waiting on them ends after exactly one turn, the
        one that takes the answer, but one the agent starts itself always outlasts its current turn; a break holds
        messages back for a turn longer, or none."""
        for status in ('idle', 'waiting'):
            with self.subTest(status=status):
                self.status(status)
                focus.start(self.directory, turn=True)
                if status == 'idle':
                    self.status('working')
                self.assertIsNotNone(focus.current(self.directory))
                self.status('idle')
                self.assertIsNone(focus.current(self.directory))
        # The agent runs focus --turn inside its turn, whatever its last reported status says.
        self.status('waiting')
        self.assertEqual(focus.start(self.directory, turn=True, by='agent')['turns'], 2)

    def test_a_turn_end_saved_before_a_turn_focus_never_counts_against_it(self):
        """A focus --turn that starts just after a turn end was saved is not consumed by that turn end; a break ends
        the new focus at once, before the turn it was meant for."""
        self.status('working')
        started = []
        def focus_in_the_gap(*arguments, **options):
            # Runs where the turn end used to be counted: after the status is saved and the runtime lock released.
            if not started:
                started.append(focus.start(self.directory, turn=True))
        with patch.object(agents, '_event', side_effect=focus_in_the_gap):
            self.status('idle')
        self.assertEqual(started[0]['turns'], 1)
        self.assertEqual(focus.current(self.directory), started[0])

    def test_a_focus_waits_for_a_send_in_progress(self):
        """A focus starts only after a send that found no focus has finished; a break lets a steer that was admitted
        before the focus be saved and submitted after focus said it was in effect."""
        done = threading.Event()
        with store.lock(self.directory / '.send.lock'):
            worker = threading.Thread(target=lambda: (focus.start(self.directory), done.set()))
            worker.start()
            self.assertFalse(done.wait(0.3))
            self.assertIsNone(focus.current(self.directory))
        worker.join(5)
        self.assertTrue(done.is_set())
        self.assertIsNotNone(focus.current(self.directory))

    def test_focus_never_recreates_a_removed_agent_folder(self):
        """focus and unfocus on an agent folder that was removed fail and leave nothing behind; a break brings back a
        damaged leftover folder that list then shows."""
        directory = self.root / 'state/agents' / str(uuid4())
        for label, action in (('focus', focus.start), ('unfocus', focus.end)):
            with self.subTest(label), self.assertRaises(OSError):
                action(directory)
            self.assertFalse(directory.exists())

    def test_a_relaunch_is_not_a_turn_end_and_timed_focus_ignores_turns(self):
        """A relaunch (unknown then idle) is not a turn end, and a timed focus ignores turns; either break ends a
        focus early."""
        self.status('idle')
        focus.start(self.directory, turn=True)
        self.status('unknown')
        self.status('idle')
        self.assertIsNotNone(focus.current(self.directory))
        focus.start(self.directory, duration=600)
        self.status('working')
        self.status('idle')
        self.assertIsNotNone(focus.current(self.directory))


class FocusCommandTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directories = {}
        self.launch = str(uuid4())
        for name in ('asker', 'helper', 'other'):
            directory = self.root / 'state/agents' / str(uuid4())
            self.directories[name] = directory
            store.write(directory / 'properties.json', {'id': directory.name, 'name': name, 'phase': 'working',
                'summary': 'Waiting on the user', 'harness': 'claude', 'workspace': str(self.work),
                'universe_id': store.read(self.root / 'universe.json')['id'], 'native_session_id': str(uuid4())})
            store.write(directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'idle',
                'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}})
            (directory / '.launch.lock').touch()   # a launched agent's; held and listen never create it
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.calls = []
        owner = self
        class Adapter:
            def send(self, session, message, executor):
                owner.calls.append((session.agent_id, message.delivery))
                return NativeResult('native_queued', 'Native safe-boundary steering accepted; consumption unconfirmed', {})
            def get_delivery(self, session, message_id, executor):
                return NativeResult('native_queued', 'queued', {})
        self.enterContext(patch.object(harnesses, 'load', return_value=Adapter()))

    def output(self, *arguments, code=0, agent=None):
        """Run mv2 as the person, or as a launch-strict agent session; stderr is kept in self.stderr."""
        output, errors = io.StringIO(), io.StringIO()
        environment = self.agent_environment(agent) if agent else {}
        with redirect_stdout(output), redirect_stderr(errors), patch.dict(os.environ, environment), \
                patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *arguments]):
            try:
                cli.main()
            except SystemExit as error:
                self.assertEqual(error.code, code, output.getvalue() + errors.getvalue())
            else:
                self.assertEqual(code, 0, output.getvalue())
        self.stderr = errors.getvalue()
        return output.getvalue()

    def agent_environment(self, name):
        directory = self.directories[name]
        return {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': directory.name, 'MV2_AGENT_DIR': str(directory),
                'MV2_LAUNCH_ID': self.launch}

    def history(self, name):
        return mailbox.inbox(self.directories[name], all_messages=True, limit=None)['messages']

    def test_steer_and_follow_up_to_a_focused_agent_wait_in_its_inbox(self):
        """Steer and follow-up to a focused agent are saved inbox-only, naming the focus and what they were meant to
        be, and never reach its harness, while other recipients are not held up; a break loses the message or buries
        the question the focus protects."""
        self.output('focus', 'asker', '--for', '20m')
        held = focus.current(self.directories['asker'])
        line = 'asker is currently focused. Your message has been delivered as an inbox message instead.\n'
        for delivery in ('steer', 'follow-up'):
            with self.subTest(delivery=delivery):
                self.assertEqual(self.output('send', 'asker', '--text', 'start the benchmark', '--delivery', delivery,
                                             agent='helper'), line)
        record = json.loads(self.output('send', 'asker', '--text', 'start the benchmark', '--json'))
        self.assertEqual((record['state'], record['saved'], record['ok'], record['delivery'], record['intended']),
                         ('held', True, True, 'inbox-only', 'steer'))
        self.assertEqual([(m['delivery'], m['intended'], m['focus']) for m in self.history('asker')],
                         [('inbox-only', 'steer', held['id']), ('inbox-only', 'follow-up', held['id']),
                          ('inbox-only', 'steer', held['id'])])
        self.assertEqual(focus.last(self.directories['asker'])['held'], 3)
        self.assertEqual(self.calls, [])
        output = self.output('send', 'asker,other', '--text', 'when you are free', agent='helper')
        self.assertEqual(output, line + 'Sent to other; it reads this at its next pause.\n')
        self.assertEqual([agent for agent, _ in self.calls], [self.directories['other'].name])
        self.output('send', 'asker', '--text', 'also this', '--queue-only', agent='helper')
        self.assertNotIn('intended', self.history('asker')[-1])

    def test_held_reads_only_what_the_last_focus_held_and_the_note_goes_to_the_agent_that_unfocuses(self):
        """mv2 held reads and marks read only the unread messages the last focus held, however it ended, leaving
        other unread messages unread, and only an agent unfocusing itself is told about them; a break shows other
        messages, repeats read ones, or tells the agent nothing about the messages it held."""
        directory = self.directories['asker']
        self.output('focus', agent='asker')
        self.output('send', 'asker', '--text', 'stop the benchmark', agent='helper')
        self.output('held', agent='asker', code=1)
        self.assertIn('You are focused', self.stderr)
        self.output('send', 'asker', '--text', 'fyi', '--delivery', 'inbox-only', agent='helper')
        self.assertEqual(self.output('unfocus', agent='asker'), 'Unfocused; messages reach you normally again. You '
                         'received 1 steer or follow-up message during your focus. Use mv2 held to check only that '
                         'message. You also have 1 other unread inbox message: read it with mv2 inbox.\n')
        self.assertEqual(self.output('held', agent='asker'),
                         'Message from helper (steer, held while you were focused):\nstop the benchmark\n')
        self.assertEqual(self.output('held', agent='asker'), 'No unread messages held during your last focus.\n')
        self.assertEqual(statusline.unread(directory), 1)   # the tmux bar's own count skips it too
        self.assertEqual([m['text'] for m in json.loads(self.output('inbox', '--peek', '--json',
                                                                    agent='asker'))['messages']], ['fyi'])
        # A focus the user ends gets no note, and mv2 held reads only the latest focus's messages.
        self.output('focus', 'asker')
        self.output('send', 'asker', '--text', 'next task', '--delivery', 'follow-up', agent='helper')
        self.assertNotIn('held', self.output('unfocus', 'asker'))
        self.output('focus', 'asker')
        self.output('unfocus', 'asker')
        self.assertEqual(self.output('held', agent='asker'), 'No unread messages held during your last focus.\n')
        self.assertEqual(self.output('inbox', agent='asker'), 'Message from helper:\nfyi\n\nMessage from helper '
                         '(follow-up, held while you were focused):\nnext task\n')
        self.assertEqual(self.output('inbox', agent='asker'), 'No unread messages.\n')
        self.output('focus', agent='asker')
        self.output('send', 'asker', '--text', 'read early', agent='helper')
        mailbox.inbox(directory)    # read with mv2 inbox during the focus: nothing left to note or hold
        self.assertEqual(self.output('unfocus', agent='asker'), 'Unfocused; messages reach you normally again.\n')
        self.assertEqual(self.output('held', agent='asker'), 'No unread messages held during your last focus.\n')
        # A focus that begins while mv2 held reads waits for it: held never reads into a new focus.
        self.output('focus', agent='asker')
        self.output('send', 'asker', '--text', 'late', agent='helper')
        self.output('unfocus', agent='asker')
        original, waited = mailbox.held, []
        def focus_meanwhile(*arguments, **options):
            starting = threading.Thread(target=focus.start, args=(directory,))
            starting.start()
            starting.join(0.3)
            waited.append(starting.is_alive())
            self.addCleanup(starting.join, 5)
            return original(*arguments, **options)
        with patch.object(mailbox, 'held', side_effect=focus_meanwhile):
            self.assertIn('late', self.output('held', agent='asker'))
        self.assertEqual(waited, [True])

    def test_a_turn_focus_end_sends_one_note_while_no_listener_reports(self):
        """A turn focus, the agent's or the user's, that ends with held messages unread sends one follow-up note from
        mv2, and none when they were read or a listener claims the report; without it the agent never learns of them."""
        directory = self.directories['asker']
        spawned = []
        original_note = focus_note.run
        def spawn(agent_dir, focus_id):
            spawned.append(focus_id)
            focus_note.run(agent_dir, focus_id, wait=0)
        self.enterContext(patch.object(focus, '_spawn', side_effect=spawn))
        def turns(*statuses):
            for status in statuses:
                agents.update_runtime(directory, self.launch, status=status)
        for who in ('agent', 'user'):
            with self.subTest(focused_by=who):
                self.calls.clear()
                turns('working')
                if who == 'agent':
                    self.output('focus', '--turn', agent='asker')
                else:
                    self.output('focus', 'asker', '--turn')
                self.output('send', 'asker', '--text', 'one', agent='helper')
                self.output('send', 'asker', '--text', 'two', '--delivery', 'follow-up', agent='helper')
                turns('idle', 'working')
                self.assertEqual(self.calls, [])
                turns('idle')
                self.assertIsNone(focus.current(directory))
                self.assertEqual(self.calls, [(directory.name, 'follow-up')])
                note = self.history('asker')[-1]
                self.assertEqual((note['from']['name'], note['delivery'], note['text']),
                                 ('mv2', 'follow-up', focus.note(2)))
                mailbox.inbox(directory)
        # Read before the focus ended, or claimed by a listener: no note.
        self.calls.clear()
        turns('working')
        self.output('focus', '--turn', agent='asker')
        self.output('send', 'asker', '--text', 'three', agent='helper')
        mailbox.inbox(directory)
        turns('idle', 'working', 'idle', 'working')
        self.output('focus', '--turn', agent='asker')
        self.output('send', 'asker', '--text', 'four', agent='helper')
        turns('idle', 'working')
        with focus.listening(directory):
            store.write(directory / focus.LAST, {**focus.last(directory), 'report': 'listener'})
            turns('idle')
        self.assertEqual((len(spawned), self.calls), (4, []))
        # A focus that begins before the note is sent: the note is dropped, never held.
        turns('working')
        self.output('focus', '--turn', agent='asker')
        self.output('send', 'asker', '--text', 'five', agent='helper')
        sending = send.run
        def refocus_then_send(*arguments, **options):
            focus.start(directory)   # the person focuses it again
            return sending(*arguments, **options)
        with patch.object(send, 'run', side_effect=refocus_then_send):
            turns('idle', 'working', 'idle')
        self.assertEqual(len(spawned), 5)
        self.assertEqual(self.history('asker')[-1]['text'], 'five')
        self.assertEqual((focus.last(directory)['held'], self.calls), (0, []))
        # Held messages read after the note counted them, before it is saved: the note is dropped.
        focus.end(directory)
        turns('working')
        self.output('focus', '--turn', agent='asker')
        self.output('send', 'asker', '--text', 'six', agent='helper')
        def read_then_send(*arguments, **options):
            mailbox.held(directory, options['_note_for'])
            return sending(*arguments, **options)
        with patch.object(send, 'run', side_effect=read_then_send):
            turns('idle', 'working', 'idle')
        self.assertEqual((len(spawned), self.history('asker')[-1]['text'], self.calls), (6, 'six', []))
        self.assertIsNone(focus.last(directory)['report'])
        # A note that fails before it is saved claims nothing, so a listener still reports, and a later note may go.
        focus.end(directory)
        turns('working')
        self.output('focus', '--turn', agent='asker')
        self.output('send', 'asker', '--text', 'seven', agent='helper')
        send_lock = directory / '.send.lock'
        def damaged_send(*arguments, **options):
            send_lock.unlink()
            send_lock.mkdir()   # the real send fails on its lock, before saving anything
            try:
                return sending(*arguments, **options)
            finally:
                send_lock.rmdir()
                send_lock.touch()
        with patch.object(send, 'run', side_effect=damaged_send), \
                patch.object(focus_note, 'run', side_effect=lambda *a, **k: self.assertRaises(
                    IsADirectoryError, original_note, *a, **k)):
            turns('idle', 'working', 'idle')
        self.assertEqual((self.history('asker')[-1]['text'], focus.last(directory)['report']), ('seven', None))
        held = mailbox.inbox(directory, limit=None, peek=True)['messages']
        with focus.locked(directory):
            self.assertIsNotNone(listen._report(directory, held, datetime.now(timezone.utc) - timedelta(seconds=60)))
        original_note(directory, focus.last(directory)['id'], wait=0)
        note = self.history('asker')[-1]
        self.assertEqual((note['text'], focus.last(directory)['report'], focus.last(directory)['note']),
                         (focus.note(1), 'note', note['id']))
        # A listener that reports after the note looked, before it is saved: the note stays unsaid.
        mailbox.inbox(directory)
        turns('working')
        self.output('focus', '--turn', agent='asker')
        self.output('send', 'asker', '--text', 'eight', agent='helper')
        self.calls.clear()
        def listener_reports_then_send(*arguments, **options):
            with focus.locked(directory):
                self.assertTrue(focus.claim_locked(directory, options['_note_for'], 'listener'))
            return sending(*arguments, **options)
        with patch.object(send, 'run', side_effect=listener_reports_then_send):
            turns('idle', 'working', 'idle')
        self.assertEqual((self.history('asker')[-1]['text'], focus.last(directory)['report'], self.calls),
                         ('eight', 'listener', []))

    def test_a_saved_note_stays_the_report_when_its_claim_was_never_written(self):
        """A note saved before its claim was written, by a sender that then failed or died, is still the report: a
        listener that waited through the focus stays quiet, and a rerun sends no second note and replays nothing;
        a break notifies the agent twice. Another focus's note or an ordinary follow-up is no such note."""
        directory = self.directories['asker']
        def ended_focus():
            record = focus.start(directory)
            self.output('send', 'asker', '--text', 'held work', agent='helper')
            focus.end(directory)
            return record['id']
        before = datetime.now(timezone.utc) - timedelta(seconds=60)
        def listener_report():
            unread = mailbox.inbox(directory, limit=None, peek=True)['messages']
            with focus.locked(directory):
                return listen._report(directory, unread, before)
        # Controls: an ordinary follow-up, and mv2's note about another focus, leave the report to the listener.
        focus_id = ended_focus()
        mailbox.append_message(directory, focus.note(1), {'name': 'mv2', 'agent_id': None, 'core': True}, 'follow-up',
                               focus_note='another focus')
        mailbox.append_message(directory, focus.note(1), {'name': 'user', 'agent_id': None}, 'follow-up', focus_note=focus_id)
        report = listener_report()
        self.assertIsNotNone(report)
        self.assertEqual(report[1], focus_id)   # the listener reports, and would claim this focus
        self.assertIsNone(focus.last(directory)['report'])
        mailbox.inbox(directory)
        # The claim's write fails after the note was saved; nothing was submitted.
        focus_id = ended_focus()
        with patch.object(focus, 'commit_note', side_effect=OSError('injected claim write failure')), \
                patch.object(native, 'send_or_wake') as submitted, self.assertRaisesRegex(OSError, 'claim write'):
            focus_note.run(directory, focus_id, wait=0)
        submitted.assert_not_called()
        note = self.history('asker')[-1]
        self.assertEqual((note.get('focus_note'), focus.last(directory)['report']), (focus_id, None))
        # A listener that waited through the focus finds the saved note and stays quiet; one started later reports.
        self.assertIsNone(listener_report())
        self.assertEqual((focus.last(directory)['report'], focus.last(directory)['note']), ('note', note['id']))
        unread = mailbox.inbox(directory, limit=None, peek=True)['messages']
        with focus.locked(directory):
            self.assertIsNotNone(listen._report(directory, unread, datetime.now(timezone.utc)))
        # A rerun of the note's sender adds nothing and submits nothing.
        with patch.object(native, 'send_or_wake') as submitted:
            focus_note.run(directory, focus_id, wait=0)
        submitted.assert_not_called()
        self.assertEqual(self.history('asker')[-1]['id'], note['id'])
        # Without a listener first, the rerun itself adopts the saved note.
        mailbox.inbox(directory)
        focus_id = ended_focus()
        with patch.object(focus, 'commit_note', side_effect=OSError('injected claim write failure')), \
                self.assertRaises(OSError):
            focus_note.run(directory, focus_id, wait=0)
        orphan = self.history('asker')[-1]['id']
        with patch.object(native, 'send_or_wake') as submitted:
            focus_note.run(directory, focus_id, wait=0)
        submitted.assert_not_called()
        self.assertEqual((self.history('asker')[-1]['id'], focus.last(directory)['note']), (orphan, orphan))
        # A note whose native submission is uncertain stays single and is never sent again.
        mailbox.inbox(directory)
        focus_id = ended_focus()
        with patch.object(native, 'send_or_wake', return_value={'state': 'unknown', 'ok': False,
                                                                'reason': 'injected uncertainty'}) as submitted:
            focus_note.run(directory, focus_id, wait=0)
            focus_note.run(directory, focus_id, wait=0)
        self.assertEqual(submitted.call_count, 1)
        self.assertEqual(sum(m.get('focus_note') == focus_id for m in self.history('asker')), 1)
        # Its claim lost later, as by a damaged or restored focus-last.json: the same note is recovered, with no
        # send, no second note and its delivery tracking untouched.
        noted = focus.last(directory)['note']
        mailbox.update_message(directory, noted, submission_state='unknown', attempts=[{'state': 'unknown'}])
        ledger = (directory / 'inbox-state.json').read_bytes()
        store.write(directory / focus.LAST, {**{key: value for key, value in focus.last(directory).items()
                                                if key != 'note'}, 'report': None})
        with patch.object(native, 'send_or_wake') as submitted:
            focus_note.run(directory, focus_id, wait=0)
        submitted.assert_not_called()
        self.assertEqual((focus.last(directory)['report'], focus.last(directory)['note']), ('note', noted))
        self.assertEqual(sum(m.get('focus_note') == focus_id for m in self.history('asker')), 1)
        self.assertEqual((directory / 'inbox-state.json').read_bytes(), ledger)
        self.assertFalse(list((directory / 'inbox-unsubmitted').glob('*.json')))

    def test_unfocus_restores_delivery_and_points_at_the_inbox(self):
        """unfocus restores delivery and says how many unread messages wait, and how many came during the focus;
        without the count the agent never reads what was held back."""
        self.assertEqual(self.output('send', 'asker', '--text', 'before', '--queue-only'),
                         "Saved to asker's inbox; it sees it at its next check.\n")
        output = self.output('focus', agent='asker')
        self.assertIn('Focused until unfocused.', output)
        self.assertIn('Run mv2 unfocus once the user has answered', output)
        self.output('send', 'asker', '--text', 'during', '--delivery', 'inbox-only')
        output = self.output('unfocus', agent='asker')
        self.assertEqual(output, 'Unfocused; messages reach you normally again. You have 2 unread inbox messages, '
                                 '1 sent while you were focused: read them now with mv2 inbox.\n')
        self.assertEqual(self.output('send', 'asker', '--text', 'now'), 'Sent to asker; it reads this at its next pause.\n')
        self.assertEqual(self.output('unfocus', agent='asker'), 'You were not focused; messages already reach you '
                         'normally. You have 2 unread inbox messages: read them now with mv2 inbox.\n')
        mailbox.inbox(self.directories['asker'])
        self.assertEqual(self.output('unfocus', 'asker'), 'asker was not focused.\n')
        self.output('focus', agent='asker')
        self.output('send', 'asker', '--text', 'only', '--delivery', 'inbox-only')
        self.assertEqual(self.output('unfocus', agent='asker'), 'Unfocused; messages reach you normally again. You '
                         'have 1 unread inbox message, sent while you were focused: read it now with mv2 inbox.\n')

    def test_agents_change_only_their_own_focus(self):
        """An agent can focus and unfocus only itself while the person can change any agent; otherwise one agent could
        silence another or clear its focus."""
        self.output('focus', agent='asker')
        self.assertTrue(focus.current(self.directories['asker']))
        for action in ('focus', 'unfocus'):
            with self.subTest(action=action):
                self.output(action, 'asker', agent='helper', code=1)
                self.assertIn('only their own focus', self.stderr)
        self.assertTrue(focus.current(self.directories['asker']))
        # The person (no agent identity) and the dashboard may change anyone's.
        with patch.dict(os.environ, self.agent_environment('helper')):
            self.assertTrue(unfocus.run(self.root, agent='asker', as_user=True)['was_focused'])
        self.assertIn('Focused asker until unfocused', self.output('focus', 'asker'))
        with self.assertRaisesRegex(ValueError, 'Name the agent'):
            focus_tool.run(self.root)
        # Only the person focuses for a time.
        self.output('focus', '--for', '5m', agent='asker', code=1)
        self.assertIn('Only the user can focus you for a time', self.stderr)
        self.output('focus', 'asker', '--for', '5m', agent='asker', code=1)

    def test_a_mismatched_or_outdated_agent_identity_is_refused(self):
        """focus and unfocus inside an agent need the same verified identity as send: an agent can't claim another's
        ID, directory or launch, and an outdated launch is refused; a break lets one agent silence another or clear
        the focus the person set on it."""
        asker, helper = self.directories['asker'], self.directories['helper']
        focus.start(asker)
        mine = self.agent_environment('helper')
        claims = {
            "another agent's ID": ({**mine, 'MV2_AGENT_ID': asker.name}, 'does not match its saved record'),
            "another agent's directory": ({**mine, 'MV2_AGENT_DIR': str(asker)}, 'does not match its saved record'),
            'an outdated launch': ({**mine, 'MV2_LAUNCH_ID': str(uuid4())},
                                   'Outdated launch; refusing to change focus as this agent'),
            'no universe': ({key: value for key, value in mine.items() if key != 'MV2_UNIVERSE'}, 'incomplete'),
        }
        for label, (environment, expected) in claims.items():
            for action in (unfocus.run, focus_tool.run):
                with self.subTest(label, action=action.__module__), patch.dict(os.environ, environment), \
                        self.assertRaisesRegex(ValueError, expected):
                    action(self.root)
        self.assertTrue(focus.current(asker))
        self.assertIsNone(focus.current(helper))

    def test_an_agent_cannot_change_a_same_id_agent_in_another_universe(self):
        """A verified agent naming an agent in another universe that has its own ID is refused; a break compares IDs
        only, so -u OTHER changes another universe's agent."""
        directory = self.directories['asker']
        spec = AgentIdentitySpec('fixture-agent-v1', 'FIXTURE_THREAD', ('FIXTURE_HOME',))
        service = NativeServiceHandle(ServiceIdentity('fixture', {'FIXTURE_HOME': str(self.work)}),
                                      NativeEndpoint('unix-websocket', str(self.work / 'service.sock')))
        properties = {**store.read(directory / 'properties.json'), 'native_service_identity': asdict(service.identity)}
        properties['agent_identity'] = agent_identity.record(spec, properties, service)
        store.write(directory / 'properties.json', properties)
        other = templates.create(self.work / 'other' / '.multiverse', 'other', workdir=self.work / 'other')
        twin = other / 'state/agents' / directory.name
        store.write(twin / 'properties.json', {'id': directory.name, 'name': 'twin', 'harness': 'claude',
            'workspace': str(self.work), 'universe_id': store.read(other / 'universe.json')['id']})
        store.write(twin / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'idle'})
        stable = {'MV2_IDENTITY_MODE': spec.mode, 'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': directory.name,
                  'MV2_AGENT_DIR': str(directory), 'FIXTURE_THREAD': properties['native_session_id'],
                  'FIXTURE_HOME': str(self.work)}
        with patch.dict(os.environ, stable):
            with self.assertRaisesRegex(ValueError, 'only their own focus'):
                focus_tool.run(other, agent='twin')
            self.assertIsNone(focus.current(twin))
            self.assertEqual(focus_tool.run(other)['agent'], 'asker')   # no name: itself, in its own universe
        self.assertTrue(focus.current(directory))

    def test_the_claude_mods_status_reports_end_a_turn_focus(self):
        """The status reports the Claude mod makes (python -m multiverse.tools.runtime_update --status, in the agent's
        environment) count turn ends, so a turn focus the agent set ends once the user's answer turn ends; a break
        leaves Claude agents focused for good, which only this path shows."""
        directory = self.directories['asker']
        source = str(Path(__file__).resolve().parents[1] / 'src')
        environment = {**os.environ, **self.agent_environment('asker'), 'PYTHONPATH': source}
        def report(status, *extra):
            # Exactly the mod's argv (harnesses/claude/mod/hooks/index.ts, report()).
            result = subprocess.run([environment['MV2_PYTHON'], '-m', 'multiverse.tools.runtime_update',
                                     '--status', status, *extra], env=environment, cwd=self.work,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
        report('working', '--bridge', 'claude-mod-v1')
        self.output('focus', '--turn', agent='asker')
        self.assertEqual(focus.current(directory)['turns'], 2)
        report('idle')       # the turn that asked ends
        self.assertEqual(focus.current(directory)['turns'], 1)
        report('working')    # the user answers
        report('idle')
        self.assertIsNone(focus.current(directory))

    def test_a_verified_agent_identity_focuses_itself_and_an_unverified_one_is_refused(self):
        """A bridge agent identified by its verified conversation acts on itself, and an unverified one is refused; a
        break lets a mismatched process change a focus."""
        directory = self.directories['asker']
        spec = AgentIdentitySpec('fixture-agent-v1', 'FIXTURE_THREAD', ('FIXTURE_HOME',))
        service = NativeServiceHandle(ServiceIdentity('fixture', {'FIXTURE_HOME': str(self.work)}),
                                      NativeEndpoint('unix-websocket', str(self.work / 'service.sock')))
        properties = {**store.read(directory / 'properties.json'), 'native_service_identity': asdict(service.identity)}
        properties['agent_identity'] = agent_identity.record(spec, properties, service)
        store.write(directory / 'properties.json', properties)
        stable = {'MV2_IDENTITY_MODE': spec.mode, 'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': directory.name,
                  'MV2_AGENT_DIR': str(directory), 'FIXTURE_THREAD': properties['native_session_id'],
                  'FIXTURE_HOME': str(self.work)}
        with patch.dict(os.environ, stable):
            self.assertEqual(focus_tool.run(self.root)['by'], 'agent')
            with self.assertRaisesRegex(ValueError, 'only their own focus'):
                unfocus.run(self.root, agent='other')
        with patch.dict(os.environ, {**stable, 'FIXTURE_THREAD': 'another conversation'}), \
                self.assertRaisesRegex(ValueError, 'identity could not be verified'):
            unfocus.run(self.root)
        self.assertTrue(focus.current(directory))

    def test_turn_focus_needs_reported_turns_and_ends_with_the_next_turn(self):
        """focus --turn is refused while the harness reports no status, and once accepted it holds sends until the
        turn ends; otherwise a turn focus would never end."""
        senders = []
        def finish_note(process):
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                self.fail('The focus-end note sender did not finish')
        def start_note(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs)
            senders.append(process)
            self.addCleanup(finish_note, process)
            track(pid=process.pid)
            return process
        # Turn reporting deliberately detaches the note. Reap that exact child before BaseTest removes its files.
        self.enterContext(patch.object(focus, 'subprocess',
                                       SimpleNamespace(Popen=start_note, DEVNULL=subprocess.DEVNULL)))
        directory = self.directories['asker']
        store.write(directory / 'runtime.json', {**store.read(directory / 'runtime.json'), 'status': 'unknown'})
        with self.assertRaisesRegex(ValueError, 'not reporting its turns'):
            focus_tool.run(self.root, agent='asker', turn=True)
        agents.update_runtime(directory, self.launch, status='working')
        output = self.output('focus', '--turn', agent='asker')
        self.assertIn('Focused until your next turn ends.', output)
        self.assertIn('It ends by itself', output)
        self.assertEqual(focus.current(directory)['turns'], 2)
        for status in ('idle', 'working', 'idle'):
            self.assertIn('currently focused', self.output('send', 'asker', '--text', 'ping'))
            agents.update_runtime(directory, self.launch, status=status)
        self.assertIsNone(focus.current(directory))
        self.assertIn('next pause', self.output('send', 'asker', '--text', 'ping'))
        self.assertEqual(len(senders), 1)

    def test_retry_waits_for_unfocus_but_check_does_not(self):
        """deliver --retry is refused while focused and works after unfocus, and --check is never held back; a retry
        would otherwise slip a steer past the focus."""
        self.output('focus', 'asker')
        self.assertEqual(self.output('deliver', 'asker', '--retry'), 'asker: no messages to retry.\n')
        self.output('unfocus', 'asker')
        self.output('send', 'asker', '--text', 'before')
        mailbox.update_message(self.directories['asker'], self.history('asker')[0]['id'], submission_state='failed')
        self.output('focus', 'asker', '--for', '1h')
        with patch.object(native, 'finalize_launch_only', side_effect=AssertionError('up-front focus must avoid delivery work')):
            self.assertEqual(self.output('deliver', 'asker', '--retry', code=1), '')
        self.assertTrue(self.stderr.startswith('Not retried: asker is focused until '), self.stderr)
        self.assertEqual(len(self.calls), 1)
        # --check is not held back: it reports the definite failure (exit 1) instead of the focus.
        self.output('deliver', 'asker', '--check', code=1)
        self.assertIn('Not delivered to asker', self.stderr)
        self.assertNotIn('focused', self.stderr)
        self.output('unfocus', 'asker')
        self.output('deliver', 'asker', '--retry')
        self.assertEqual(len(self.calls), 2)

    def test_a_focus_that_starts_during_a_retry_stops_it(self):
        """A focus that starts while deliver --retry works through failed messages stops it before the next one is
        submitted; a break slips steers past a focus that was already in effect."""
        directory = self.directories['asker']
        for text in ('first', 'second'):
            self.output('send', 'asker', '--text', text)
        for message in self.history('asker'):
            mailbox.update_message(directory, message['id'], submission_state='failed')
        submit = native.send_or_wake
        def focus_after_first(*arguments, **options):
            receipt = submit(*arguments, **options)
            # As if mv2 focus ran now: it waits for this retry's send lock, then starts.
            store.write(directory / focus.FILE, {'since': datetime.now(timezone.utc).isoformat(), 'until': None,
                                                 'turns': None, 'by': 'user'})
            return receipt
        self.calls.clear()
        with patch.object(native, 'send_or_wake', side_effect=focus_after_first):
            self.output('deliver', 'asker', '--retry', code=1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('The rest were not retried: asker is focused until unfocused', self.stderr)

    def test_the_dashboard_backend_reads_a_damaged_focus_as_none(self):
        """The dashboard's real backend lists an agent whose focus.json has a far-future until as not focused, and
        list and send keep working; a break crashed every refresh with "date value out of range"."""
        from multiverse.ui.backend import LocalBackend
        directory = self.directories['asker']
        store.write(directory / focus.FILE, {'since': datetime.now(timezone.utc).isoformat(),
                                             'until': '9999-12-31T23:59:59-14:00', 'turns': None, 'by': 'user'})
        rows = {row['name']: row for row in LocalBackend().snapshot(self.root)['agents']}
        self.assertIsNone(rows['asker']['focus'])
        self.assertNotIn('Focused', self.output('list'))
        self.output('send', 'asker', '--text', 'ping')

    def test_list_inspect_and_the_tmux_bar_show_focus(self):
        """list, list --json, inspect and the tmux bar show the focus and when it ends, and stop once it expires;
        senders and the user otherwise cannot tell why sends are refused."""
        self.output('focus', 'asker', '--for', '20m')
        listing = self.output('list')
        self.assertIn('  Focused until ', listing)
        self.assertIn('messages to it wait in its inbox', listing)
        self.assertEqual(listing.count('Focused'), 1)
        rows = {row['name']: row for row in json.loads(self.output('list', '--json'))}
        self.assertIsNotNone(rows['asker']['focus']['until'])
        self.assertIsNone(rows['other']['focus'])
        with patch('multiverse.tools.inspect.run', return_value=''):
            self.assertIn('Focused until ', self.output('inspect', 'asker'))
        directory = self.directories['asker']
        bar = statusline.render(self.root, directory.name, self.launch, 'claude')
        self.assertIn('asker · claude · focused 20m', bar)
        self.output('focus', 'asker')
        self.assertIn('· focused', statusline.render(self.root, directory.name, self.launch, 'claude'))
        self.output('focus', 'asker', '--for', '2h')
        self.assertIn('· focused 2h00m', statusline.render(self.root, directory.name, self.launch, 'claude'))
        store.write(directory / focus.FILE, {**focus.current(directory),
                                             'until': (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()})
        self.assertNotIn('focused', statusline.render(self.root, directory.name, self.launch, 'claude'))
        self.assertNotIn('Focused', self.output('list'))


if __name__ == '__main__':
    unittest.main()
