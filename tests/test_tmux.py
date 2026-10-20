"""Hostname drift must not hide verified local panes or authorize foreign ones."""
import unittest
from unittest.mock import patch

from multiverse.terminal.tmux import Tmux, observe, owned


class HostOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.properties = {'id': 'agent', 'universe_id': 'universe'}
        self.runtime = {'host': 'before', 'launch_id': 'launch',
                        'tmux': {'socket': '/recorded/socket', 'pane': '%1'}}
        self.pane = {'pane_dead': '0', '@mv2_agent_id': 'agent',
                     '@mv2_universe_id': 'universe', '@mv2_launch_id': 'launch'}
        self.enterContext(patch('socket.gethostname', return_value='after'))

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
            self.assertEqual(owned(self.properties, self.runtime)[1], '%1')
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
