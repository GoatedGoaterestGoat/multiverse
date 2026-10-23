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
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest
from multiverse import cli, harnesses, store
from multiverse.harnesses.types import AgentIdentitySpec, NativeEndpoint, NativeResult, NativeServiceHandle, ServiceIdentity
from multiverse.runtime import agent_identity, native
from multiverse.terminal import statusline
from multiverse.tools import focus as focus_tool, unfocus
from multiverse.universe import agents, focus, mailbox, templates


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
                descriptors = len(os.listdir('/dev/fd'))
                for _ in range(20):
                    self.assertIsNone(focus.current(self.directory))   # a FIFO must not block
                    self.assertEqual(statusline.focus_text(self.directory), '')
                self.assertEqual(len(os.listdir('/dev/fd')), descriptors)   # every read closes what it opened
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

    def test_steer_and_follow_up_to_a_focused_agent_are_refused_and_not_saved(self):
        """Steer and follow-up to a focused agent are refused and not saved, inbox-only still arrives, and other
        recipients are not held up; this is the feature, and a silent save would bury the question it protects."""
        self.output('focus', 'asker', '--for', '20m')
        for delivery in ('steer', 'follow-up'):
            with self.subTest(delivery=delivery):
                self.output('send', 'asker', '--text', 'start the benchmark', '--delivery', delivery,
                            agent='helper', code=1)
                self.assertTrue(self.stderr.startswith('Not sent: asker is focused until '), self.stderr)
                self.assertIn('--delivery inbox-only', self.stderr)
        record = json.loads(self.output('send', 'asker', '--text', 'start the benchmark', '--json', code=1))
        self.assertEqual((record['state'], record['saved'], record['ok']), ('focused', False, False))
        self.assertIn('asker is focused', record['reason'])
        self.assertIsNotNone(record['focus']['until'])
        self.assertEqual(self.history('asker'), [])
        self.assertEqual(self.calls, [])
        # Inbox-only still arrives, and other recipients of a batch are not held up.
        output = self.output('send', 'asker,other', '--text', 'when you are free', agent='helper', code=1)
        self.assertEqual(output, 'Sent to other; it reads this at its next pause.\n')
        self.assertTrue(self.stderr.startswith('Not sent: asker is focused'), self.stderr)
        self.assertEqual([agent for agent, _ in self.calls], [self.directories['other'].name])
        self.output('send', 'asker', '--text', 'when you are free', '--delivery', 'inbox-only', agent='helper')
        self.output('send', 'asker', '--text', 'also this', '--queue-only', agent='helper')
        self.assertEqual([m['delivery'] for m in self.history('asker')], ['inbox-only', 'inbox-only'])

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

    def test_the_refusal_says_who_focused_the_agent(self):
        """A refusal says the agent needs the user only when the agent focused itself; otherwise a sender is told the
        agent waits on the user when the person focused it."""
        self.output('focus', 'asker')
        self.output('send', 'asker', '--text', 'ping', code=1)
        self.assertIn('asker is focused until unfocused by the user, so it takes only inbox-only', self.stderr)
        self.output('focus', agent='asker')
        self.output('send', 'asker', '--text', 'ping', code=1)
        self.assertIn("because it needs the user's attention", self.stderr)

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
        """focus --turn is refused while the harness reports no status, and once accepted it refuses sends until the
        turn ends; otherwise a turn focus would never end."""
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
            self.output('send', 'asker', '--text', 'ping', code=1)
            agents.update_runtime(directory, self.launch, status=status)
        self.assertIsNone(focus.current(directory))
        self.output('send', 'asker', '--text', 'ping')

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
        self.assertIn('send it only --delivery inbox-only messages', listing)
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
