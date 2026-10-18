"""Geometry comes from the same owned pane observation, without another tmux call."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.runtime import runner
from multiverse.store import write
from test_multiverse import BaseTest


class PaneEvidenceTest(BaseTest):
    def snapshot(self, raw):
        identity, launch_id = str(uuid4()), str(uuid4())
        directory = self.root / 'state/agents' / identity
        write(directory / 'properties.json', {'id': identity, 'native_session_id': str(uuid4())})
        write(directory / 'runtime.json', {'launch_id': launch_id, 'state': 'running',
              'attachment': {'readiness': 'process'}, 'native_versions': {'cli': '0.163.0'}})
        observed = []
        def started(evidence):
            observed.append(evidence)
            return True
        adapter = SimpleNamespace(launch_refusal=lambda evidence: None,
                                  supports_launch_refusal=lambda version: True, launch_started=started)
        monitor = runner.RefusalMonitor(directory, launch_id, self.root, adapter, 'fixture')
        backend = Mock()
        backend.run.side_effect = ['current owned pane', raw]
        with patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')):
            monitor._run()
        self.assertEqual(backend.run.call_count, 2)
        self.assertIn('#{pane_width}', backend.run.call_args.args[-1])
        return observed[0]

    def test_width_joins_cursor_capture(self):
        observed = self.snapshot('1\t2\t20\t40')
        self.assertIs(observed['cursor_visible'], True)
        self.assertEqual((observed['cursor_x'], observed['cursor_y'], observed['width']), (2, 20, 40))

    def test_unknown_width_does_not_corrupt_other_cursor_evidence(self):
        for value in ('0', '-1', 'unknown', 'True'):
            with self.subTest(value=value):
                observed = self.snapshot('0\t2\t20\t' + value)
                self.assertIs(observed['cursor_visible'], False)
                self.assertEqual((observed['cursor_x'], observed['cursor_y']), (2, 20))
                self.assertIsNone(observed['width'])

    def test_old_three_field_fixture_retains_full_composer_evidence(self):
        observed = self.snapshot('1\t2\t20')
        self.assertIs(observed['cursor_visible'], True)
        self.assertEqual(observed['cursor_x'], 2)
        self.assertIsNone(observed['width'])
