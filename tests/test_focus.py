"""Focus: a focused agent refuses steer and follow-up messages and takes inbox-only ones."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import io
import json
import os
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest
from multiverse import cli, harnesses, store
from multiverse.harnesses.types import AgentIdentitySpec, NativeEndpoint, NativeResult, NativeServiceHandle, ServiceIdentity
from multiverse.runtime import agent_identity, native
from multiverse.terminal import statusline
from multiverse.tools import focus as focus_tool, unfocus
from multiverse.universe import agents, focus, mailbox


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

    def test_unreadable_focus_never_blocks(self):
        """A damaged focus.json counts as no focus; otherwise a bad file would refuse every steer to that agent with
        no way to see why."""
        (self.directory / focus.FILE).write_text('{not json')
        self.assertIsNone(focus.current(self.directory))
        store.write(self.directory / focus.FILE, {'since': 'earlier', 'until': 'soon'})
        self.assertIsNone(focus.current(self.directory))

    def test_turn_focus_set_mid_turn_lasts_through_the_next_turn(self):
        """A turn focus started mid-turn outlasts that turn and ends with the next one, and only a working or waiting
        to idle change counts; a break ends the focus before the user answers."""
        focus.start(self.directory, turn=True, status='working')
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
        """A turn focus started while idle or waiting ends after exactly one turn; a break holds messages back for a
        turn longer, or none."""
        for status in ('idle', 'waiting'):
            with self.subTest(status=status):
                self.status(status)
                focus.start(self.directory, turn=True, status=status)
                if status == 'idle':
                    self.status('working')
                self.assertIsNotNone(focus.current(self.directory))
                self.status('idle')
                self.assertIsNone(focus.current(self.directory))

    def test_a_relaunch_is_not_a_turn_end_and_timed_focus_ignores_turns(self):
        """A relaunch (unknown then idle) is not a turn end, and a timed focus ignores turns; either break ends a
        focus early."""
        focus.start(self.directory, turn=True, status='idle')
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
        output = self.output('deliver', 'asker', '--retry', code=1)
        self.assertTrue(output.startswith('Not retried: asker is focused until '), output)
        self.assertEqual(len(self.calls), 1)
        # --check is not held back: it reports the definite failure (exit 1) instead of the focus.
        self.output('deliver', 'asker', '--check', code=1)
        self.assertIn('Not delivered to asker', self.stderr)
        self.assertNotIn('focused', self.stderr)
        self.output('unfocus', 'asker')
        self.output('deliver', 'asker', '--retry')
        self.assertEqual(len(self.calls), 2)

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
