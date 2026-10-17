"""A harness integration reports that its process moved to another native session (/clear)."""
import argparse
import os
from unittest.mock import patch
from uuid import uuid4

from multiverse.harnesses.types import ObservedBinding
from multiverse.store import write
from multiverse.tools import runtime_update

from test_multiverse import BaseTest


class SessionRebindReportTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.previous, self.session = str(uuid4()), str(uuid4())
        self.agent = self.root / 'state/agents' / str(uuid4())
        write(self.agent / 'properties.json', {'id': self.agent.name, 'name': 'worker', 'harness': 'claude',
                                               'workspace': str(self.work), 'native_session_id': self.previous})
        write(self.agent / 'runtime.json', {'launch_id': 'current', 'state': 'running'})
        self.config = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current', 'CLAUDE_CONFIG_DIR': str(self.config)}))

    def report(self, **changes):
        values = dict(agent='worker', native_session_id=self.session, previous_session_id=self.previous,
                      session_reason='clear')
        values.update(changes)
        return runtime_update.run(self.root, **values)

    def test_a_clear_rebinds_the_exact_launch_with_the_launch_storage(self):
        with patch('multiverse.universe.agents.rebind_session', return_value={'native_session_id': self.session}) as rebind:
            self.assertEqual(self.report(), {'native_session_id': self.session})
        rebind.assert_called_once_with(self.root, self.agent.name, self.previous, self.session, 'clear', 'current',
                                       home=os.path.realpath(self.config))

    def test_a_refused_or_incomplete_report_fails(self):
        with patch('multiverse.universe.agents.rebind_session', side_effect=ValueError('bound to another agent')):
            with self.assertRaisesRegex(ValueError, 'bound to another agent'):
                self.report()
        with patch('multiverse.universe.agents.rebind_session') as rebind:
            for missing in ('previous_session_id', 'session_reason'):
                with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, 'together'):
                    self.report(**{missing: None})
            with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'old'}), self.assertRaisesRegex(ValueError, 'Outdated launch'):
                self.report()
            rebind.assert_not_called()
        parser = argparse.ArgumentParser()
        runtime_update.configure(parser)
        with self.assertRaises(SystemExit):  # only what an integration can know; core records "detected" itself
            parser.parse_args(['--agent', 'worker', '--native-session-id', self.session,
                               '--previous-session-id', self.previous, '--session-reason', 'detected'])

    def test_a_harness_without_storage_passes_no_home(self):
        write(self.agent / 'properties.json', {'id': self.agent.name, 'name': 'worker', 'harness': 'codex',
                                               'native_session_id': self.previous})
        with patch('multiverse.universe.agents.rebind_session', return_value={}) as rebind:
            self.report(session_reason='new')
        self.assertIsNone(rebind.call_args.kwargs['home'])

    def test_a_move_the_launchs_process_does_not_show_is_refused(self):
        observed = lambda native_id: ObservedBinding(native_id=native_id, home=os.path.realpath(self.config),
                                                     evidence='claude-process-file')
        observer = 'multiverse.harnesses.claude.ClaudeAdapter.observed_binding'
        with patch(observer, return_value=observed(str(uuid4()))), \
                patch('multiverse.universe.agents.rebind_session') as rebind:
            with self.assertRaisesRegex(ValueError, 'process is on another session'):
                self.report()
            rebind.assert_not_called()
        for seen in (observed(self.session), None):  # the process shows it, or no evidence either way
            with self.subTest(seen=seen), patch(observer, return_value=seen), \
                    patch('multiverse.universe.agents.rebind_session', return_value={}) as rebind:
                self.report()
                rebind.assert_called_once()

