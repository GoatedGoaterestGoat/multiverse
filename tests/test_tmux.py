"""Hostname drift must not hide verified local panes or authorize foreign ones."""
import unittest
from unittest.mock import patch

from isolation import isolate

from multiverse.terminal.tmux import Tmux, observe, owned


class HostOwnershipTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.properties = {'id': 'agent', 'universe_id': 'universe'}
        self.runtime = {'host': 'before', 'launch_id': 'launch',
                        'tmux': {'socket': '/recorded/socket', 'session': '$1', 'pane': '%1'}}
        self.pane = {'pane_dead': '0', '@mv2_agent_id': 'agent',
                     '@mv2_universe_id': 'universe', '@mv2_launch_id': 'launch'}
        self.enterContext(patch('socket.gethostname', return_value='after'))
        def identity(*args):
            self.assertEqual(args[:4], ('display-message', '-p', '-t', '%1'))
            return '1234\t5678\t$1\t%1\tagent\tuniverse\tlaunch'
        self.enterContext(patch.object(Tmux, '_run', side_effect=identity))

    def snapshot(self, value):
        return patch.object(Tmux, 'snapshot', return_value=value)

    def test_pane_ownership_gate(self):
        """observe and owned over pane snapshots: exact tags after a hostname change are alive and owned, any wrong or
        missing agent, universe or launch tag is not, and a missing pane, snapshot error, dead pane or ending state
        reads correctly; fragile because several independent checks gate every kill and send, and a break hides live
        agents or authorizes control of a foreign pane."""
        with self.subTest('exact tags after a hostname change'), self.snapshot(({'%1': self.pane}, None)) as snapshot:
            cache = {}
            for _ in range(2):
                self.assertEqual(observe(self.properties, self.runtime, cache)['live'], 'alive')
            snapshot.assert_called_once()
            backend, pane = owned(self.properties, self.runtime)
            self.assertEqual(pane, '%1')
            self.assertEqual(backend.identity, {'pid': 1234, 'epoch': 5678})
        for field in ('@mv2_agent_id', '@mv2_universe_id', '@mv2_launch_id'):
            for value in ('different', None):
                with self.subTest(field=field, value=value), self.snapshot(({'%1': {**self.pane, field: value}}, None)):
                    self.assertEqual(observe(self.properties, self.runtime)['live'], 'dormant')
                    with self.assertRaises(ValueError):
                        owned(self.properties, self.runtime)
        # A pane or server gone after a hostname change is dormant, so the agent can resume its session.
        for runtime in (self.runtime, {**self.runtime, 'state': 'running'}):
            for snapshot in (({}, None), ({'%2': self.pane}, None)):
                with self.subTest(runtime=runtime, snapshot=snapshot), self.snapshot(snapshot):
                    self.assertEqual(observe(self.properties, runtime),
                                     {'live': 'dormant', 'reason': 'Recorded pane is absent'})
                    with self.assertRaises(ValueError):
                        owned(self.properties, runtime)
        with self.subTest('no local server, no saved identity'), self.snapshot(({}, None)):
            elsewhere = {'host': 'some-other-host', 'tmux': {'pane': '%1', 'socket': '/tmp/nope'}}
            self.assertEqual(observe({}, elsewhere)['live'], 'dormant')
        for reason in ('Operation not permitted', 'tmux observation timed out'):
            with self.subTest(reason=reason), self.snapshot(({}, reason)):
                self.assertEqual(observe(self.properties, self.runtime), {'live': 'unknown', 'reason': reason})
                with self.assertRaises(ValueError):
                    owned(self.properties, self.runtime)
        with self.subTest('dead pane'), self.snapshot(({'%1': {**self.pane, 'pane_dead': '1'}}, None)):
            self.assertEqual(observe(self.properties, self.runtime)['live'], 'stopped')
            with self.assertRaises(ValueError):
                owned(self.properties, self.runtime)
        ending = {**self.runtime, 'state': 'ending', 'ending_at': 'now'}
        with self.subTest('ending'), self.snapshot(({'%1': self.pane}, None)):
            self.assertEqual(observe(self.properties, ending)['live'], 'ending')
            with self.assertRaises(ValueError):
                owned(self.properties, ending)
            self.assertEqual(owned(self.properties, ending, states=('ending',))[1], '%1')
            exited = {**ending, 'state': 'stopped', 'exit_code': -15}
            self.assertEqual(observe(self.properties, exited)['live'], 'stopped')
        with self.subTest('ending without its pane'), self.snapshot(({}, None)):
            self.assertEqual(observe(self.properties, ending)['live'], 'dormant')


from test_multiverse import TerminalBase


class StorageEnvironmentTest(TerminalBase):
    def test_storage_paths_are_read_whole_before_proving_unique_home(self):
        """A literal newline or trailing whitespace in tmux's storage value must not hide a second conversation and falsely prove a unique home."""
        import json
        import sys
        from uuid import uuid4
        from multiverse.harnesses.codex import CodexAdapter
        from multiverse.runtime import lifecycle, storage_adoption
        directory, props, _, _ = lifecycle._record(self.root, 'home-proof', '', 'codex', None, None)
        native = str(uuid4())
        props.update(native_session_id=native, workspace=str(self.work))
        previous = {'launch_id': str(uuid4()), 'state': 'dormant'}
        caller = self.work / 'caller'
        self.backend.run('new-session', '-d', '-s', 'env-proof', sys.executable, '-c', 'import time; time.sleep(60)')
        self.backend.run('set-environment', '-g', 'HOME', str(self.work))
        for spelling in ('server\nhome', 'server\rhome', 'server-home\n', 'server-home \n'):
            with self.subTest(spelling=spelling):
                server = self.work / spelling
                for home in (server, caller):
                    folder = home / 'sessions'
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / ('rollout-2026-10-19T19-00-00-' + native + '.jsonl')).write_text(
                        json.dumps({'type': 'session_meta', 'payload': {'id': native}}) + '\n')
                self.backend.run('set-environment', '-g', 'CODEX_HOME', str(server))
                try:
                    actual = self.backend.environment('CODEX_HOME')
                except ValueError as error:
                    self.assertEqual(spelling, 'server\rhome')
                    self.assertIn('ambiguous escaped output', str(error))
                else:
                    self.assertEqual(actual, str(server))
                with self.assertRaisesRegex(ValueError, 'Cannot prove unique conversation storage'):
                    storage_adoption.discover(CodexAdapter.configured('codex', {}), props, previous, directory,
                                              {'HOME': str(self.work), 'CODEX_HOME': str(caller)}, [('recorded terminal server', self.backend, {})])
        # Empty and unset are distinct controls for the same raw-value reader.
        self.backend.run('set-environment', '-g', 'CODEX_HOME', '')
        self.assertEqual(self.backend.environment('CODEX_HOME'), '')
        self.backend.run('set-environment', '-gu', 'CODEX_HOME')
        self.assertIsNone(self.backend.environment('CODEX_HOME'))
        self.backend.run('set-environment', '-gr', 'CODEX_HOME')
        self.assertIsNone(self.backend.environment('CODEX_HOME'))
