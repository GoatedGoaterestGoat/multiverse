"""Reported dependencies keep identity through delivery and advisory display."""
from contextlib import redirect_stdout, redirect_stderr
import io
import os
import shutil
import sys
import threading
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest
from multiverse import cli, harnesses, store
from multiverse.harnesses.types import Capability, NativeResult
from multiverse.runtime import lifecycle, native, process
from multiverse.terminal import tmux
from multiverse.tools import deliver, held, report, send
from multiverse.universe import agents, blocked, focus, mailbox, relocate, templates


class BlockedOnTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directories = {}
        for name in ('asker', 'helper', 'other', 'user', 'self'):
            directory = self.root / 'state/agents' / str(uuid4())
            self.directories[name] = directory
            store.write(directory / 'properties.json', {
                'id': directory.name, 'universe_id': store.read(self.root / 'universe.json')['id'],
                'name': name, 'phase': 'working', 'summary': '', 'harness': 'claude', 'workspace': str(self.work),
                'native_session_id': str(uuid4())})
            store.write(directory / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'running', 'status': 'idle',
                'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}})
            (directory / '.launch.lock').touch()
        self.calls = []
        self.capabilities = {}
        self.behavior = NativeResult('native_queued', 'Confirmed', value={'transcript_confirmed': True})
        owner = self
        class Adapter:
            def capabilities(self, session, executor=None):
                return owner.capabilities
            def send(self, session, message, executor):
                owner.calls.append((session.agent_id, message.text, message.delivery))
                return owner.behavior
        self.enterContext(patch.object(harnesses, 'load', return_value=Adapter()))
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))

    def environment(self, name, root=None):
        directory = self.directories[name]
        return {'MV2_UNIVERSE': str(root or self.root), 'MV2_AGENT_ID': directory.name, 'MV2_AGENT_DIR': str(directory),
                'MV2_LAUNCH_ID': store.read(directory / 'runtime.json')['launch_id']}

    def output(self, command, *arguments, code=0):
        out, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(errors), patch.object(sys, 'argv', [
                'mv2', '-u', str(self.root), command, *arguments]):
            try:
                cli.main()
            except SystemExit as error:
                self.assertEqual(error.code, code, errors.getvalue())
            else:
                self.assertEqual(code, 0, out.getvalue())
        return out.getvalue() + errors.getvalue()

    def report(self, target='helper', **options):
        return report.run(self.root, agent='asker', phase='blocked', blocked_on=target, **options)

    def test_dependencies_resolve_before_writing_and_clear_with_a_new_phase(self):
        """A stale dependency must neither survive a new phase nor switch to a reused name or ambiguous special source."""
        for reference, kind, name in (('user', 'user', None), ('self', 'self', None),
                                      ('agent:user', 'agent', 'user'), ('agent:self', 'agent', 'self'),
                                      ('asker', 'self', None), (' helper ', 'agent', 'helper'),
                                      (self.directories['helper'].name[:12], 'agent', 'helper')):
            with self.subTest(reference=reference):
                result = self.report(reference)
                self.assertEqual(result['blocked_on']['kind'], kind)
                if name:
                    self.assertEqual(result['blocked_on']['id'], self.directories[name].name)
        old = (self.directories['asker'] / 'properties.json').read_bytes()
        for options in ({'blocked_on': 'absent'}, {'blocked_on': 'agent:'}, {'blocked_on': 'helper,other'},
                        {'blocked_on': 'user', 'blocked_on_universe': str(self.root)},
                        {'phase': 'working', 'blocked_on': 'helper'},
                        {'blocked_on_universe': str(self.root)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                report.run(self.root, agent='asker', **{'phase': 'blocked', **options})
            self.assertEqual((self.directories['asker'] / 'properties.json').read_bytes(), old)
        self.assertEqual(report.run(self.root, agent='asker', summary='still waiting')['blocked_on']['kind'], 'agent')
        for phase in ('blocked', 'working', 'done', 'starting'):
            self.report()
            self.assertNotIn('blocked_on', report.run(self.root, agent='asker', phase=phase))
        other_root = templates.create(self.root / 'subverses/qa', 'qa', workdir=self.work)
        with patch.object(relocate, 'observe', return_value={'live': 'dormant', 'pane': None}):
            relocate.move_agent(self.root, 'helper', other_root)
        result = self.report('helper', blocked_on_universe='qa')
        self.assertEqual(result['blocked_on']['universe'], str(other_root))
        with patch.object(lifecycle, '_listing_observation', side_effect=lambda p, f, s: (f, {'live': 'dormant'})):
            rows = lifecycle.listing(self.root, usage=False)
        shown = next(row for row in rows if row['name'] == 'asker')['blocked_on']
        self.assertEqual(shown, {'kind': 'agent', 'id': result['blocked_on']['id'],
                                'name': 'helper', 'universe': 'qa', 'gone': False})
        moved = other_root / 'state/agents' / result['blocked_on']['id']
        properties = store.read(moved / 'properties.json')
        store.write(moved / 'properties.json', {**properties, 'name': 'renamed'})
        self.assertEqual(blocked.display(self.root, result)['name'], 'renamed')
        shutil.rmtree(moved)
        # A new helper cannot inherit the old dependency.
        store.write(other_root / 'state/agents' / str(uuid4()) / 'properties.json',
                    {**properties, 'id': str(uuid4())})
        self.assertTrue(blocked.display(self.root, result)['gone'])
        self.assertEqual(blocked.display(self.root, result)['name'], 'helper')
        for bad in ([], {'kind': 'agent'}, {'kind': 'agent', 'id': '../bad', 'name': 'bad', 'universe': str(self.root)}):
            self.assertIsNone(blocked.display(self.root, {'phase': 'blocked', 'blocked_on': bad}))

    def test_report_advice_never_waits_or_consumes_and_matches_sender_ids(self):
        """Reporting blocked must not hide a waiting reply, consume it, or wait behind an inbox reader or damaged file."""
        directory = self.directories['asker']
        source = {'agent_id': self.directories['helper'].name, 'name': 'before rename', 'universe': str(self.root)}
        mailbox.append_message(directory, 'read already', source)
        mailbox.inbox(directory)
        mailbox.append_message(directory, 'not manual', source, delivery='follow-up')
        mailbox.append_message(directory, 'name alone is not identity', {'name': 'helper'})
        with patch.dict(os.environ, self.environment('asker')):
            line = self.output('report', '--phase', 'blocked', '--blocked-on', 'helper')
            self.assertNotIn('Reminder:', line)
            mailbox.append_message(directory, 'reply', source)
            files = lambda: {p.name: p.read_bytes() for p in directory.glob('inbox*') if p.is_file()}
            before = files()
            line = self.output('report', '--phase', 'blocked', '--blocked-on', 'helper')
            self.assertTrue(line.strip().endswith('Reminder: you have unread messages from helper.'), line)
            self.assertEqual(files(), before)
            with patch.dict(os.environ):
                for key in ('MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID'):
                    os.environ.pop(key, None)
                operator = self.output('report', '--agent', 'asker', '--phase', 'blocked', '--blocked-on', 'helper')
                self.assertNotIn('Reminder:', operator)
            self.assertEqual(files(), before)
            with store.lock(directory / '.inbox.lock'):
                line = self.output('report', '--phase', 'blocked', '--blocked-on', 'helper')
                self.assertNotIn('Reminder:', line)
            (directory / 'inbox.cursor').write_text('bad')
            self.assertNotIn('Reminder:', self.output('report', '--phase', 'blocked', '--blocked-on', 'helper'))
            (directory / 'inbox.cursor').unlink()
            os.mkfifo(directory / 'inbox.cursor')
            self.assertNotIn('Reminder:', self.output('report', '--phase', 'blocked', '--blocked-on', 'helper'))
            (directory / 'inbox.cursor').unlink()

    def test_only_the_named_agents_inbox_messages_upgrade_and_focus_still_holds_them(self):
        """A targeted wake must happen once, keep both notices, and take the normal held-message path during focus."""
        self.report()
        with patch.dict(os.environ, self.environment('helper')):
            output = self.output('send', 'asker,other,asker', '--delivery', 'inbox-only', '--text', 'reply')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], self.directories['asker'].name)
        self.assertEqual(self.calls[0][2], 'follow-up')
        note = 'This message was delivered as a follow-up because your status is blocked on helper.'
        self.assertIn(note + '\n\nreply', self.calls[0][1])
        self.assertEqual(self.calls[0][1].count(note), 1)
        self.assertTrue(output.startswith("Your message was delivered as a follow-up instead because asker's"))
        self.assertIn("Saved to other's inbox", output)
        for source in ({'name': 'helper'}, {'agent_id': self.directories['other'].name, 'name': 'helper'},
                       {'plugin': 'helper'}, {'core': True, 'agent_id': self.directories['helper'].name}):
            with self.subTest(source=source):
                result = send.run(self.root, agent='asker', text='unchanged', sender=source, delivery='inbox-only')
                self.assertEqual(result['delivery'], 'inbox-only')
                self.assertNotIn('upgraded_from', result)
        with patch.dict(os.environ, self.environment('helper')):
            for delivery in ('steer', 'follow-up'):
                result = send.run(self.root, agent='asker', text='explicit', delivery=delivery)
                self.assertEqual(result['delivery'], delivery)
                self.assertNotIn('upgraded_from', result)
                self.assertEqual(result['text'], 'explicit')
        # The source moves and is renamed; a new agent with its old name must not inherit the upgrade.
        destination = templates.create(self.root / 'subverses/moved', 'moved', workdir=self.work)
        with patch.object(relocate, 'observe', return_value={'live': 'dormant', 'pane': None}):
            relocate.move_agent(self.root, 'helper', destination)
        moved = destination / 'state/agents' / self.directories['helper'].name
        self.directories['helper'] = moved
        props = store.read(moved / 'properties.json')
        store.write(moved / 'properties.json', {**props, 'name': 'renamed'})
        with patch.dict(os.environ, self.environment('helper', destination)):
            self.behavior = NativeResult('error', 'Not submitted')
            output = self.output('send', 'asker', '--queue-only', '--text', 'failed but saved')
            self.behavior = NativeResult('native_queued', 'Confirmed', value={'transcript_confirmed': True})
        self.assertTrue(output.startswith("Tried a follow-up because"), output)
        self.assertIn("Saved to asker's inbox", output)
        self.assertEqual(self.calls[-1][2], 'follow-up')
        self.assertIn('blocked on renamed.', self.calls[-1][1])
        note = 'This message was delivered as a follow-up because your status is blocked on renamed.'
        directory = self.directories['asker']
        focused = focus.start(directory)
        before = len(self.calls)
        with patch.dict(os.environ, self.environment('helper', destination)):
            output = self.output('send', 'asker', '--delivery', 'inbox-only', '--text', 'during focus')
        self.assertEqual(len(self.calls), before)
        self.assertLess(output.index('scheduled as a follow-up instead'), output.index('currently focused'))
        waiting = mailbox.held(directory, focused['id'], peek=True)['messages']
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]['delivery'], 'inbox-only')
        self.assertEqual(waiting[0]['intended'], 'follow-up')
        self.assertEqual(waiting[0]['text'],
                         'This message was sent as a follow-up because your status is blocked on renamed; '
                         'it was held in your inbox while you were focused.\n\nduring focus')
        self.assertEqual(focus.last(directory)['held'], 1)
        focus.end(directory)
        with patch.dict(os.environ, self.environment('asker')):
            self.assertEqual(len(held.run(self.root)['messages']), 1)
            self.assertEqual(held.run(self.root)['messages'], [])
        self.assertEqual(len(self.calls), before)  # Reading held input never replays it natively.
        report.run(self.root, agent='asker', phase='working')
        with patch.dict(os.environ, self.environment('helper', destination)):
            result = send.run(self.root, agent='asker', text='ordinary again', delivery='inbox-only')
        self.assertNotIn('upgraded_from', result)
        self.assertEqual(len(self.calls), before)

    def test_unavailable_upgrade_keeps_manual_input_while_wake_and_focus_still_work(self):
        """An automatic upgrade must not strand inbox input in a final native refusal on a launch-only recipient."""
        self.report()
        directory = self.directories['asker']
        for live, wake, focused, expected in (
                ('alive', False, False, 'inbox-only'), ('alive', True, False, 'inbox-only'),
                ('stopped', False, False, 'inbox-only'), ('stopped', True, False, 'follow-up'),
                ('alive', False, True, 'held')):
            with self.subTest(live=live, wake=wake, focused=focused):
                self.capabilities = {'send_follow_up': Capability('unavailable', 'Launch-only', mechanism='launch-only'),
                                     'wake': Capability('supported' if wake else 'unavailable')}
                if focused:
                    focus.start(directory)
                with patch.object(native, 'observe', return_value={'live': live}), \
                        patch.object(native, 'send_or_wake', return_value={'state': 'native_queued', 'ok': True}) as submit, \
                        patch.dict(os.environ, self.environment('helper')):
                    output = self.output('send', 'asker', '--delivery', 'inbox-only', '--text', 'keep this reply')
                message = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
                if expected == 'inbox-only':
                    self.assertEqual(message['delivery'], 'inbox-only')
                    self.assertEqual(message['text'], 'keep this reply')
                    self.assertIn('cannot take a follow-up', output)
                    self.assertNotIn('resend', output)
                    self.assertTrue(any(m['id'] == message['id'] for m in mailbox.inbox(directory, peek=True)['messages']))
                    submit.assert_not_called()
                elif expected == 'follow-up':
                    self.assertEqual(message['delivery'], 'follow-up')
                    submit.assert_called_once()
                else:
                    self.assertEqual(message['delivery'], 'inbox-only')
                    self.assertEqual(message.get('intended'), 'follow-up')
                    submit.assert_not_called()
                    focus.end(directory)
        self.capabilities = {}
        # Valid JSON can still contain unusable terminal metadata. Exercise the real observation
        # path: optional classification must not lose input before the mailbox append.
        facts = store.read(directory / 'runtime.json')
        for terminal in ('damaged but valid JSON', [], {'pane': 7, 'socket': 5}):
            with self.subTest(terminal=terminal):
                store.write(directory / 'runtime.json', {**facts, 'tmux': terminal})
                before = len(mailbox.inbox(directory, all_messages=True, limit=None)['messages'])
                failure = None
                with patch.object(native, 'observe', side_effect=tmux.observe), \
                        patch.object(native, 'send_or_wake', return_value={'state': 'native_queued', 'ok': True}) as submit, \
                        patch.dict(os.environ, self.environment('helper')):
                    try:
                        result = send.run(self.root, agent='asker', text='save before optional observation',
                                          delivery='inbox-only')
                    except Exception as error:
                        failure = type(error).__name__
                history = mailbox.inbox(directory, all_messages=True, limit=None)['messages']
                self.assertEqual(len(history), before + 1, failure)
                self.assertIsNone(failure)
                self.assertEqual(result['delivery'], 'inbox-only')

                self.assertEqual(history[-1]['text'], 'save before optional observation')
                self.assertTrue(any(message['id'] == history[-1]['id']
                                    for message in mailbox.inbox(directory, peek=True)['messages']))
                submit.assert_not_called()
        for label, observation in (('unknown', {'live': 'unknown'}),
                                   ('unreadable server', OSError('fixture server cannot be read')),
                                   ('owned child still running', {'live': 'dormant'})):
            with self.subTest(observation=label):
                current = facts
                if label == 'owned child still running':
                    token = process.process_started(os.getpid())
                    self.assertIsNotNone(token)
                    current = {**facts, 'pid': os.getpid(), 'pid_started': token}
                store.write(directory / 'runtime.json', current)
                with patch.object(native, 'observe', return_value=observation,
                                  side_effect=observation if isinstance(observation, Exception) else None), \
                        patch.object(native, 'send_or_wake', return_value={'state': 'native_queued', 'ok': True}) as submit, \
                        patch.dict(os.environ, self.environment('helper')):
                    failure = None
                    try:
                        result = send.run(self.root, agent='asker', text='unproven liveness', delivery='inbox-only')
                    except Exception as error:
                        failure = type(error).__name__
                    self.assertIsNone(failure)
                    self.assertEqual(result['delivery'], 'inbox-only')
                    submit.assert_not_called()

    def test_unconfirmed_upgrades_keep_inbox_input_without_a_second_native_attempt(self):
        """An optional upgrade must keep unconfirmed input readable without replaying native attempts or forgetting reads."""
        self.report()
        directory = self.directories['asker']
        for label, behavior in (
                ('refused', NativeResult('error', 'socket refused')),
                ('unavailable', NativeResult('unavailable', 'bridge unavailable')),
                ('launch-only', NativeResult('unavailable', 'plain now', value={'launch_only': True, 'final': True})),
                ('unknown', NativeResult('unknown', 'reply lost')),
                ('accepted', NativeResult('native_queued', 'Accepted')),
                ('confirmed', NativeResult('native_queued', 'Confirmed', value={'transcript_confirmed': True}))):
            with self.subTest(outcome=label):
                self.behavior = behavior
                original = f'original reply {label}'
                before = len(self.calls)
                with patch.dict(os.environ, self.environment('helper')):
                    output = self.output('send', 'asker', '--delivery', 'inbox-only', '--text', original)
                history = mailbox.inbox(directory, all_messages=True, limit=None)['messages']
                message = history[-1]
                self.assertEqual(len(self.calls), before + 1)
                copies = mailbox.inbox(directory, peek=True)['messages']
                if label == 'confirmed':
                    self.assertFalse(copies)
                    self.assertTrue(output.startswith('Your message was delivered'), output)
                else:
                    self.assertTrue(output.startswith('Tried a follow-up because'), output)
                    self.assertIn("Saved to asker's inbox", output)
                    self.assertNotIn('delivered as a follow-up', output)
                    self.assertNotIn('--retry', output)
                    saved = next(m for m in copies if m['id'] == message['id'])
                    self.assertEqual(saved['delivery'], 'inbox-only')
                    self.assertTrue(saved['text'].endswith(original))
                    if label in ('unknown', 'accepted'):
                        self.assertIn('may also have arrived as a follow-up', saved['text'])
                        self.assertIn('Delivery is not confirmed', output)
                        self.assertFalse(message['tracking'].get('dismissed_at'))
                    else:
                        self.assertEqual(saved['text'], original)
                        self.assertTrue(message['tracking'].get('dismissed_at'))
                    self.assertEqual(mailbox.inbox(directory)['messages'][-1]['text'], saved['text'])
                    self.assertEqual(mailbox.inbox(directory)['messages'], [])
                deliver.run(self.root, agent='asker', retry=True)
                with store.lock(directory / '.send.lock'):
                    native.send_or_wake(self.root, 'asker', message['id'], retry=True)
                self.assertEqual(len(self.calls), before + 1)
                self.assertEqual(len(mailbox.inbox(directory, all_messages=True, limit=None)['messages']), len(history))

        # A manual result needs no native advice read after a transport failure changed its runtime.
        with self.subTest('runtime damaged after admission'):
            owner = self
            facts = store.read(directory / 'runtime.json')
            class RefusingAdapter:
                def capabilities(self, session, executor=None):
                    return {}
                def send(self, session, message, executor):
                    owner.calls.append((session.agent_id, message.text, message.delivery))
                    store.write(directory / 'runtime.json', {**facts, 'tmux': 'damaged after admission'})
                    return NativeResult('error', 'socket refused')
            def observe(properties, facts):
                return {'live': 'alive'} if isinstance(facts.get('tmux'), dict) else tmux.observe(properties, facts)
            failure = None
            with patch.object(harnesses, 'load', return_value=RefusingAdapter()), \
                    patch.object(native, 'observe', side_effect=observe), \
                    patch.dict(os.environ, self.environment('helper')):
                try:
                    output = self.output('send', 'asker', '--delivery', 'inbox-only', '--text', 'read me')
                except Exception as error:
                    failure = type(error).__name__
            store.write(directory / 'runtime.json', facts)
            self.assertIsNone(failure)
            self.assertIn("Saved to asker's inbox", output)
            self.assertEqual([m['text'] for m in mailbox.inbox(directory)['messages']], ['read me'])

        # A wake's error is not negative proof after its child may have received the prompt.
        self.capabilities = {'wake': Capability('supported')}
        for recorded in (None, 'accepted', 'submitting', 'unknown'):
            with self.subTest(wake_recorded=recorded):
                def failed_start(root, *, resume_message_id, **options):
                    if recorded:
                        mailbox.update_message(directory, resume_message_id, submission_state=recorded,
                                               attempts=[{'state': 'ok' if recorded == 'accepted' else 'unknown'}])
                    raise RuntimeError('wake stopped')
                before = len(self.calls)
                with patch.object(native, 'observe', return_value={'live': 'stopped'}), \
                        patch.object(lifecycle, 'start', side_effect=failed_start), \
                        patch.dict(os.environ, self.environment('helper')):
                    output = self.output('send', 'asker', '--delivery', 'inbox-only', '--text', 'wake reply')
                message = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
                self.assertTrue(output.startswith('Tried a follow-up because'), output)
                self.assertEqual(bool(message['tracking'].get('inbox_fallback_at')), recorded is None)
                if recorded:
                    self.assertIn('Delivery is not confirmed', output)
                    self.assertNotIn('It was not submitted', output)
                    self.assertNotIn('Not delivered', output)
                    self.assertNotIn('--retry', output)
                readable = mailbox.inbox(directory)['messages']
                self.assertEqual(len(readable), 1)
                self.assertTrue(readable[0]['text'].endswith('wake reply'))
                self.assertEqual('may also have arrived' in readable[0]['text'], bool(recorded))
                self.assertEqual(len(self.calls), before)
                deliver.run(self.root, agent='asker', retry=True)
                self.assertEqual(len(self.calls), before)
        self.capabilities = {}

        # A known preflight refusal saves manual input before any native attempt or retry reservation.
        before = len(self.calls)
        with patch.object(mailbox, 'preflight_delivery', side_effect=ValueError('damaged ledger')), \
                patch.dict(os.environ, self.environment('helper')):
            result = send.run(self.root, agent='asker', delivery='inbox-only', text='preflight reply')
        self.assertEqual(result['delivery'], 'inbox-only')
        self.assertEqual([m['text'] for m in mailbox.inbox(directory)['messages']], ['preflight reply'])
        self.assertEqual(len(self.calls), before)

        # Pending input is readable too. Consuming it cannot reserve a second native submission,
        # and a later startup failure must not bring the consumed inbox copy back.
        facts = store.read(directory / 'runtime.json')
        store.write(directory / 'runtime.json', {**facts, 'startup_delivery': 'waiting'})
        with patch.dict(os.environ, self.environment('helper')):
            output = self.output('send', 'asker', '--delivery', 'inbox-only', '--text', 'late startup failure')
        self.assertIn('scheduled as a follow-up', output)
        self.assertIn('Delivery is not confirmed', output)
        self.assertNotIn('delivered as a follow-up', output)
        pending = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
        self.assertEqual(pending['tracking']['submission_state'], 'pending')
        for _ in range(2):
            self.assertTrue(mailbox.inbox(directory, peek=True)['messages'][0]['text'].endswith('late startup failure'))
        with self.assertRaises(BrokenPipeError):
            mailbox.inbox(directory, present=lambda result: (_ for _ in ()).throw(BrokenPipeError()))
        self.assertEqual(len(mailbox.inbox(directory)['messages']), 1)
        old_cursor = (directory / 'inbox.cursor').read_bytes()
        with store.lock(directory / '.launch.lock'):
            self.assertEqual(mailbox.end_startup_messages(directory, facts['launch_id'], 'startup failed'), 1)
        self.assertEqual(mailbox.inbox(directory)['messages'], [])
        self.assertEqual((directory / 'inbox.cursor').read_bytes(), old_cursor)
        before = len(self.calls)
        deliver.run(self.root, agent='asker', retry=True)
        with store.lock(directory / '.send.lock'):
            native.send_or_wake(self.root, 'asker', pending['id'], retry=True)
        self.assertEqual(len(self.calls), before)

        # Reading a pending copy leaves the one queued native submission intact.
        with patch.dict(os.environ, self.environment('helper')):
            result = send.run(self.root, agent='asker', delivery='inbox-only', text='queued then read')
        queued = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
        self.assertEqual(queued['tracking']['submission_state'], 'pending')
        self.assertFalse(queued['tracking'].get('dismissed_at'))
        self.assertTrue(mailbox.inbox(directory)['messages'][0]['text'].endswith('queued then read'))
        store.write(directory / 'runtime.json', facts)
        self.behavior = NativeResult('native_queued', 'Accepted')
        before = len(self.calls)
        for _ in range(2):
            native.deliver(self.root, directory.name, queued['id'])
        self.assertEqual(len(self.calls), before + 1)
        self.assertEqual(mailbox.inbox(directory)['messages'], [])

        # Live confirmation suppresses a copy; later proved loss without confirmation restores it
        # even when the cursor passed it. The original native identity/receipt remain in history.
        store.write(directory / 'runtime.json', facts)
        self.behavior = NativeResult('native_queued', 'Accepted')
        for eventual in ('confirmed', 'confirmed stamp', 'lost'):
            with self.subTest(eventual=eventual), patch.dict(os.environ, self.environment('helper')):
                send.run(self.root, agent='asker', delivery='inbox-only', text='eventual ' + eventual)
                message = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
                self.assertEqual(len(mailbox.inbox(directory, peek=True)['messages']), 1)
                if eventual.startswith('confirmed'):
                    proof = ({'transcript_confirmed_at': store.now()} if eventual == 'confirmed stamp'
                             else {'native_receipt': {'transcript_confirmed': True}})
                    mailbox.update_message(directory, message['id'], **proof)
                    self.assertEqual(mailbox.inbox(directory)['messages'], [])
                else:
                    # This is a valid older read position: an operator may have read with older mv2
                    # code before this optional-upgrade policy, or a prior confirmed receipt was corrected.
                    store.write(directory / 'inbox.cursor', (directory / 'inbox.jsonl').stat().st_size)
                    mailbox.update_message(directory, message['id'], submission_state='cancelled', final_reason='lost',
                                           native_receipt={'lost_when_stopped': True})
                    self.assertEqual([m['text'] for m in mailbox.inbox(directory)['messages']], ['eventual lost'])
                    self.assertEqual(mailbox.inbox(directory)['messages'], [])
                after = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
                self.assertEqual((after['id'], after['delivery']), (message['id'], 'follow-up'))

        # Reading while submission is in flight neither breaks an empty ledger nor cancels its only
        # native attempt. If that attempt later proves accepted, the read copy does not reappear.
        fresh = self.directories['user']
        tagged = mailbox.append_message(fresh, 'native prefix\n\nread once', {'name': 'helper'},
                                        delivery='follow-up', inbox_only_text='read once')
        self.assertEqual(len(mailbox.inbox(fresh)['messages']), 1)
        mailbox.update_message(fresh, tagged['id'], submission_state='accepted')
        self.assertEqual(mailbox.inbox(fresh)['messages'], [])
        self.assertFalse(mailbox.message_record(fresh, tagged['id'])[1].get('dismissed_at'))

        # The optional fallback must not change explicit follow-up failures.
        store.write(directory / 'runtime.json', facts)
        self.behavior = NativeResult('error', 'explicit refusal')
        for delivery in ('steer', 'follow-up'):
            with self.subTest(explicit=delivery), patch.dict(os.environ, self.environment('helper')):
                result = send.run(self.root, agent='asker', delivery=delivery, text='explicit')
                self.assertFalse(result['ok'])
                explicit = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
                self.assertFalse(explicit['tracking'].get('dismissed_at'))
                self.assertEqual(mailbox.inbox(directory)['messages'], [])

    def test_fallback_stays_final_after_internal_refresh_or_an_old_runner_write(self):
        """A retry can itself create a final manual fallback after its entry check, and old runners lack its new marker."""
        self.report()
        directory = self.directories['asker']
        facts = store.read(directory / 'runtime.json')
        for old_runner in (False, True):
            with self.subTest(old_runner=old_runner):
                store.write(directory / 'runtime.json', {**facts, 'startup_delivery': 'waiting'})
                with patch.dict(os.environ, self.environment('helper')):
                    send.run(self.root, agent='asker', delivery='inbox-only', text='deferred reply')
                message = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
                self.assertEqual(message['tracking']['submission_state'], 'pending')
                if old_runner:
                    # An already-running pre-update runner writes a real failed receipt, without the new
                    # dismissal fields. The durable upgrade tag still prevents a native retry.
                    states = store.read(directory / 'inbox-state.json')
                    states[message['id']].update(submission_state='failed', attempts=[{'state': 'error', 'reason': 'startup failed'}])
                    store.write(directory / 'inbox-state.json', states)
                store.write(directory / 'runtime.json', {**facts, 'startup_delivery': 'failed'})
                before = len(self.calls)
                with store.lock(directory / '.send.lock'):
                    native.send_or_wake(self.root, 'asker', message['id'], retry=True)
                self.assertEqual(len(self.calls), before)
                self.assertEqual([m['text'] for m in mailbox.inbox(directory)['messages']], ['deferred reply'])
                self.assertTrue(mailbox.message_record(directory, message['id'])[1].get('dismissed_at'))

        # A receipt recheck can finalize input too. Both running retries and wake retries must honor
        # the refreshed dismissal, rather than their older eligible snapshot.
        store.write(directory / 'runtime.json', facts)
        self.capabilities = {'wake': Capability('supported')}
        for live in ('alive', 'stopped'):
            with self.subTest(receipt_refresh=live):
                message = mailbox.append_message(directory, 'explicit receipt', {}, delivery='follow-up')
                receipt = {'state': 'error', 'value': {'definite_failure': True, 'delivery_rejected': True}}
                mailbox.update_message(directory, message['id'], submission_state='failed', last_receipt=receipt)
                def recheck(*args):
                    mailbox.update_message(directory, message['id'], dismissed_at=store.now())
                    return receipt
                before = len(self.calls)
                with patch.object(native, 'observe', return_value={'live': live}), \
                        patch.object(native, '_check_delivery_receipt', side_effect=recheck), \
                        patch.object(lifecycle, 'start') as wake, store.lock(directory / '.send.lock'):
                    native.send_or_wake(self.root, 'asker', message['id'], retry=True)
                self.assertEqual(len(self.calls), before)
                wake.assert_not_called()

    def test_damaged_delivery_evidence_cannot_hide_input_or_its_repair_diagnostic(self):
        """Malformed proof must not suppress inbox input, and a torn ledger must stay an actionable send error."""
        self.report()
        directory = self.directories['asker']
        self.behavior = NativeResult('unknown', 'unconfirmed')
        with patch.dict(os.environ, self.environment('helper')):
            send.run(self.root, agent='asker', delivery='inbox-only', text='repairable reply')
        message = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
        initial = store.read(directory / 'inbox-state.json')
        for field in ('transcript_confirmed_at', 'inbox_copy_read_at'):
            for damaged in ({'damaged': True}, True, 1, 'not a date', '2026-11-07', '2026-11-07T12:00:00'):
                with self.subTest(field=field, damaged=damaged):
                    store.write(directory / 'inbox-state.json', {
                        **initial, message['id']: {**initial[message['id']], field: damaged}})
                    self.assertEqual(len(mailbox.inbox(directory, peek=True)['messages']), 1)
        store.write(directory / 'inbox-state.json', initial)
        mailbox.inbox(directory)
        class TearingAdapter:
            def capabilities(self, session, executor=None):
                return {}
            def send(self, session, message, executor):
                (directory / 'inbox-state.json').write_text('{')
                return NativeResult('unknown', 'reply lost')
        with patch.object(harnesses, 'load', return_value=TearingAdapter()), \
                patch.dict(os.environ, self.environment('helper')):
            output = self.output('send', 'asker', '--delivery', 'inbox-only', '--text', 'keep this original', code=1)
        self.assertIn('inbox-state.json', output)
        self.assertIn('Repair', output)
        self.assertNotIn('it sees it at its next check', output)
        self.assertNotIn('delivered as a follow-up', output)
        with self.assertRaises(ValueError):
            mailbox.inbox(directory, peek=True)
        # The failed diagnostic did not lose the original or try native submission again.
        (directory / 'inbox-state.json').unlink()
        repaired = mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]
        self.assertEqual(repaired.get('inbox_only_text'), 'keep this original')

    def test_reports_and_upgrades_share_the_send_fence(self):
        """A phase clear cannot finish while an already admitted upgrade is still being saved and delivered."""
        directory = self.directories['asker']
        self.report()
        started, done = threading.Event(), threading.Event()
        failures = []
        def clear():
            started.set()
            try:
                report.run(self.root, agent='asker', phase='working')
            except Exception as error:
                failures.append(error)
            finally:
                done.set()
        with store.lock(directory / '.send.lock'):
            worker = threading.Thread(target=clear)
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(done.wait(.1))
            self.assertIsNotNone(blocked.current(store.read(directory / 'properties.json')))
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertIsNone(blocked.current(store.read(directory / 'properties.json')))
