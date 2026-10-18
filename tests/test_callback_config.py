"""Agent data callbacks remain usable when a harness definition is damaged."""
import os
from unittest.mock import patch
from uuid import uuid4

from multiverse.store import read, write
from multiverse.tools import inbox, report
from multiverse.universe.mailbox import append_message
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
        self.config = self.root / 'harnesses/codex/harness.json'
        write(self.config, {'schema': 2, 'adapter': 'codex',
                            'settings': {'inbox_bridge': True, 'args': ['--remote', 'unix:///custom']}})

    def test_report_and_inbox_work_with_invalid_configuration_files(self):
        append_message(self.directory, 'still readable', {}, delivery='inbox-only')
        for config in (self.config.read_text(), '{bad-json', '[]'):
            with self.subTest(config=config), patch.dict(os.environ, self.env, clear=True):
                self.config.write_text(config)
                self.assertEqual(report.run(self.root, agent=self.identity, phase='working')['phase'], 'working')
                self.assertEqual(inbox.run(self.root, agent=self.identity, peek=True)['messages'][0]['text'],
                                 'still readable')

    def test_wrong_typed_harness_name_does_not_break_agent_data(self):
        write(self.directory / 'properties.json', {**self.properties, 'harness': []})
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(report.run(self.root, agent=self.identity, summary='saved')['summary'], 'saved')
            self.assertEqual(inbox.run(self.root, agent=self.identity)['messages'], [])

    def test_callbacks_still_refuse_damaged_identity_fields_or_unreadable_objects(self):
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

    def test_bad_configuration_does_not_relax_launch_or_agent_authority(self):
        for action in (report.run, inbox.run):
            with self.subTest(action=action), patch.dict(os.environ, {**self.env, 'MV2_LAUNCH_ID': 'old'}, clear=True):
                with self.assertRaisesRegex(ValueError, 'Outdated launch'):
                    action(self.root, agent=self.identity)
            with self.subTest(action=action, proof='incomplete'), patch.dict(os.environ,
                    {**self.env, 'MV2_LAUNCH_ID': '', 'MV2_IDENTITY_MODE': 'codex-agent-v1'}, clear=True):
                with self.assertRaisesRegex(ValueError, 'Agent identity could not be verified'):
                    action(self.root, agent=self.identity)
        self.assertNotIn('summary', read(self.directory / 'properties.json'))
