"""Launch publication races affect display, never terminal authority."""
from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import patch

from multiverse import display
from multiverse.runtime import lifecycle, native
from multiverse.store import lock, read, uid, write
from multiverse.terminal.tmux import Tmux
from multiverse.tools import list_agents
from multiverse.universe import records
from test_multiverse import BaseTest, TerminalBase


class LaunchDisplayFollowupTest(BaseTest):
    def setUp(self):
        super().setUp()
        write(self.root / 'harnesses/sleeper/harness.json',
              {'schema': 1, 'spawn': ['/bin/sleep', '600'], 'resume': ['/bin/sleep', '600']})
        self.socket = str(self.work / 'absent.sock')

    def record(self, number, *, state='starting', terminal=True):
        identity = f'00000000-0000-4000-8000-{number:012d}'
        directory = self.root / 'state/agents' / identity
        properties = {'id': identity, 'name': f'worker{number}', 'harness': 'sleeper',
                      'universe_id': read(self.root / 'universe.json')['id'], 'workspace': str(self.work)}
        facts = {'launch_id': uid(), 'state': state, 'status': 'unknown'}
        if terminal:
            facts['tmux'] = {'socket': self.socket, 'pane': f'%{number}', 'session': f'${number}'}
        write(directory / 'properties.json', properties)
        write(directory / 'runtime.json', facts)
        (directory / 'launches' / facts['launch_id']).mkdir(parents=True)
        pane = {'pane_id': f'%{number}', 'session_id': f'${number}', 'pane_dead': '0',
                '@mv2_agent_id': identity, '@mv2_universe_id': properties['universe_id'],
                '@mv2_launch_id': facts['launch_id'], 'pane_pid': str(number)}
        return directory, facts, pane

    def test_publisher_with_terminal_still_reads_starting_when_snapshot_predates_tags(self):
        directory, _, pane = self.record(1)
        for snapshot in ({}, {'%1': {**pane, '@mv2_launch_id': ''}}):
            with self.subTest(snapshot=snapshot), lock(directory / '.launch.lock'), \
                    patch.object(Tmux, 'snapshot', return_value=(snapshot, None)):
                row = lifecycle.listing(self.root)[0]
                self.assertNotIn('damaged', row)
                self.assertEqual(display.live_label(row), 'starting', row)
                self.assertEqual(list_agents.cli_run(self.root, json=True)[0]['live'], 'starting')
                self.assertIn('starting', list_agents.run(self.root))

    def test_new_running_pane_refreshes_an_older_agents_cached_absence(self):
        _, _, first = self.record(1, state='running')
        _, _, second = self.record(2, state='running')
        with patch.object(Tmux, 'snapshot', side_effect=[({'%1': first}, None),
                ({'%1': first, '%2': second}, None)]) as snapshots:
            rows = lifecycle.listing(self.root)
        self.assertEqual([row['live'] for row in rows], ['alive', 'alive'])
        self.assertEqual(snapshots.call_count, 2)

    def test_reconciliation_reread_keeps_same_launch_preparation_display(self):
        directory, _, _ = self.record(1, terminal=False)
        with lock(directory / '.launch.lock'), \
                patch.object(native, 'session_snapshot', side_effect=ValueError('identity updating')):
            row = lifecycle.listing(self.root)[0]
        self.assertEqual(display.live_label(row), 'starting', row)

    def test_unknown_server_and_abandoned_publisher_keep_their_diagnostics(self):
        directory, _, _ = self.record(1)
        with lock(directory / '.launch.lock'), patch.object(Tmux, 'snapshot', return_value=({}, 'Permission denied')):
            row = lifecycle.listing(self.root)[0]
        self.assertEqual(display.live_label(row), 'unknown')
        self.assertIn('Permission denied', row['reason'])
        with patch.object(Tmux, 'snapshot', return_value=({}, None)):
            row = lifecycle.listing(self.root)[0]
        self.assertEqual(display.live_label(row), 'dormant')

    def test_kill_damaged_pid_points_to_repair_without_recommending_itself(self):
        directory, facts, _ = self.record(1, state='stopped')
        write(directory / 'runtime.json', {**facts, 'pid': 'abc'})
        write(directory / 'launches' / facts['launch_id'] / 'request.json',
              {'env': {'MV2_TMUX_SOCKET': self.socket}})
        before = (directory / 'runtime.json').read_bytes()
        with patch.object(Tmux, 'snapshot', return_value=({}, None)), self.assertRaises(records.RecordError) as caught:
            lifecycle.kill(self.root, 'worker1')
        reason = str(caught.exception)
        self.assertIn('Restore', reason)
        self.assertIn('independently verified', reason)
        self.assertIn('remove the damaged runtime.json', reason)
        self.assertNotIn('use mv2 kill', reason)
        self.assertEqual((directory / 'runtime.json').read_bytes(), before)


class ConcurrentLaunchDisplayTest(TerminalBase):
    def test_concurrent_fake_spawns_never_flash_absent_or_unknown(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(lifecycle.start, self.root, name=f'new{number}', harness='fake',
                                   socket_path=self.socket, wait=True, wait_timeout=15)
                       for number in range(8)]
            deadline = time.monotonic() + 20
            sightings = 0
            while not all(future.done() for future in futures) and time.monotonic() < deadline:
                for row in lifecycle.listing(self.root):
                    sightings += 1
                    self.assertIn(display.live_label(row), ('starting', 'alive'), row)
                time.sleep(.01)
            self.assertGreater(sightings, 0)
            for future in futures:
                self.assertTrue(future.result(timeout=1)['ready'])
