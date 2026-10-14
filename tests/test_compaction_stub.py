"""`python -m multiverse.compaction`, kept for Claude launches from before compaction became a plugin.

Their --settings still run `"${MV2_PYTHON}" -m multiverse.compaction guard || exit 2`.
The guard must let Claude compact (such a launch no longer has the hooks that would
answer /compact), other actions go to the plugin only where it is installed and on,
and `mv2 doctor` names the running agents that still use the old guard.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import compaction
from multiverse.compaction import __main__ as stub
from multiverse.store import write
from test_multiverse import BaseTest

SOURCE = str(Path(__file__).resolve().parents[1] / 'src')
CLEARED = ('MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID', 'MV2_PLUGIN_PATH')
OLD_GUARD = '"${MV2_PYTHON:-python3}" -m multiverse.compaction guard || exit 2'


class StubTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = self.root / 'state' / 'agents' / 'a'
        self.agent.mkdir(parents=True)

    def test_an_old_launchs_guard_lets_claude_compact_and_says_to_restart(self):
        environment = {key: value for key, value in os.environ.items() if key not in CLEARED}
        result = subprocess.run(['sh', '-c', OLD_GUARD], input='{"trigger": "auto"}', text=True, capture_output=True,
                                cwd=self.work, env={**environment, 'MV2_PYTHON': sys.executable, 'PYTHONPATH': SOURCE,
                                                    'MV2_AGENT_DIR': str(self.agent)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('restart this agent to use tree compaction', result.stderr)

    def test_other_actions_go_to_the_plugin_only_where_it_is_installed_and_on(self):
        # An agent's own MV2_AGENT_DIR or MV2_UNIVERSE must not decide where these go.
        clean = {key: value for key, value in os.environ.items() if key not in CLEARED}
        with patch.dict(os.environ, clean, clear=True), \
                patch('multiverse.plugins.__main__.main', return_value=7) as forwarded:
            with patch('multiverse.plugins.entry', side_effect=ValueError('No plugin named compaction here')):
                self.assertEqual(stub.main(['status', '--agent-dir', str(self.agent)]), 0)  # not installed: nothing
            self.assertEqual(stub.main(['status']), 0)  # no agent, no universe: nothing
            forwarded.assert_not_called()
            for state, expected in (('off', 0), ('loaded', 7)):
                found = type('Entry', (), {'state': state})()
                with patch('multiverse.plugins.entry', return_value=found):
                    self.assertEqual(stub.main(['status', '--agent-dir', str(self.agent)]), expected)
        forwarded.assert_called_once_with(['-u', str(self.root.resolve()), 'run', 'compaction', 'status',
                                           '--agent-dir', str(self.agent)])

    def test_doctor_names_running_agents_that_still_use_the_old_guard(self):
        launch = self.agent / 'launches' / 'L1'
        settings = json.dumps({'hooks': {'PreCompact': [{'hooks': [{'type': 'command', 'command': OLD_GUARD}]}]}})
        write(launch / 'request.json', {'argv': ['claude', '--settings', settings]})
        self.assertTrue(compaction.old_guard(self.agent, {'state': 'running', 'launch_id': 'L1'}))
        self.assertFalse(compaction.old_guard(self.agent, {'state': 'stopped', 'launch_id': 'L1'}))
        write(launch / 'request.json', {'argv': ['claude', '--settings', settings.replace(
            '-m multiverse.compaction guard || exit 2', '-m multiverse.plugins run compaction guard')]})
        self.assertFalse(compaction.old_guard(self.agent, {'state': 'running', 'launch_id': 'L1'}))
        self.assertFalse(compaction.old_guard(self.agent, {'state': 'running', 'launch_id': 'missing'}))
        # Only the hook counts: a prompt or message that mentions the module is not the old guard.
        write(launch / 'request.json', {'argv': ['claude', '--append-system-prompt', 'see python -m multiverse.compaction guard',
                                                 'resume python -m multiverse.compaction status']})
        self.assertFalse(compaction.old_guard(self.agent, {'state': 'running', 'launch_id': 'L1'}))
        # A launch record of another shape is not the old guard, and never an error doctor would trip on.
        for request in ([], {'argv': 'claude'}, {'argv': ['claude', '--settings']}, {'argv': ['--settings', 'not json']},
                        {'argv': ['--settings', json.dumps(['x'])]}, {'argv': ['--settings', json.dumps({'hooks': ['bad']})]},
                        {'argv': ['--settings', json.dumps({'hooks': {'PreCompact': {'x': 1}}})]},
                        {'argv': ['--settings', json.dumps({'hooks': {'PreCompact': ['bad', {'hooks': 'x'}, {'hooks': [5]}]}})]}):
            with self.subTest(request=request):
                write(launch / 'request.json', request)
                self.assertFalse(compaction.old_guard(self.agent, {'state': 'running', 'launch_id': 'L1'}))
        (launch / 'request.json').write_text('{broken')
        self.assertFalse(compaction.old_guard(self.agent, {'state': 'running', 'launch_id': 'L1'}))
        self.assertFalse(compaction.old_guard(self.agent, None))

    def test_mv2_doctor_reports_it(self):
        from multiverse.tools import doctor
        item = {'id': 'a', 'name': 'worker', 'live': 'alive', 'runtime': {'state': 'running', 'launch_id': 'L1'}}
        with patch('multiverse.runtime.lifecycle.listing', return_value=[item]), \
                patch('multiverse.compaction.old_guard', return_value=True):
            problems = [sentence for _, sentence in doctor.check(self.root)]
        self.assertIn('worker runs the compaction guard from before compaction became a plugin: '
                      'restart it to use tree compaction.', problems)


if __name__ == '__main__':
    unittest.main()
