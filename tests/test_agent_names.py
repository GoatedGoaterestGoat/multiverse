"""Universe-local spawn names, concurrent reservation and addressing precedence."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import sys
import threading
from unittest.mock import Mock, patch
from uuid import uuid4

from test_multiverse import BaseTest
from multiverse import cli
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.store import read, write


class AgentNamesTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.recipe = {'schema': 1, 'spawn': [sys.executable, '-c', 'pass'],
                       'resume': [sys.executable, '-c', 'pass']}
        write(self.root / 'harnesses/fake/harness.json', self.recipe)
        self.backend = Mock()
        self.backend.create.return_value = {'socket': '/unused/tmux.sock', 'session': '$1', 'pane': '%1'}
        self.enterContext(patch.object(lifecycle, 'Tmux', return_value=self.backend))
        self.enterContext(patch.object(lifecycle.shutil, 'which', return_value=sys.executable))
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}))

    def record(self, name, identifier=None, state=None):
        identifier = identifier or str(uuid4())
        directory = self.root / 'state/agents' / identifier
        write(directory / 'properties.json', {'id': identifier, 'name': name, 'harness': 'fake',
            'universe_id': read(self.root / 'universe.json')['id'], 'task': '',
            'workspace': str(self.work), 'native_session_id': str(uuid4())})
        if state:
            write(directory / 'runtime.json', {'launch_id': str(uuid4()), 'state': state})
        return directory

    def cli(self, *arguments):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *arguments]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                self.assertRaises(SystemExit) as stopped:
            cli.main()
        return stopped.exception.code, output.getvalue()

    def test_duplicate_name_is_refused_before_side_effects_in_every_end_state(self):
        """A duplicate name is refused before any file or tmux side effect in every end state; fragile because new
        preparation steps get added ahead of the check, and a break leaves half-made records that later read as
        damage."""
        directory = self.record('worker')
        for state in (None, 'running', 'stopped', 'dormant', 'failed'):
            with self.subTest(state=state):
                if state:
                    write(directory / 'runtime.json', {'launch_id': 'old', 'state': state})
                before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                with self.assertRaisesRegex(ValueError, 'already has an agent named worker'):
                    lifecycle.start(self.root, 'worker', harness='fake')
                after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                self.assertEqual(after, before)
        self.backend.create.assert_not_called()

    def test_parallel_spawns_cannot_publish_two_agents_with_the_same_name(self):
        """Parallel spawns reserve a name once, while different names still prepare at the same time; fragile because
        it is a race hand-testing never hits, and a break publishes two agents with one name."""
        def named(name):
            return [path for path in (self.root / 'state/agents').glob('*/properties.json')
                    if read(path)['name'] == name]

        def start(name):
            try:
                return lifecycle.start(self.root, name, harness='fake')
            except ValueError as error:
                return error
        with self.subTest('two spawns of one name'):
            ready = threading.Barrier(2)
            def contend(_):
                ready.wait(timeout=3)  # Concurrent contenders; only the winner prepares.
                return start('shared-name')
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(contend, range(2)))
            self.assertEqual(sum(isinstance(value, dict) for value in results), 1, results)
            self.assertEqual(sum(isinstance(value, ValueError) for value in results), 1, results)
            self.assertEqual(len(named('shared-name')), 1)
            self.assertEqual(self.backend.create.call_count, 1)
        adapter = harnesses.load(self.root, 'fake')
        with self.subTest('different names prepare together'):
            entered = threading.Barrier(2)
            def prepare(context):
                entered.wait(5)  # Both preparations run at once, or this barrier breaks.
                raise ValueError('preparation fixture fails')
            with patch.object(harnesses, 'load', return_value=adapter), \
                    patch.object(adapter, 'spawn', side_effect=prepare), ThreadPoolExecutor() as pool:
                results = list(pool.map(start, ('one', 'two')))
            self.assertEqual([str(result) for result in results], ['preparation fixture fails'] * 2)
            self.assertEqual((len(named('one')), len(named('two'))), (1, 1))
        with self.subTest('four spawns of one name whose preparation fails'):
            with patch.object(harnesses, 'load', return_value=adapter), \
                    patch.object(adapter, 'spawn', side_effect=ValueError('preparation fixture fails')), \
                    ThreadPoolExecutor() as pool:
                results = [str(result) for result in pool.map(start, ['same'] * 4)]
            self.assertEqual(sum('preparation fixture fails' in result for result in results), 1, results)
            self.assertEqual(sum('already has an agent named same' in result for result in results), 3, results)
            self.assertEqual(len(named('same')), 1)

    def test_addressing_precedence(self):
        """An exact name beats another agent's uuid prefix but never its full uuid, an ambiguous reference is refused
        naming each match without saving anything, and old duplicate names still resolve by prefix; fragile because
        the precedence is subtle, and a break silently sends to, kills or resumes the wrong agent (abac381,
        14bc474)."""
        with self.subTest('an exact name beats another agent\'s uuid prefix'):
            named = self.record('a', identifier='33333333-0000-4000-8000-000000000003')
            prefix = self.record('b', identifier='ab2ea794-0000-4000-8000-000000000004')
            self.assertEqual(agents.directory(self.root, 'a'), named)
            self.assertEqual(agents.directory(self.root, 'ab2'), prefix)
            self.assertEqual(agents.directory(self.root, prefix.name), prefix)
        with self.subTest('an exact name cannot retarget another agent\'s full uuid'):
            first = self.record('first', identifier='44444444-0000-4000-8000-000000000005')
            self.record(first.name, identifier='55555555-0000-4000-8000-000000000006')
            with self.assertRaises(agents.AmbiguousAgent):
                agents.directory(self.root, first.name)
        with self.subTest('old duplicate names still resolve and resume by uuid prefix'):
            first = self.record('old-name', identifier='11111111-0000-4000-8000-000000000001', state='dormant')
            second = self.record('old-name', identifier='22222222-0000-4000-8000-000000000002', state='failed')
            with self.assertRaises(ValueError):
                agents.directory(self.root, 'old-name')
            self.assertEqual(agents.directory(self.root, first.name[:8]), first)
            self.assertEqual(agents.directory(self.root, second.name), second)
            self.assertEqual(lifecycle.start(self.root, resume=first.name[:8])['id'], first.name)
            self.assertEqual(lifecycle.start(self.root, resume=second.name[:8])['id'], second.name)
        ids = ('a1234567-1111-4111-8111-111111111111', 'a1234567-2222-4222-8222-222222222222')
        for identifier, name in zip(ids, ('alpha', 'beta')):
            self.record(name, identifier=identifier)
        for command in (('send', 'a1234567', '--text', 'must not be saved'), ('inspect', 'a1234567')):
            with self.subTest(command=command):
                code, output = self.cli(*command)
                self.assertEqual(code, 1)
                for text in (*ids, 'alpha', 'beta'):
                    self.assertIn(text, output)
        self.assertEqual(list(self.root.glob('state/agents/*/inbox.jsonl')), [])
