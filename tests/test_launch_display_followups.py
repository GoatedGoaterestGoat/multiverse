"""Launch publication races affect display, never terminal authority."""
from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import patch

from multiverse import display
from multiverse.runtime import lifecycle, native, missing_runtime
from multiverse.store import lock, read, uid, write
from multiverse.terminal.tmux import Tmux
from multiverse.tools import list_agents
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

    def test_publication_stages_read_as_starting_never_damaged_or_absent(self):
        """Each stage of a launch's publication, a cached absence and an unknown server read as starting, alive or
        unknown with their reason, never damaged or absent; fragile because the snapshot and the record are
        written at different moments, and a starting agent shown as absent or damaged invites removing or
        relaunching it (fbbdfe2)."""
        directory, _, pane = self.record(1)
        for snapshot in ({}, {'%1': {**pane, '@mv2_launch_id': ''}}):
            with self.subTest(stage='terminal published before its tags', snapshot=snapshot), \
                    lock(directory / '.launch.lock'), patch.object(Tmux, 'snapshot', return_value=(snapshot, None)):
                row = lifecycle.listing(self.root)[0]
                self.assertNotIn('damaged', row)
                self.assertEqual(display.live_label(row), 'starting', row)
                self.assertEqual(list_agents.cli_run(self.root, json=True)[0]['live'], 'starting')
                self.assertIn('starting', list_agents.run(self.root))

        directory, _, _ = self.record(1, terminal=False)
        with self.subTest(stage='identity reread during reconciliation'), lock(directory / '.launch.lock'), \
                patch.object(native, 'session_snapshot', side_effect=ValueError('identity updating')):
            row = lifecycle.listing(self.root)[0]
            self.assertEqual(display.live_label(row), 'starting', row)

        for boundary in ('initial', 'after reconciliation'):
            for publication in ('terminal', 'still missing', 'changed but damaged', 'different launch'):
                with self.subTest(boundary=boundary, publication=publication):
                    directory, stale, pane = self.record(1, terminal=False)
                    latest = dict(stale)
                    if publication in ('terminal', 'different launch'):
                        latest['tmux'] = {'socket': self.socket, 'pane': '%1', 'session': '$1'}
                    if publication == 'changed but damaged':
                        latest['status'] = 'working'
                    if publication == 'different launch':
                        latest['launch_id'] = uid()
                    calls = []
                    original_damage = missing_runtime.damage

                    def publish_before_damage(agent_dir, facts):
                        calls.append(dict(facts))
                        if len(calls) == (1 if boundary == 'initial' else 2):
                            # The reader has the old runtime; publication completes before
                            # its sidecar probe. Both writes follow the real launch order.
                            write(directory / 'runtime.json', latest)
                            write(directory / 'launches' / stale['launch_id'] / 'request.json', {})
                        return original_damage(agent_dir, facts)

                    with patch.object(missing_runtime, 'damage', side_effect=publish_before_damage), \
                            patch.object(native, 'session_snapshot', side_effect=lambda root, directory, props, facts: (props, facts)), \
                            patch.object(Tmux, 'snapshot', return_value=({'%1': pane}, None)):
                        row = lifecycle.listing(self.root)[0]
                    if publication == 'terminal':
                        self.assertNotIn('damaged', row)
                        self.assertEqual(row['live'], 'alive', row)
                        self.assertEqual(row['runtime']['tmux'], latest['tmux'])
                    else:
                        self.assertIn('tmux terminal identity is missing', row['damaged']['reason'])
                    self.assertEqual(read(directory / 'runtime.json'), latest, 'Listing must not repair authority')

        directory, _, _ = self.record(1)
        with self.subTest(stage='unknown server, then an abandoned publisher'):
            with lock(directory / '.launch.lock'), \
                    patch.object(Tmux, 'snapshot', return_value=({}, 'Permission denied')):
                row = lifecycle.listing(self.root)[0]
            self.assertEqual(display.live_label(row), 'unknown')
            self.assertIn('Permission denied', row['reason'])
            with patch.object(Tmux, 'snapshot', return_value=({}, None)):
                row = lifecycle.listing(self.root)[0]
            self.assertEqual(display.live_label(row), 'dormant')

        _, _, first = self.record(1, state='running')
        _, _, second = self.record(2, state='running')
        with self.subTest(stage='a new pane after an older agent was cached absent'), \
                patch.object(Tmux, 'snapshot', side_effect=[({'%1': first}, None),
                    ({'%1': first, '%2': second}, None)]) as snapshots:
            rows = lifecycle.listing(self.root)
            self.assertEqual([row['live'] for row in rows], ['alive', 'alive'])
            self.assertEqual(snapshots.call_count, 2)


class ConcurrentLaunchDisplayTest(TerminalBase):
    def test_concurrent_fake_spawns_never_flash_absent_or_unknown(self):
        """Concurrent real spawns never show absent or unknown to a reader polling meanwhile; fragile because it is
        a publication race only real concurrency exposes (fbbdfe2)."""
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
