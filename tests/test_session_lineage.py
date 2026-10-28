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

    def fresh(self, name):
        """Make a new agent the one self.change moves, so each case of a table starts from its own binding."""
        self.agent, self.old = self.make_agent(name)

    def test_a_session_change_is_journaled_once(self):
        """Each real session change writes one journal row and one plugin event, and a repeated /clear report writes
        nothing; fragile because hooks fire the same report repeatedly, and duplicates silently grow the journal and
        spam subscribers."""
        from multiverse.plugins import events
        self.enterContext(patch.object(events, 'subscribers', return_value=['fixture']))
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
        self.change(target)
        records = [json.loads(line) for _, path in events.segments(self.root) for line in path.read_text().splitlines()]
        records = [event for event in records if event['type'] == 'agent.session']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['launch'], 'launch')
        self.assertEqual(records[0]['data'], {'from': self.old, 'to': target, 'reason': 'clear'})
        # Returning to its own earlier generation is a change of its own.
        agents.rebind_session(self.root, self.agent.name, target, self.old, 'resume', 'launch', home=self.home)
        self.assertEqual([row['to'] for row in self.rows()], [target, self.old])

    def test_transcript_generations_keep_departed_scopes_without_repairing_records(self):
        """Fresh starts and returns must not assign an old conversation the current home or invent an unbound generation."""
        properties = read(self.agent / 'properties.json')
        self.assertEqual(sessions.generations(self.agent, properties), ())
        first, second, third = self.old, str(uuid4()), str(uuid4())
        def transition(previous, target, home, stamp):
            return {'schema': 1, 'id': str(uuid4()), 'at': stamp, 'harness': 'pi',
                    'home': home, 'launch': 'launch', 'from': previous, 'to': target, 'reason': 'clear'}
        def save(rows):
            (self.agent / 'sessions.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        rows = [transition(first, second, self.home, 'first'),
                transition(second, first, self.home, 'second'),
                transition(first, third, self.home, 'latest')]
        save(rows)
        current = {**properties, 'native_session_id': third}
        before = {path.name: path.read_bytes() for path in self.agent.iterdir() if path.is_file()}
        with patch.object(sessions, 'write', side_effect=AssertionError('generation reads never repair')):
            earlier = sessions.generations(self.agent, current)
        self.assertEqual([(item['native_id'], item['ended_at']) for item in earlier],
                         [(second, 'second'), (first, 'latest')])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.agent.iterdir() if path.is_file()})
        # Returning to second excludes its old departure from the earlier list.
        rows.append(transition(third, second, self.home, 'returned'))
        save(rows)
        self.assertEqual([item['native_id'] for item in sessions.generations(self.agent,
                         {**properties, 'native_session_id': second})], [first, third])
        # Retirement's replacement scope is never assigned to the old ID.
        replacement_home = self.home + '-new'
        replacement = {key: None for key in sessions.FRESH_FIELDS}
        replacement.update(native_session_home=replacement_home, native_storage_environment={}, native_fresh_pending=True)
        retirement = {'schema': 2, 'kind': 'fresh-start', 'id': str(uuid4()), 'at': 'retired',
                      'harness': 'pi', 'home': self.home, 'launch': str(uuid4()), 'from': second, 'to': None,
                      'reason': 'missing-conversation', 'replacement': replacement}
        rows.append(retirement)
        save(rows)
        pending = {**properties, **replacement, 'native_session_transition': retirement['id']}
        self.assertEqual([(item['native_id'], item['home']) for item in sessions.generations(self.agent, pending)],
                         [(first, self.home), (third, self.home), (second, self.home)])
        bound = str(uuid4())
        rows.append({**transition(None, bound, replacement_home, 'bound'), 'schema': 2, 'kind': 'bind'})
        save(rows)
        bound_properties = {**pending, 'native_session_id': bound}
        self.assertEqual(len(sessions.generations(self.agent, bound_properties)), 3)
        # A recorded unknown home stays unknown; no current-home fallback.
        rows[-2]['home'] = None
        rows = rows[-2:]
        save(rows)
        self.assertIsNone(sessions.generations(self.agent, bound_properties)[0]['home'])
        # Historical strings are not canonicalized through a link changed later.
        rows[0]['home'] = self.home
        save(rows)
        Path(self.home).symlink_to(self.work / 'moved', target_is_directory=True)
        self.assertEqual(sessions.generations(self.agent, bound_properties)[0]['home'], self.home)
        for content in ('{', '[]\n'):
            with self.subTest(damaged=content):
                (self.agent / 'sessions.jsonl').write_text(content)
                with self.assertRaisesRegex(sessions.JournalError, 'sessions.jsonl.*repair'):
                    sessions.generations(self.agent, bound_properties)

    def test_who_may_bind_or_rebind_a_native_session(self):
        """Only the owner may bind or rebind a native session id across the whole tree (stale launch, wrong previous
        id or home, foreign current or earlier generation, legacy provider, symlinked root, move import refused;
        other storage allowed); fragile because ownership is a scan of every agent's journal, and a missed scope lets
        two agents share a conversation."""
        from multiverse import harnesses
        with self.subTest('a wrong launch, previous id or storage'):
            target = str(uuid4())
            for previous, launch, home in ((self.old, 'outdated', self.home), (str(uuid4()), 'launch', self.home),
                                           (self.old, 'launch', self.home + '-other'), (self.old, None, self.home)):
                with self.subTest(previous=previous, launch=launch, home=home), self.assertRaises(ValueError):
                    agents.rebind_session(self.root, self.agent.name, previous, target, 'clear', launch, home=home)
            self.assertEqual(read(self.agent / 'properties.json')['native_session_id'], self.old)
            self.assertFalse((self.agent / 'sessions.jsonl').exists())
        with self.subTest("another agent's session, and the conflict it leaves visible"):
            self.fresh('conflicted')
            other, target = self.make_agent('holder')
            with self.assertRaisesRegex(ValueError, 'another agent'):
                self.change(target)
            self.assertIn('another agent', agents.runtime(self.agent)['binding_conflict'])
            self.assertFalse((self.agent / 'sessions.jsonl').exists())
            self.change(str(uuid4()))
            self.assertIsNone(agents.runtime(self.agent)['binding_conflict'])
        with self.subTest("a subverse agent's current and earlier sessions"):
            self.fresh('tree')
            child = templates.create(self.root / 'subverses' / 'child', 'child', workdir=self.work)
            other, target = self.make_agent('other', child)
            with self.assertRaisesRegex(ValueError, 'another agent'):
                self.change(target)
            new = str(uuid4())
            agents.rebind_session(child, other.name, target, new, 'new', 'launch', home=self.home)
            with self.assertRaisesRegex(ValueError, 'another agent'):
                self.change(target)
        with self.subTest("a first binding to another agent's session"):
            self.fresh('unbound')
            other, target = self.make_agent('bound')
            data = read(self.agent / 'properties.json')
            data.pop('native_session_id')
            write(self.agent / 'properties.json', data)
            with self.assertRaisesRegex(ValueError, 'another agent'):
                agents.bind_session(self.root, self.agent.name, target, 'launch')
            self.assertNotIn('native_session_id', read(self.agent / 'properties.json'))
        with self.subTest('an unknown legacy provider'):
            self.fresh('legacy')
            _, foreign = self.make_agent('foreign')
            for failure in (FileNotFoundError('no harness definition'), OSError('cannot read it'),
                            ValueError('bad manifest')):
                data = read(self.agent / 'properties.json')
                data.pop('native_session_id', None)
                data.pop('native_session_provider', None)
                write(self.agent / 'properties.json', data)
                with self.subTest(failure=failure), patch.object(harnesses, 'load', side_effect=failure):
                    bound = agents.bind_session(self.root, self.agent.name, self.old, 'launch')
                    self.assertEqual(bound['native_session_id'], self.old)
                    data = read(self.agent / 'properties.json')
                    data.pop('native_session_id')
                    write(self.agent / 'properties.json', data)
                    with self.assertRaisesRegex(ValueError, 'another agent'):
                        agents.bind_session(self.root, self.agent.name, foreign, 'launch')
        with self.subTest('the same id in other storage'):
            self.fresh('elsewhere-stored')
            _, target = self.make_agent('other-storage', home=self.home + '-other')
            self.assertEqual(self.change(target)['native_session_id'], target)
        with self.subTest('a symlinked universe path'):
            self.fresh('aliased')
            alias = self.work / 'universe-alias'
            alias.symlink_to(self.root, target_is_directory=True)
            bound = agents.bind_session(alias, self.agent.name, self.old, 'launch')
            self.assertEqual(bound['native_session_id'], self.old)
            target = str(uuid4())
            rebound = agents.rebind_session(alias, self.agent.name, self.old, target, 'clear', 'launch', home=self.home)
            self.assertEqual(rebound['native_session_id'], target)
        with self.subTest('a move into a universe that holds an earlier generation'):
            self.fresh('mover')
            target = str(uuid4())
            self.change(target)
            # Relocation needs a real stopped launch shape, beyond this module's
            # session-only fixture. The earlier conversation stays in its journal.
            write(self.agent / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'stopped',
                  'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}})
            destination = templates.create(self.work / 'elsewhere', 'elsewhere', workdir=self.work)
            self.make_agent('other', destination, native_id=self.old)
            with patch.object(relocate, 'observe', return_value={'live': 'dormant'}), \
                    self.assertRaisesRegex(ValueError, 'another agent'):
                relocate.move_agent(self.root, self.agent.name, destination)
            self.assertTrue((self.agent / 'properties.json').exists())
            self.assertFalse((destination / 'state' / 'agents' / self.agent.name).exists())

    def test_append_is_authoritative_after_pointer_write_crashes(self):
        """The journal append stays authoritative when the pointer write crashes; fragile because only a simulated
        crash reaches that state, and another agent could then claim the session; a fresh-start retirement also
        recovers its new storage and cannot resurrect the old binding while waiting for a reported ID."""
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
        for assigned in (False, True):
            with self.subTest(retirement_assigned=assigned):
                self.fresh('retired-' + str(assigned))
                previous = read(self.agent / 'properties.json')
                target = str(uuid4()) if assigned else None
                home = self.home + '-replacement'
                replacement = {**previous, 'native_session_id': target, 'native_session_home': home,
                               'native_storage_environment': {'PI_CODING_AGENT_DIR': home},
                               'native_environment': {}, 'native_service_identity': None}
                def fail_new_pointer(path, value):
                    if Path(path).name == 'properties.json' and value.get('native_session_id') != self.old:
                        raise OSError('simulated crash')
                    return original(path, value)
                with patch.object(sessions, 'write', side_effect=fail_new_pointer), self.assertRaisesRegex(OSError, 'simulated crash'):
                    sessions.retire(self.root, self.agent, self.old, replacement, str(uuid4()), previous_launch='launch')
                retained = read(self.agent / 'properties.json')
                self.assertEqual({key: retained[key] for key in previous}, previous)
                # Even before recovering the pointer, neither old nor newly
                # reserved storage may be skipped by a peer's ownership scan.
                for identity, scope in ((self.old, self.home), (target, home)):
                    if identity:
                        with sessions.namespace(self.root), self.assertRaises(sessions.SessionConflict):
                            sessions.claim(self.root, other, read(other / 'properties.json'), identity, scope)
                recovered = agents.session_properties(self.root, self.agent)
                self.assertEqual(recovered['native_session_id'], target)
                self.assertEqual(recovered['native_session_home'], home)
                self.assertEqual(recovered['native_storage_environment'], replacement['native_storage_environment'])
                self.assertTrue(recovered['native_fresh_pending'])
                self.assertTrue(sessions.pending_fresh(self.agent, recovered))
                for identity, scope in ((self.old, self.home), (target, home)):
                    if identity:
                        with sessions.namespace(self.root), self.assertRaises(sessions.SessionConflict):
                            sessions.claim(self.root, other, read(other / 'properties.json'), identity, scope)
                if not assigned:
                    target = str(uuid4())
                    with patch.object(agents, 'write', side_effect=fail_new_pointer), self.assertRaisesRegex(OSError, 'simulated crash'):
                        agents.bind_session(self.root, self.agent.name, target, 'launch')
                    self.assertIsNone(read(self.agent / 'properties.json')['native_session_id'])
                    recovered = agents.session_properties(self.root, self.agent)
                    self.assertEqual(recovered['native_session_id'], target)
                    self.assertFalse(recovered['native_fresh_pending'])
                with self.assertRaisesRegex(ValueError, 'retired'):
                    agents.rebind_session(self.root, self.agent.name, target, self.old, 'detected', 'launch', home=home)

    def test_two_agents_cannot_claim_one_session_concurrently(self):
        """Two processes cannot claim one session at once; fragile because only real concurrency exposes the
        namespace lock, and a break gives two agents one conversation."""
        other, previous = self.make_agent('two')
        target = str(uuid4())
        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, [(str(self.root), self.agent.name, self.old, target, self.home),
                                              (str(self.root), other.name, previous, target, self.home)]))
        self.assertEqual(results.count('ok'), 1, results)
        self.assertTrue(any('another agent' in result for result in results), results)

    def test_home_aliases_preserve_history_but_cannot_duplicate_ownership(self):
        """History keeps its recorded paths while live alias claims collide; resolving everywhere corrupts history, but resolving nowhere permits two owners after a rename."""
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
        # Rename the real storage and leave an alias at its former location.
        # Current and historical session IDs still belong to the same peer.
        self.fresh('claimant')
        parent = self.work / 'old-parent'
        old_home = parent / 'home'
        old_home.mkdir(parents=True)
        peer, previous = self.make_agent('old-scope', home=str(old_home))
        current = str(uuid4())
        agents.rebind_session(self.root, peer.name, previous, current, 'clear', 'launch', home=str(old_home))
        moved = self.work / 'moved-parent'
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        before = (peer / 'sessions.jsonl').read_bytes()
        for identity in (previous, current):
            for home in (str(old_home), str(moved / 'home')):
                with self.subTest(identity=identity, home=home), sessions.namespace(self.root), \
                        self.assertRaisesRegex(sessions.SessionConflict, 'another agent'):
                    sessions.claim(self.root, self.agent, read(self.agent / 'properties.json'), identity, home)
        self.assertEqual((peer / 'sessions.jsonl').read_bytes(), before)
