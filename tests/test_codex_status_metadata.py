"""Missing optional connection metadata must not disable native status."""
import asyncio
import json
import unittest
from uuid import uuid4

from multiverse.runtime.runner import StatusWatcher
from multiverse.store import write
from test_codex_status import StatusStreamTest


class StatusMetadataTest(StatusStreamTest):
    async def test_missing_launch_directory_keeps_status_and_logs_exact_launch_once(self):
        self.agent_dir = self.root / 'state/agents/agent'
        self.agent_dir.mkdir(parents=True)
        write(self.root / 'harnesses/codex/harness.json', {
            'schema': 2, 'adapter': 'codex', 'settings': {'inbox_bridge': True}})
        write(self.agent_dir / 'properties.json', {'id': 'agent', 'harness': 'codex',
            'workspace': str(self.root), 'native_session_id': self.session.native_id})
        launch_id = str(uuid4())
        launch = self.agent_dir / 'launches' / launch_id
        write(self.agent_dir / 'runtime.json', {'launch_id': launch_id, 'state': 'running',
            'status': 'unknown', 'control_endpoint': {'transport': 'unix-websocket', 'address': self.socket}})
        self.assertFalse(launch.exists())
        with self.assertLogs('multiverse.runtime.attachments', level='WARNING') as messages:
            self.watcher = StatusWatcher(self.agent_dir, launch_id, self.root)
            self.watcher.start()
            await self.eventually(lambda: self.runtime_status() == 'idle')
            await self.connection.send(json.dumps(self.event({'type': 'active', 'activeFlags': []})))
            await self.eventually(lambda: self.runtime_status() == 'working')
            await asyncio.to_thread(self.watcher.close)
        self.assertEqual(len(messages.output), 1)
        self.assertIn(launch_id, messages.output[0])
        self.assertIn('launch directory is missing', messages.output[0])
        self.assertFalse((self.agent_dir / 'launches').exists())


def load_tests(loader, tests, pattern):
    # Reuse the offline server fixture without rediscovering its inherited tests.
    return unittest.TestSuite([StatusMetadataTest(
        'test_missing_launch_directory_keeps_status_and_logs_exact_launch_once')])
