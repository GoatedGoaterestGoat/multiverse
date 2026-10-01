"""Hostname drift must not hide verified local panes or authorize foreign ones."""
import unittest
from unittest.mock import patch

from multiverse.tmux import Tmux, observe, owned


class HostOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.properties = {'id': 'agent', 'universe_id': 'universe'}
        self.runtime = {'host': 'before', 'launch_id': 'launch',
                        'tmux': {'socket': '/recorded/socket', 'pane': '%1'}}
        self.pane = {'pane_dead': '0', '@mv2_agent_id': 'agent',
                     '@mv2_universe_id': 'universe', '@mv2_launch_id': 'launch'}
        self.enterContext(patch('socket.gethostname', return_value='after'))

    def test_exact_local_tags_allow_hostname_change_and_cached_snapshots(self):
        with patch.object(Tmux, 'snapshot', return_value=({'%1': self.pane}, None)) as snapshot:
            cache = {}
            for _ in range(2):
                self.assertEqual(observe(self.properties, self.runtime, cache)['live'], 'alive')
            snapshot.assert_called_once()
            self.assertEqual(owned(self.properties, self.runtime)[1], '%1')

    def test_each_wrong_or_missing_tag_refuses_control(self):
        for field in ('@mv2_agent_id', '@mv2_universe_id', '@mv2_launch_id'):
            for value in ('different', None):
                pane = {**self.pane, field: value}
                with self.subTest(field=field, value=value), \
                        patch.object(Tmux, 'snapshot', return_value=({'%1': pane}, None)):
                    self.assertEqual(observe(self.properties, self.runtime)['live'], 'stopped')
                    with self.assertRaises(ValueError):
                        owned(self.properties, self.runtime)

    def test_missing_local_pane_is_stopped_after_hostname_change(self):
        for snapshot in (({}, None), ({'%2': self.pane}, None)):
            with self.subTest(snapshot=snapshot), patch.object(Tmux, 'snapshot', return_value=snapshot):
                result = observe(self.properties, self.runtime)
                self.assertEqual(result, {'live': 'stopped', 'reason': 'Recorded pane is absent'})
                with self.assertRaises(ValueError):
                    owned(self.properties, self.runtime)

    def test_observation_errors_remain_unknown_with_the_actual_reason(self):
        for reason in ('Operation not permitted', 'tmux observation timed out'):
            snapshot = ({}, reason)
            with self.subTest(snapshot=snapshot), patch.object(Tmux, 'snapshot', return_value=snapshot):
                self.assertEqual(observe(self.properties, self.runtime), {'live': 'unknown', 'reason': reason})
                with self.assertRaises(ValueError):
                    owned(self.properties, self.runtime)

    def test_verified_dead_pane_is_stopped(self):
        with patch.object(Tmux, 'snapshot', return_value=({'%1': {**self.pane, 'pane_dead': '1'}}, None)):
            self.assertEqual(observe(self.properties, self.runtime)['live'], 'stopped')
            with self.assertRaises(ValueError):
                owned(self.properties, self.runtime)
