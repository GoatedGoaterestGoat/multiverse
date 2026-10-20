"""An agent directory appears only after its minimal identity is complete, and every end of a preparation is safe."""
import concurrent.futures
from contextlib import ExitStack
import threading
from unittest.mock import patch

from multiverse import harnesses
from multiverse.runtime import lifecycle, missing_runtime
from multiverse.store import read, uid, write
from multiverse.universe import agents, relocate
from multiverse import display
from multiverse.harnesses.types import CommandSpec
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
        """An agent folder appears only with complete minimal properties and reads as starting while it is prepared;
        fragile because only concurrent readers (dashboard, doctor) see the window, and a break shows intermittent
        damaged rows during every spawn (f7cf2ce)."""
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

    def test_preparation_end_states(self):
        """A validation failure releases its new name, an abandoned preparation is failed, never starting, and removable
        without discovery, and a preparing flag with native child evidence is a damaged unknown row that resume, kill
        and remove refuse; fragile because preparing is a crash state only a killed preparer produces, and a break
        strands undeletable agents or starts a second child (e5bbba0, 1eb2abc)."""
        for label, behavior, reason in (
                ('adapter refusal', {'side_effect': ValueError('preparation fixture fails')}, 'preparation fixture'),
                ('missing executable', {'return_value': CommandSpec([str(self.work / 'missing-cli')])},
                 'Harness executable not found')):
            with self.subTest(preflight=label), patch.object(harnesses, 'load', return_value=self.adapter), \
                    patch.object(self.adapter, 'spawn', **behavior):
                for _ in range(2):  # The same name remains usable without removing a failed record first.
                    with self.assertRaisesRegex(ValueError, reason):
                        lifecycle.start(self.root, name='failed', harness='sleeper')
                    self.assertEqual(list(agents.record_paths(self.root)), [])
                    agents.check_available_name(self.root, 'failed')
        with self.subTest('abandoned before the child'):
            directory, properties, _, _ = lifecycle._record(self.root, 'abandoned', None, 'sleeper', None, None)
            with lifecycle._publish_record(self.root, directory, properties, uid()):
                pass  # Like a killed preparer: the minimal stage remains, but its lock is released.
            row = lifecycle.listing(self.root, only=directory.name)[0]
            self.assertEqual(row['live'], 'dormant')
            self.assertNotEqual(display.live_label(row), 'starting')
            self.assertEqual(row['runtime']['state'], 'failed')
            self.assertFalse(row['runtime']['preparing'])
            self.assertIn('preparation ended', '\n'.join(doctor.run(self.root)['problems']))
            before = (directory / 'runtime.json').read_bytes()
            unexpected = AssertionError('no discovery for minimal preparation')
            with patch.object(missing_runtime, 'discover', side_effect=unexpected), ExitStack() as stack:
                relocate._hold(stack, [directory])
            self.assertEqual((directory / 'runtime.json').read_bytes(), before)
            relocate.remove_agent(self.root, directory.name)
            self.assertFalse(directory.exists())
        with self.subTest('preparing with native child evidence'):
            directory, properties, _, _ = lifecycle._record(self.root, 'unknown', None, 'sleeper', None, None)
            launch_id = uid()
            with lifecycle._publish_record(self.root, directory, properties, launch_id):
                write(directory / 'launches' / launch_id / 'request.json', {'argv': ['/bin/sleep', '600']})
                row = lifecycle.listing(self.root, only=directory.name)[0]  # while its publisher holds the lock
                self.assertNotEqual(display.live_label(row), 'starting')
                self.assertIn('native startup evidence', row['damaged']['reason'])
            row = lifecycle.listing(self.root)[0]
            self.assertEqual(row['live'], 'unknown')
            self.assertIn('runtime.json', row['damaged']['file'])
            self.assertEqual(display.live_label(row), 'unknown')
            self.assertFalse(doctor.run(self.root)['ok'])
            self.assertIn('runtime.json', '\n'.join(doctor.run(self.root)['problems']))
            with self.assertRaisesRegex(ValueError, 'running or its liveness is unknown'):
                lifecycle.start(self.root, resume='unknown')
            before = (directory / 'runtime.json').read_bytes()
            for operation in (lifecycle.kill, relocate.remove_agent):
                with self.assertRaisesRegex(ValueError, 'runtime.json.*native startup evidence'):
                    operation(self.root, 'unknown')
                self.assertEqual((directory / 'runtime.json').read_bytes(), before)
            self.assertTrue(directory.exists())
