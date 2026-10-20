"""A runtime record that a launch replaces while discovery runs is never overwritten or trashed."""
from unittest.mock import patch
from multiverse.runtime import lifecycle, missing_runtime
from multiverse.store import read, uid, write
from multiverse.universe import records, relocate
from test_multiverse import BaseTest


class RelocateAuthorityBoundaryTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory, self.properties, _, _ = lifecycle._record(self.root, 'worker', '', 'codex', None, None)
        write(self.directory / 'properties.json', self.properties)
        self.path = self.directory / 'runtime.json'
        write(self.directory / 'launches' / uid() / 'request.json',
              {'env': {'MV2_TMUX_SOCKET': str(self.work / 'absent.sock')}})
        self.facts = {'launch_id': None, 'state': 'running', 'pid': 12345}

    def test_runtime_replaced_during_discovery_is_never_overwritten(self):
        """For remove and resume, a runtime.json replaced while discovery runs is neither overwritten nor trashed;
        fragile because it races a concurrent launch, and losing the fresh record is silent."""
        operations = (('remove', lambda: relocate.remove_agent(self.root, 'worker')),
                      ('remove with torn JSON', lambda: relocate.remove_agent(self.root, 'worker')),
                      ('resume', lambda: lifecycle.start(self.root, resume='worker',
                                                         socket_path=str(self.work / 'absent.sock'))))
        for operation, call in operations:
            with self.subTest(operation=operation):
                write(self.path, self.facts)
                if operation == 'remove with torn JSON':
                    self.path.write_text('{torn')
                replacement = {**self.facts, 'launch_id': uid()}

                def discovery(*args, **kwargs):
                    write(self.path, replacement)
                    return []
                with patch.object(missing_runtime, 'require_stopped', side_effect=discovery), \
                     self.assertRaisesRegex(records.RecordError, 'runtime.json.*changed during terminal discovery'):
                    call()
                self.assertEqual(read(self.path), replacement)
                self.assertTrue(self.directory.exists())
                self.assertFalse((self.root / 'trash').exists())
