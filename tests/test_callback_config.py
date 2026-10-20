"""Agent data callbacks refuse a damaged identity record instead of acting on it."""
import os
from unittest.mock import patch
from uuid import uuid4

from multiverse.store import write
from multiverse.tools import inbox, report
from test_multiverse import BaseTest


class CallbackConfigTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.identity = str(uuid4())
        self.directory = self.root / 'state/agents' / self.identity
        self.properties = {'id': self.identity, 'name': 'worker', 'harness': 'codex',
                           'workspace': str(self.work)}
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', {'launch_id': 'current'})
        self.env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': self.identity,
                    'MV2_AGENT_DIR': str(self.directory), 'MV2_LAUNCH_ID': 'current'}

    def test_callbacks_refuse_damaged_identity_and_write_nothing(self):
        """report and inbox refuse, naming properties.json and writing no cursor, when the agent's id or name is
        damaged or the file is not an object; a break goes unnoticed because the agent's calls fail or act on a
        wrong identity inside its own session (2bb9d42)."""
        for value in ({**self.properties, 'id': []}, {**self.properties, 'name': []}, []):
            with self.subTest(value=value):
                write(self.directory / 'properties.json', value)
                before = (self.directory / 'properties.json').read_bytes()
                with patch.dict(os.environ, self.env, clear=True):
                    for action in (report.run, inbox.run):
                        with self.assertRaisesRegex(ValueError, 'properties.json'):
                            action(self.root, agent=self.identity)
                self.assertEqual((self.directory / 'properties.json').read_bytes(), before)
                self.assertFalse((self.directory / 'inbox.cursor').exists())
