"""Exact-launch session changes, durable lineage, and conversation ownership."""
import concurrent.futures
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from multiverse.universe import agents, sessions, templates, relocate
from multiverse.store import read, write
from test_multiverse import BaseTest


def compete(arguments):
    root, identity, previous, target, home = arguments
    try:
        agents.rebind_session(Path(root), identity, previous, target, 'detected', 'launch', home=home)
        return 'ok'
    except ValueError as error:
        return str(error)


class SessionLineageTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.home = str(self.work / 'native-home')
        self.agent, self.old = self.make_agent('one')

    def make_agent(self, name, root=None, native_id=None, home=None):
        root = root or self.root
        identity, native_id = str(uuid4()), native_id or str(uuid4())
        directory = root / 'state' / 'agents' / identity
        write(directory / 'properties.json', {'id': identity, 'name': name, 'harness': 'pi',
              'workspace': str(self.work), 'native_session_id': native_id,
              'native_session_provider': 'fixture', 'native_session_home': home or self.home})
        write(directory / 'runtime.json', {'launch_id': 'launch', 'state': 'running'})
        return directory, native_id

    def change(self, target, **kwargs):
        return agents.rebind_session(self.root, self.agent.name, self.old, target, 'clear', 'launch',
                                     home=self.home, **kwargs)

    def rows(self):
        return [json.loads(line) for line in (self.agent / 'sessions.jsonl').read_text().splitlines()]

    def test_change_is_journaled_once_and_repeat_is_a_zero_write_noop(self):
        target = str(uuid4())
        result = self.change(target.upper())
        self.assertEqual(result['native_session_id'], target)
        row, = self.rows()
        self.assertEqual(set(row), {'schema', 'id', 'at', 'harness', 'home', 'launch', 'from', 'to', 'reason'})
        self.assertEqual((row['from'], row['to'], row['reason'], row['home']), (self.old, target, 'clear', self.home))
        before = [(self.agent / path).read_bytes() for path in ('properties.json', 'sessions.jsonl')]
        with patch.object(sessions, 'write', side_effect=AssertionError('no write')), patch.object(agents, '_event') as event:
            self.change(target)
            event.assert_not_called()
        self.assertEqual(before, [(self.agent / path).read_bytes() for path in ('properties.json', 'sessions.jsonl')])

    def test_wrong_launch_previous_or_storage_never_changes_binding(self):
        target = str(uuid4())
        for previous, launch, home in ((self.old, 'outdated', self.home), (str(uuid4()), 'launch', self.home),
                                       (self.old, 'launch', self.home + '-other'), (self.old, None, self.home)):
            with self.subTest(previous=previous, launch=launch, home=home), self.assertRaises(ValueError):
                agents.rebind_session(self.root, self.agent.name, previous, target, 'clear', launch, home=home)
        self.assertEqual(read(self.agent / 'properties.json')['native_session_id'], self.old)
        self.assertFalse((self.agent / 'sessions.jsonl').exists())

    def test_append_is_authoritative_after_pointer_write_crashes(self):
        target = str(uuid4())
        original = sessions.write
        def fail_pointer(path, value):
            if Path(path).name == 'properties.json':
                raise OSError('simulated crash')
            return original(path, value)
        with patch.object(sessions, 'write', side_effect=fail_pointer), self.assertRaisesRegex(OSError, 'simulated crash'):
            self.change(target)
        self.assertEqual(read(self.agent / 'properties.json')['native_session_id'], self.old)
        other, previous = self.make_agent('two')
        with self.assertRaisesRegex(ValueError, 'another agent'):
            agents.rebind_session(self.root, other.name, previous, target, 'detected', 'launch', home=self.home)
        self.assertEqual(agents.session_properties(self.root, self.agent)['native_session_id'], target)
        self.assertEqual(len(self.rows()), 1)
        self.change(target)
        self.assertEqual(len(self.rows()), 1)

    def test_foreign_session_and_earlier_generations_are_reserved_across_the_tree(self):
        child = templates.create(self.root / 'subverses' / 'child', 'child', workdir=self.work)
        other, target = self.make_agent('other', child)
        with self.assertRaisesRegex(ValueError, 'another agent'):
            self.change(target)
        new = str(uuid4())
        agents.rebind_session(child, other.name, target, new, 'new', 'launch', home=self.home)
        with self.assertRaisesRegex(ValueError, 'another agent'):
            self.change(target)

    def test_different_storage_can_use_the_same_native_id(self):
        _, target = self.make_agent('other', home=self.home + '-other')
        self.assertEqual(self.change(target)['native_session_id'], target)

    def test_canonical_and_symlinked_universe_paths_are_the_same_owner(self):
        alias = self.work / 'universe-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        bound = agents.bind_session(alias, self.agent.name, self.old, 'launch')
        self.assertEqual(bound['native_session_id'], self.old)
        target = str(uuid4())
        rebound = agents.rebind_session(alias, self.agent.name, self.old, target, 'clear', 'launch', home=self.home)
        self.assertEqual(rebound['native_session_id'], target)

    def test_returning_to_own_earlier_generation_is_supported(self):
        target = str(uuid4())
        self.change(target)
        agents.rebind_session(self.root, self.agent.name, target, self.old, 'resume', 'launch', home=self.home)
        self.assertEqual([row['to'] for row in self.rows()], [target, self.old])

    def test_two_agents_cannot_claim_one_session_concurrently(self):
        other, previous = self.make_agent('two')
        target = str(uuid4())
        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, [(str(self.root), self.agent.name, self.old, target, self.home),
                                              (str(self.root), other.name, previous, target, self.home)]))
        self.assertEqual(results.count('ok'), 1, results)
        self.assertTrue(any('another agent' in result for result in results), results)

    def test_malformed_or_conflicting_journal_never_repairs_the_pointer(self):
        self.change(str(uuid4()))
        path = self.agent / 'sessions.jsonl'
        original = path.read_text()
        for suffix in ('{"schema":', '\n{}\n'):
            path.write_text(original + suffix)
            with self.assertRaisesRegex(ValueError, 'session journal'):
                agents.session_properties(self.root, self.agent)

    def test_first_binding_cannot_claim_another_agents_session(self):
        other, target = self.make_agent('two')
        data = read(self.agent / 'properties.json')
        data.pop('native_session_id')
        write(self.agent / 'properties.json', data)
        with self.assertRaisesRegex(ValueError, 'another agent'):
            agents.bind_session(self.root, self.agent.name, target, 'launch')
        self.assertNotIn('native_session_id', read(self.agent / 'properties.json'))

    def test_unknown_legacy_provider_binds_itself_but_never_takes_a_foreign_id(self):
        from multiverse import harnesses
        _, foreign = self.make_agent('two')
        for failure in (FileNotFoundError('no harness definition'), OSError('cannot read it'), ValueError('bad manifest')):
            data = read(self.agent / 'properties.json')
            data.pop('native_session_id', None)
            data.pop('native_session_provider', None)
            write(self.agent / 'properties.json', data)
            with self.subTest(failure=failure), patch.object(harnesses, 'load', side_effect=failure):
                self.assertEqual(agents.bind_session(self.root, self.agent.name, self.old, 'launch')['native_session_id'], self.old)
                data = read(self.agent / 'properties.json')
                data.pop('native_session_id')
                write(self.agent / 'properties.json', data)
                with self.assertRaisesRegex(ValueError, 'another agent'):
                    agents.bind_session(self.root, self.agent.name, foreign, 'launch')

    def test_conflict_is_visible_and_successful_owned_observation_clears_it(self):
        other, target = self.make_agent('two')
        with self.assertRaisesRegex(ValueError, 'another agent'):
            self.change(target)
        self.assertIn('another agent', agents.runtime(self.agent)['binding_conflict'])
        self.assertFalse((self.agent / 'sessions.jsonl').exists())
        self.change(str(uuid4()))
        self.assertIsNone(agents.runtime(self.agent)['binding_conflict'])

    def test_one_session_event_after_durable_change_and_none_for_replay(self):
        from multiverse.plugins import events
        target = str(uuid4())
        with patch.object(events, 'subscribers', return_value=['fixture']):
            self.change(target)
            self.change(target)
        records = [json.loads(line) for _, path in events.segments(self.root) for line in path.read_text().splitlines()]
        records = [event for event in records if event['type'] == 'agent.session']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['launch'], 'launch')
        self.assertEqual(records[0]['data'], {'from': self.old, 'to': target, 'reason': 'clear'})

    def test_move_cannot_import_a_conflicting_earlier_generation(self):
        target = str(uuid4())
        self.change(target)
        destination = templates.create(self.work / 'elsewhere', 'elsewhere', workdir=self.work)
        self.make_agent('other', destination, native_id=self.old)
        with patch.object(relocate, 'observe', return_value={'live': 'dormant'}), \
                self.assertRaisesRegex(ValueError, 'another agent'):
            relocate.move_agent(self.root, self.agent.name, destination)
        self.assertTrue((self.agent / 'properties.json').exists())
        self.assertFalse((destination / 'state' / 'agents' / self.agent.name).exists())

    def test_other_agents_torn_journal_only_blocks_ids_it_may_own(self):
        peer, previous = self.make_agent('damaged-peer')
        current, torn = str(uuid4()), str(uuid4())
        agents.rebind_session(self.root, peer.name, previous, current, 'clear', 'launch', home=self.home)
        journal = peer / 'sessions.jsonl'
        with journal.open('ab') as stream:
            stream.write(b'{"schema":1,"to":"' + torn.encode() + b'"')
        before = journal.read_bytes()
        healthy = str(uuid4())
        self.assertEqual(self.change(healthy)['native_session_id'], healthy)
        for requested in (previous, current, torn):
            with self.subTest(requested=requested), self.assertRaisesRegex(ValueError, 'damaged-peer.*sessions.jsonl'):
                agents.rebind_session(self.root, self.agent.name, healthy, requested, 'clear', 'launch', home=self.home)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(read(self.agent / 'properties.json')['native_session_id'], healthy)

    def test_os_unreadable_peer_journal_is_a_named_actionable_refusal(self):
        peer, previous = self.make_agent('permission-peer')
        agents.rebind_session(self.root, peer.name, previous, str(uuid4()), 'clear', 'launch', home=self.home)
        journal, original = peer / 'sessions.jsonl', Path.open
        def opened(path, *args, **kwargs):
            if path == journal:
                raise PermissionError('permission denied')
            return original(path, *args, **kwargs)
        for requested in (str(uuid4()), read(peer / 'properties.json')['native_session_id']):
            with self.subTest(requested=requested), patch.object(Path, 'open', opened), \
                    self.assertRaisesRegex(ValueError, 'permission-peer.*sessions.jsonl.*read'):
                self.change(requested)

    def test_stored_home_remains_valid_when_its_parent_later_becomes_a_symlink(self):
        parent = self.work / 'scope-parent'
        home = parent / 'home'
        home.mkdir(parents=True)
        self.home = str(home.resolve())
        write(self.agent / 'properties.json', {**read(self.agent / 'properties.json'), 'native_session_home': self.home})
        target = str(uuid4())
        self.change(target)
        parent.rename(self.work / 'original-scope')
        replacement = self.work / 'replacement-scope'
        replacement.mkdir()
        parent.symlink_to(replacement, target_is_directory=True)
        self.assertNotEqual(sessions.canonical_home(self.home), self.home)
        effective, rows = sessions.effective(self.agent, read(self.agent / 'properties.json'))
        self.assertEqual(effective['native_session_id'], target)
        self.assertEqual(rows[0]['home'], self.home, 'Historical storage is not reinterpreted through a new symlink')

