"""An agent directory appears only after its minimal identity is complete."""
import concurrent.futures
import threading
from unittest.mock import patch

from multiverse import harnesses
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from multiverse.universe import agents
from test_multiverse import BaseTest


class PublicationTest(BaseTest):
    def setUp(self):
        super().setUp()
        folder = self.root / 'harnesses' / 'sleeper'
        folder.mkdir()
        write(folder / 'harness.json', {'schema': 1, 'spawn': ['/bin/sleep', '600'],
                                       'resume': ['/bin/sleep', '600']})
        self.adapter = harnesses.load(self.root, 'sleeper')

    def test_preparation_never_exposes_a_folder_without_properties(self):
        entered, release = threading.Event(), threading.Event()
        def prepare(context):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test barrier timed out')
            raise ValueError('preparation fixture fails')
        with patch.object(harnesses, 'load', return_value=self.adapter), \
                patch.object(self.adapter, 'spawn', side_effect=prepare), \
                concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(lifecycle.start, self.root, name='preparing', harness='sleeper')
            try:
                self.assertTrue(entered.wait(5))
                folders = list((self.root / 'state' / 'agents').iterdir())
                self.assertEqual(len(folders), 1)
                self.assertTrue((folders[0] / 'properties.json').is_file())
                self.assertEqual(read(folders[0] / 'properties.json')['name'], 'preparing')
            finally:
                release.set()
            with self.assertRaisesRegex(ValueError, 'preparation fixture'):
                future.result()

    def test_failed_preparation_remains_named_and_removable(self):
        with patch.object(harnesses, 'load', return_value=self.adapter), \
                patch.object(self.adapter, 'spawn', side_effect=ValueError('preparation fixture fails')):
            with self.assertRaisesRegex(ValueError, 'preparation fixture'):
                lifecycle.start(self.root, name='failed', harness='sleeper')
        directory = agents.directory(self.root, 'failed')
        self.assertEqual(read(directory / 'runtime.json')['state'], 'failed')
        self.assertIn('preparation fixture fails', read(directory / 'runtime.json')['error'])
        from multiverse.universe.relocate import remove_agent
        remove_agent(self.root, directory.name)
        self.assertEqual(list((self.root / 'state' / 'agents').glob('*/properties.json')), [])

    def test_same_name_is_reserved_once_and_different_names_prepare_together(self):
        entered = threading.Barrier(2)
        def prepare(context):
            entered.wait(5)
            raise ValueError('preparation fixture fails')
        with patch.object(harnesses, 'load', return_value=self.adapter), \
                patch.object(self.adapter, 'spawn', side_effect=prepare), \
                concurrent.futures.ThreadPoolExecutor() as pool:
            futures = [pool.submit(lifecycle.start, self.root, name=name, harness='sleeper')
                       for name in ('one', 'two')]
            for future in futures:
                with self.assertRaisesRegex(ValueError, 'preparation fixture'):
                    future.result()
        self.assertEqual({read(path)['name'] for path in (self.root / 'state' / 'agents').glob('*/properties.json')}, {'one', 'two'})
        def same():
            try:
                lifecycle.start(self.root, name='same', harness='sleeper')
            except ValueError as error:
                return str(error)
        with patch.object(harnesses, 'load', return_value=self.adapter), \
                patch.object(self.adapter, 'spawn', side_effect=ValueError('preparation fixture fails')), \
                concurrent.futures.ThreadPoolExecutor() as pool:
            results = list(pool.map(lambda _: same(), range(4)))
        self.assertEqual(sum('preparation fixture fails' in result for result in results), 1)
        self.assertEqual(sum('already has an agent named same' in result for result in results), 3)
