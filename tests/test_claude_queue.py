"""Claude's part of the queued-prompts report: the adapter reading the mod's queue file, with no Claude session."""
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from multiverse import harnesses
from multiverse.harnesses.claude import BRIDGE, QUEUE_FILE
from multiverse.harnesses.types import NativeEndpoint, SessionHandle
from multiverse.store import read, write
from test_multiverse import BaseTest

TOKEN = '0123456789abcdef0123456789abcdef'
FOLLOW_UP, STEER = str(uuid4()), str(uuid4())


class ClaudeQueueTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.socket = str(self.work / 'inbox.sock')
        self.config = self.work / 'claude-config'
        self.agent_dir = self.work / 'agent'
        self.agent_dir.mkdir()
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))
        self.enterContext(patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 322)))
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings']['inbox_bridge'] = True
        write(path, config)
        self.adapter = harnesses.load(self.root, 'claude')
        digest = hashlib.sha256(os.path.abspath(self.socket).encode()).hexdigest()
        (self.config / 'sessions').mkdir(parents=True)
        (self.config / 'sessions' / f'4242.{digest}.key').write_text(json.dumps({'peerToken': TOKEN}))
        self.native = str(uuid4())

    def session(self, bridge=BRIDGE):
        return SessionHandle('agent', 'claude', str(self.work), launch_id='launch-1', native_id=self.native, state='running',
                             endpoint=NativeEndpoint('unix-lines', self.socket), bridge=bridge, status='working',
                             session_file=str(self.agent_dir / 'session.jsonl'), home=str(self.config))

    def test_the_report_is_only_this_launchs_and_conversations(self):
        """mv2 refuses an agent's message on this report: a stale file from another launch or conversation, a damaged
        one, or a reloaded mod's partial view read as complete would refuse sends for messages no longer queued, or let
        two agents' messages cross."""
        entry = lambda ident, mode: {'message_id': ident, 'mode': mode, 'since': '2026-11-10T12:00:00Z'}
        good = {'schema': 1, 'launch_id': 'launch-1', 'session_id': self.native, 'observed_at': '2026-11-10T12:00:01Z',
                'coverage': 'complete', 'reason': None, 'entries': [entry(FOLLOW_UP, 'follow-up'), entry(STEER, 'steer')]}
        listed = [{**entry(FOLLOW_UP, 'follow-up'), 'native_id': None}, {**entry(STEER, 'steer'), 'native_id': None}]
        for name, contents, coverage, reason, entries in [
            ('this launch and conversation', good, 'complete', None, listed),
            ('the mod after a reload', {**good, 'coverage': 'unknown', 'reason': 'The multiverse plugin reloaded'},
             'unknown', 'The multiverse plugin reloaded', listed),
            ('another launch', {**good, 'launch_id': 'launch-0'}, 'unknown', 'another launch', []),
            ('another conversation', {**good, 'session_id': str(uuid4())}, 'unknown', 'another conversation', []),
            ('no file yet', None, 'unknown', "hasn't written", []),
            ('a damaged file', '{"schema": 1, "entr', 'unknown', "can't be read", []),
            # Python equates True and 1.0 with 1: only the integer 1 is this schema.
            ('a schema marker that is true, not 1', {**good, 'schema': True}, 'unknown', "can't be read", []),
            ('a schema marker that is 1.0, not 1', {**good, 'schema': 1.0}, 'unknown', "can't be read", []),
            ('an entry that is not a message ID', {**good, 'entries': [entry(FOLLOW_UP, 'follow-up'), entry('typed', 'follow-up')]},
             'unknown', "can't read", listed[:1]),
            ('an entry of another kind', {**good, 'entries': [entry(STEER, 'ask')]}, 'unknown', "can't read", []),
        ]:
            with self.subTest(name):
                path = self.agent_dir / QUEUE_FILE
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(contents if isinstance(contents, str) else json.dumps(contents))
                reply = self.adapter.queued_prompts(self.session())
                self.assertEqual(reply.state, 'ok')
                self.assertEqual((reply.value['coverage'], reply.value['entries']), (coverage, entries))
                # Core refuses a report with no time: a file that says nothing still has the time it was read.
                self.assertTrue(reply.value['observed_at'])
                if name == 'another launch':
                    self.assertNotEqual(reply.value['observed_at'], good['observed_at'])
                if reason is None:
                    self.assertIsNone(reply.value['reason'])
                else:
                    self.assertIn(reason, reply.value['reason'])
        with self.subTest('a launch without the bridge'):
            reply = self.adapter.queued_prompts(self.session(bridge=None))
            self.assertEqual(reply.state, 'unavailable')
