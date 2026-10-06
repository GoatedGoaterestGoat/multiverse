"""`runtime-update --bridge` checks the shape of an integration's bridge id, not a fixed list."""
import argparse
import contextlib
import io
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse.store import read, write
from multiverse.tools import runtime_update
from test_multiverse import BaseTest

EXISTING = ('pi-inbox-v1', 'pi-socket-v1', 'claude-mod-v1', 'none')
WELL_FORMED = (*EXISTING, 'other-bridge-v1', 'codex-app-v2', 'a', 'x1', 'a1-b2-c3', 'a' * 40)
MALFORMED = ('', ' ', 'Claude-mod-v1', 'claude_mod_v1', 'claude mod', '-claude', 'claude-', 'claude--mod', '1claude',
             'claude/mod', 'claude.mod', '../claude', 'claude\n', 'claude-mod-v1\n', '\nclaude', 'clâude', 'ｃlaude',
             'a' * 41, 'a' * 200, '--status', 'none ')


class BridgeIdTest(BaseTest):
    def agent(self):
        agent = str(uuid4())
        directory = self.root / 'state/agents' / agent
        write(directory / 'properties.json', {'id': agent, 'name': 'worker', 'harness': 'claude'})
        write(directory / 'runtime.json', {'launch_id': 'current', 'state': 'running', 'native_bridge': 'claude-mod-v1'})
        return agent, directory

    def test_every_id_in_use_stays_valid_and_other_well_formed_ids_are_accepted(self):
        for value in WELL_FORMED:
            with self.subTest(value=value):
                self.assertEqual(runtime_update.bridge_id(value), value)

    def test_malformed_ids_are_rejected_with_the_rule(self):
        for value in MALFORMED:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'lowercase letters, digits and single hyphens'):
                runtime_update.bridge_id(value)
        for value in (None, 5, b'claude', ['claude']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime_update.bridge_id(value)

    def test_the_command_line_accepts_a_well_formed_id_and_explains_a_bad_one(self):
        parser = argparse.ArgumentParser()
        runtime_update.configure(parser)
        for value in WELL_FORMED:
            self.assertEqual(parser.parse_args(['--status', 'idle', '--bridge', value]).bridge, value)
        for value in ('Bad Id', 'x' * 41, 'two--hyphens'):
            error = io.StringIO()
            with self.subTest(value=value), contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                parser.parse_args(['--status', 'idle', '--bridge', value])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('lowercase letters, digits and single hyphens', error.getvalue())
        self.assertIsNone(parser.parse_args(['--status', 'idle']).bridge)

    def test_run_records_a_valid_id_clears_on_none_and_never_writes_a_malformed_one(self):
        agent, directory = self.agent()
        with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current'}):
            runtime_update.run(self.root, agent=agent, status='idle', bridge='other-bridge-v1')
            self.assertEqual(read(directory / 'runtime.json')['native_bridge'], 'other-bridge-v1')
            for value in ('Other Bridge', 'x' * 41, 'a--b'):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'lowercase letters'):
                    runtime_update.run(self.root, agent=agent, status='working', bridge=value)
            facts = read(directory / 'runtime.json')
            self.assertEqual((facts['native_bridge'], facts.get('status')), ('other-bridge-v1', 'idle'))  # nothing half-written
            runtime_update.run(self.root, agent=agent, status='idle', bridge='none')
            self.assertIsNone(read(directory / 'runtime.json')['native_bridge'])
            runtime_update.run(self.root, agent=agent, status='waiting')  # no --bridge leaves it alone
            self.assertIsNone(read(directory / 'runtime.json')['native_bridge'])

    def test_a_stale_launch_is_still_refused_before_any_id_is_recorded(self):
        agent, directory = self.agent()
        with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'previous'}), self.assertRaisesRegex(ValueError, 'Outdated launch'):
            runtime_update.run(self.root, agent=agent, status='idle', bridge='other-bridge-v1')
        self.assertEqual(read(directory / 'runtime.json')['native_bridge'], 'claude-mod-v1')


if __name__ == '__main__':
    import unittest
    unittest.main()
