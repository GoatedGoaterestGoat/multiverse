"""`python -m multiverse.compaction`, kept for Claude launches from before compaction became a plugin.

Their --settings still run `"${MV2_PYTHON}" -m multiverse.compaction guard || exit 2`.
The guard must let Claude compact (such a launch no longer has the hooks that would
answer /compact), and other actions go to the plugin only where it is installed and on.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse.compaction import __main__ as stub
from test_multiverse import BaseTest

SOURCE = str(Path(__file__).resolve().parents[1] / 'src')
CLEARED = ('MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID', 'MV2_PLUGIN_PATH')
OLD_GUARD = '"${MV2_PYTHON:-python3}" -m multiverse.compaction guard || exit 2'


class StubTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = self.root / 'state' / 'agents' / 'a'
        self.agent.mkdir(parents=True)

    def test_old_launches_compaction_hook_still_works(self):
        """The hook command baked into Claude launches from before compaction became a plugin still exits 0 and says to
        restart, and other old actions reach the plugin only where it is installed and on; fragile because no current
        code calls this module path, so removing it passes every other test while every old agent's /compact is
        blocked (8236aec, 06d7c4f)."""
        with self.subTest("an old launch's guard"):
            environment = {key: value for key, value in os.environ.items() if key not in CLEARED}
            result = subprocess.run(['sh', '-c', OLD_GUARD], input='{"trigger": "auto"}', text=True,
                                    capture_output=True, cwd=self.work,
                                    env={**environment, 'MV2_PYTHON': sys.executable, 'PYTHONPATH': SOURCE,
                                         'MV2_AGENT_DIR': str(self.agent)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('restart this agent to use tree compaction', result.stderr)
        with self.subTest('other actions'):
            # An agent's own MV2_AGENT_DIR or MV2_UNIVERSE must not decide where these go.
            clean = {key: value for key, value in os.environ.items() if key not in CLEARED}
            with patch.dict(os.environ, clean, clear=True), \
                    patch('multiverse.plugins.__main__.main', return_value=7) as forwarded:
                with patch('multiverse.plugins.entry', side_effect=ValueError('No plugin named compaction here')):
                    self.assertEqual(stub.main(['status', '--agent-dir', str(self.agent)]), 0)  # not installed: nothing
                self.assertEqual(stub.main(['status']), 0)  # no agent, no universe: nothing
                forwarded.assert_not_called()
                # install answers an old copy of the hooks still in memory with a failure that says to restart.
                with patch('multiverse.plugins.entry', side_effect=ValueError('No plugin named compaction here')), \
                        patch('sys.stdout', new=io.StringIO()) as out:
                    self.assertEqual(stub.main(['install', '--agent-dir', str(self.agent)]), 1)
                self.assertEqual(json.loads(out.getvalue()),
                                 {'event': 'failed', 'message': 'restart this agent to use tree compaction'})
                forwarded.assert_not_called()
                for state, expected in (('off', 0), ('loaded', 7)):
                    found = type('Entry', (), {'state': state})()
                    with patch('multiverse.plugins.entry', return_value=found):
                        self.assertEqual(stub.main(['status', '--agent-dir', str(self.agent)]), expected)
            forwarded.assert_called_once_with(['-u', str(self.root.resolve()), 'run', 'compaction', 'status',
                                               '--agent-dir', str(self.agent)])


if __name__ == '__main__':
    unittest.main()
