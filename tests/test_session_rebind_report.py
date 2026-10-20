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

    def test_a_hook_report_rebinds_only_the_exact_current_launch(self):
        """A /clear report from the mod's hook rebinds only from the current launch, with both fields, when the process
        evidence agrees or is absent, passing the launch's own storage; fragile because hooks run in subprocesses
        (subagents, nested Claude) that inherit the agent's env, so a wrong process silently moves the agent to
        another conversation (43b420c)."""
        rebind = 'multiverse.universe.agents.rebind_session'
        with patch(rebind, return_value={'native_session_id': self.session}) as rebound:
            self.assertEqual(self.report(), {'native_session_id': self.session})
        rebound.assert_called_once_with(self.root, self.agent.name, self.previous, self.session, 'clear', 'current',
                                        home=os.path.realpath(self.config))
        with patch(rebind, side_effect=ValueError('bound to another agent')):
            with self.assertRaisesRegex(ValueError, 'bound to another agent'):
                self.report()
        with patch(rebind) as rebound:
            for missing in ('previous_session_id', 'session_reason'):
                with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, 'together'):
                    self.report(**{missing: None})
            with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'old'}), self.assertRaisesRegex(ValueError, 'Outdated launch'):
                self.report()
            rebound.assert_not_called()
        parser = argparse.ArgumentParser()
        runtime_update.configure(parser)
        with self.assertRaises(SystemExit):  # only what an integration can know; core records "detected" itself
            parser.parse_args(['--agent', 'worker', '--native-session-id', self.session,
                               '--previous-session-id', self.previous, '--session-reason', 'detected'])
        observed = lambda native_id: ObservedBinding(native_id=native_id, home=os.path.realpath(self.config),
                                                     evidence='claude-process-file')
        observer = 'multiverse.harnesses.claude.ClaudeAdapter.observed_binding'
        with patch(observer, return_value=observed(str(uuid4()))), patch(rebind) as rebound:
            with self.assertRaisesRegex(ValueError, 'process is on another session'):
                self.report()
            rebound.assert_not_called()
        for seen in (observed(self.session), None):  # the process shows it, or no evidence either way
            with self.subTest(seen=seen), patch(observer, return_value=seen), patch(rebind, return_value={}) as rebound:
                self.report()
                rebound.assert_called_once()
        write(self.agent / 'properties.json', {'id': self.agent.name, 'name': 'worker', 'harness': 'codex',
                                               'native_session_id': self.previous})
        with patch(rebind, return_value={}) as rebound:
            self.report(session_reason='new')
        self.assertIsNone(rebound.call_args.kwargs['home'])  # a harness without storage passes no home
