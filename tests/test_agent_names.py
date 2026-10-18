"""Universe-local spawn names, concurrent reservation and legacy addressing."""
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import threading
from unittest.mock import Mock, patch
from uuid import uuid4

from test_multiverse import BaseTest, ROOT
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.universe import templates
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

    def test_duplicate_name_is_refused_before_side_effects_in_every_end_state(self):
        directory = self.record('worker')
        for state in (None, 'running', 'stopped', 'dormant', 'failed'):
            with self.subTest(state=state):
                if state:
                    write(directory / 'runtime.json', {'launch_id': 'old', 'state': state})
                before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                with self.assertRaisesRegex(ValueError, 'already has an agent named worker; pick another name or resume it with mv2 resume worker'):
                    lifecycle.start(self.root, 'worker', harness='fake')
                after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                self.assertEqual(after, before)
        self.backend.create.assert_not_called()

    def test_parallel_spawns_cannot_publish_two_agents_with_the_same_name(self):
        ready = threading.Barrier(2)
        def start():
            ready.wait(timeout=3)  # Concurrent contenders; only the winner prepares.
            try:
                return lifecycle.start(self.root, 'shared-name', harness='fake')
            except ValueError as error:
                return error
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1, results)
        self.assertEqual(sum(isinstance(value, ValueError) for value in results), 1, results)
        records = list((self.root / 'state/agents').glob('*/properties.json'))
        self.assertEqual(len(records), 1)
        self.assertEqual(read(records[0])['name'], 'shared-name')
        self.assertEqual(self.backend.create.call_count, 1)

    def test_same_name_in_a_subverse_is_allowed_and_resume_reuses_the_original_record(self):
        first = lifecycle.start(self.root, 'worker', harness='fake')
        child = templates.create(self.root / 'subverses/child', 'child', workdir=self.work)
        write(child / 'harnesses/fake/harness.json', self.recipe)
        second = lifecycle.start(child, 'worker', harness='fake')
        self.assertNotEqual(first['id'], second['id'])
        native_id = str(uuid4())
        agents.bind_session(self.root, first['id'], native_id)
        resumed = lifecycle.start(self.root, resume='worker')
        self.assertEqual(resumed['id'], first['id'])
        self.assertEqual(resumed['native_session_id'], native_id)
        self.assertEqual(len(list((self.root / 'state/agents').glob('*/properties.json'))), 1)

    def test_existing_duplicate_names_still_resolve_and_resume_by_uuid_prefix(self):
        first = self.record('old-name', identifier='11111111-0000-4000-8000-000000000001', state='dormant')
        second = self.record('old-name', identifier='22222222-0000-4000-8000-000000000002', state='failed')
        with self.assertRaises(ValueError):
            agents.directory(self.root, 'old-name')
        self.assertEqual(agents.directory(self.root, first.name[:8]), first)
        self.assertEqual(agents.directory(self.root, second.name), second)
        self.assertEqual(lifecycle.start(self.root, resume=first.name[:8])['id'], first.name)
        self.assertEqual(lifecycle.start(self.root, resume=second.name[:8])['id'], second.name)

    def test_exact_name_wins_over_another_agents_uuid_prefix(self):
        named = self.record('a', identifier='11111111-0000-4000-8000-000000000001')
        prefix = self.record('b', identifier='ab2ea794-0000-4000-8000-000000000002')
        self.assertEqual(agents.directory(self.root, 'a'), named)
        self.assertEqual(agents.directory(self.root, 'ab2'), prefix)
        self.assertEqual(agents.directory(self.root, prefix.name), prefix)

    def test_exact_name_cannot_retarget_another_agents_full_uuid(self):
        first = self.record('first', identifier='11111111-0000-4000-8000-000000000001')
        self.record(first.name, identifier='22222222-0000-4000-8000-000000000002')
        with self.assertRaises(agents.AmbiguousAgent):
            agents.directory(self.root, first.name)

    def test_unknown_agent_error_is_plain_in_core_and_cli(self):
        expected = "No agent named missing here. Run mv2 list to see this universe's agents."
        with self.assertRaises(ValueError) as error:
            agents.directory(self.root, 'missing')
        self.assertEqual(str(error.exception), expected)
        env = {key: value for key, value in os.environ.items() if not key.startswith('MV2_')}
        env['PYTHONPATH'] = str(ROOT / 'src')
        completed = subprocess.run([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root),
            'resume', 'missing'], env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, 'mv2: ' + expected + '\n')
        self.assertEqual(completed.stdout, '')
