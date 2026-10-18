"""An agent directory appears only after its minimal identity is complete."""
import concurrent.futures
import threading
from unittest.mock import patch

from multiverse import harnesses
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from multiverse.universe import agents
from multiverse import display
from multiverse.tools import doctor
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
                row = lifecycle.listing(self.root, only=folders[0].name)[0]
                self.assertNotIn('damaged', row, row)
                self.assertEqual(row['runtime']['state'], 'starting')
                self.assertTrue((folders[0] / 'runtime.json').is_file())
                self.assertEqual(display.state(row), 'starting')
                self.assertTrue(doctor.run(self.root)['ok'])
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

    def test_abandoned_pre_child_stage_is_failed_and_can_be_removed(self):
        directory, properties, _, _ = lifecycle._record(self.root, 'abandoned', None, 'sleeper', None, None)
        from multiverse.store import uid
        with lifecycle._publish_record(self.root, directory, properties, uid()):
            pass  # Like a killed preparer: the minimal stage remains, but its lock is released.
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['live'], 'dormant')
        self.assertEqual(row['runtime']['state'], 'failed')
        self.assertFalse(row['runtime']['preparing'])
        self.assertIn('preparation ended', '\n'.join(doctor.run(self.root)['problems']))
        from multiverse.universe.relocate import remove_agent
        remove_agent(self.root, directory.name)
        self.assertFalse(directory.exists())

    def test_preparing_flag_with_child_evidence_cannot_bypass_stopped_check(self):
        directory, properties, _, _ = lifecycle._record(self.root, 'unknown', None, 'sleeper', None, None)
        from multiverse.store import uid
        launch_id = uid()
        with lifecycle._publish_record(self.root, directory, properties, launch_id):
            write(directory / 'launches' / launch_id / 'request.json', {'argv': ['/bin/sleep', '600']})
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['live'], 'unknown')
        self.assertIn('runtime.json', row['damaged']['file'])
        self.assertEqual(display.live_label(row), 'unknown')
        self.assertFalse(doctor.run(self.root)['ok'])
        self.assertIn('runtime.json', '\n'.join(doctor.run(self.root)['problems']))
        with self.assertRaisesRegex(ValueError, 'running or its liveness is unknown'):
            lifecycle.start(self.root, resume='unknown')
        before = (directory / 'runtime.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'runtime.json: preparation record has native startup evidence') as caught:
            lifecycle.kill(self.root, 'unknown')
        self.assertIn('repair or remove', str(caught.exception))
        self.assertIn('mv2 kill unknown', str(caught.exception))
        self.assertEqual((directory / 'runtime.json').read_bytes(), before)

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
