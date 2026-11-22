"""Launch publication races affect display, never terminal authority."""
from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import patch

from multiverse import display, harnesses
from multiverse.harnesses.types import NativeResult
from multiverse.plugins.api import Agent
from types import SimpleNamespace
from multiverse.runtime import lifecycle, native, missing_runtime, runner
from multiverse.store import lock, read, uid, write
from multiverse.terminal.tmux import Tmux
from multiverse.tools import list_agents, runtime_update
from multiverse.universe import agents
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

    def test_activity_time_changes_only_when_the_harness_reports_a_new_status(self):
        """Usage and cleanup can write runtime after a harness goes idle and must never reset its activity age."""
        directory, facts, _ = self.record(1, state='running')
        path = directory / 'runtime.json'
        launch = facts['launch_id']
        first, later = '2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00'
        with patch.object(runtime_update, 'callback', return_value=(self.root, directory, launch)):
            with patch.object(agents, 'now', return_value=first):
                runtime_update.run(status='working')
            self.assertEqual(read(path)['status_changed_at'], first)
            with patch.object(agents, 'now', return_value=later):
                runtime_update.run(status='working', usage_json='{}')
                self.assertEqual(read(path)['observed_at'], later)
                self.assertEqual(read(path)['status_changed_at'], first)
                agents.update_runtime(directory, launch, status='unknown')
                runtime_update.run(status='working')
                self.assertEqual(read(path)['status'], 'working')
                self.assertEqual(read(path)['status_changed_at'], first)
                runner.StatusWatcher(directory, launch, self.root)._publish('idle')
            self.assertEqual(read(path)['status_changed_at'], later)
        agents.update_runtime(directory, launch, state='dormant', status='unknown')
        self.assertEqual(read(path)['status_changed_at'], later)

    def test_activity_uses_the_harness_report_time_even_during_a_deferred_stop(self):
        """A status reported with a prompt batch keeps its original time when a refused stop later applies it."""
        directory, facts, _ = self.record(1, state='running')
        path = directory / 'runtime.json'
        properties = directory / 'properties.json'
        write(properties, {**read(properties), 'native_session_id': 'conversation'})
        launch = facts['launch_id']
        waiting = '2026-01-01T00:00:00+00:00'
        reported = '2026-01-01T00:01:00+00:00'
        resumed = '2026-01-01T00:02:00+00:00'
        batch = {'prompt_events_json': '[]', 'prompt_session_id': 'conversation'}
        with patch.object(runtime_update, 'callback', return_value=(self.root, directory, launch)):
            with patch.object(agents, 'now', return_value=waiting):
                runtime_update.run(status='waiting', **batch)
            self.assertEqual(read(path)['status_changed_at'], waiting)
            agents.update_runtime(directory, launch, state='ending')
            with patch.object(agents, 'now', return_value=reported):
                runtime_update.run(status='working', **batch)
            self.assertEqual(read(path)['status'], 'waiting')
            self.assertEqual(read(path)['status_changed_at'], reported)
            with patch.object(agents, 'now', return_value=resumed):
                runtime_update.run(status='working', **batch)
                agents.mutate_runtime(directory, launch,
                                      lambda current: {'state': 'running', 'status': current['deferred_stop_status']})
            self.assertEqual(read(path)['status'], 'working')
            self.assertEqual(read(path)['observed_at'], resumed)
            self.assertEqual(read(path)['status_changed_at'], reported)

    def test_activity_listing_fields_use_live_status_and_a_readable_utc_time(self):
        """A dormant agent's saved working status and malformed or legacy timestamps must not masquerade as activity."""
        directory, facts, pane = self.record(1, state='running')
        path = directory / 'runtime.json'
        stored, utc = '2026-01-01T03:30:00+03:30', '2026-01-01T00:00:00+00:00'
        for live, status, timestamp, active, expected in (
                (True, 'working', stored, True, utc), (True, 'waiting', stored, False, utc),
                (False, 'working', stored, False, utc), (True, 'idle', '2026-01-01', False, None),
                (True, 'idle', 'broken', False, None), (True, 'idle', None, False, None)):
            with self.subTest(live=live, status=status, timestamp=timestamp):
                values = {**facts, 'status': status}
                if timestamp is not None:
                    values['status_changed_at'] = timestamp
                write(path, values)
                with patch.object(Tmux, 'snapshot', return_value=({'%1': pane} if live else {}, None)), \
                        patch.object(native, 'usage_snapshot', side_effect=AssertionError('No transcript read')):
                    fields = list_agents.cli_run(self.root, json=True, columns='active_now,status_changed_at')[0]
                    row = lifecycle.listing(self.root, usage=False)[0]
                self.assertEqual(fields, {'active_now': active, 'status_changed_at': expected})
                plugin = Agent(row, None)
                self.assertEqual({key: plugin.record[key] for key in fields}, fields)

    def test_listing_can_skip_usage_without_changing_identity_or_display(self):
        """A projected listing must not parse transcripts, and its allowed fields must cover healthy, damaged and preparing rows."""
        measured = {'state': 'ok', 'value': {'total_tokens': 123}}
        for state in ('healthy', 'damaged', 'preparing'):
            with self.subTest(state=state):
                directory, _, pane = self.record(1, state='running' if state == 'healthy' else 'starting',
                                                  terminal=state != 'preparing')
                if state == 'damaged':
                    (directory / 'properties.json').write_text('[]')
                with lock(directory / '.launch.lock'), \
                        patch.object(Tmux, 'snapshot', return_value=({'%1': pane}, None)), \
                        patch.object(native, 'usage_snapshot', return_value=measured) as reader:
                    full = lifecycle.listing(self.root)[0]
                    self.assertEqual(reader.call_count, 0 if state == 'damaged' else 1)
                    reader.reset_mock()
                    light = lifecycle.listing(self.root, usage=False)[0]
                    reader.assert_not_called()
                self.assertEqual(light, {key: value for key, value in full.items()
                                         if key not in lifecycle.USAGE_FIELDS})
                self.assertNotIn('observed_model_name', light)
                if state != 'damaged':
                    self.assertIsNone(full['observed_model_name'])
                self.assertLessEqual(full.keys(), lifecycle.ROW_FIELDS)
                if state == 'healthy':
                    self.assertEqual(full['usage'], measured)
                    self.assertEqual(full['tokens'], '123')
                elif state == 'damaged':
                    self.assertIn('damaged', light)
                else:
                    self.assertEqual(display.live_label(light), 'starting')

    def test_a_deferred_usage_read_never_reconciles_or_repairs_an_identity_error(self):
        """A usage thread may be left at exit only if it cannot append a session transition or replace error evidence."""
        directory, _, pane = self.record(1, state='running')
        with patch.object(Tmux, 'snapshot', return_value=({'%1': pane}, None)):
            row = lifecycle.listing(self.root, usage=False)[0]
        before = {name: (directory / name).read_bytes() for name in ('properties.json', 'runtime.json')}
        measured = NativeResult('ok', value={'total_tokens': 456})
        calls = []
        def usage(session):
            calls.append(session)
            return measured
        with patch.object(harnesses, 'load', return_value=SimpleNamespace(get_usage=usage)), \
                patch.object(native, 'session_snapshot', side_effect=AssertionError('usage must never reconcile')) as reconcile:
            result = lifecycle.row_usage(row)
            self.assertEqual(result['usage'], {**measured.to_dict(), 'value': {
                **measured.value, 'model': None, 'effort': None, 'observed_at': None,
            }})
            self.assertEqual(result['tokens'], '456')
            self.assertEqual(set(result), set(lifecycle.USAGE_FIELDS))
            self.assertIsNone(result['observed_model_name'])
            self.assertEqual(len(calls), 1)
            self.assertEqual(lifecycle.row_usage({**row, 'damaged': {'reason': 'broken'}}), {})
            error = lifecycle.row_usage({**row, 'identity_error': 'Cannot read the session journal'})
            self.assertEqual(error['usage']['state'], 'error')
            self.assertIn('Cannot read the session journal', error['usage_detail'])
            self.assertEqual(len(calls), 1)
            reconcile.assert_not_called()
        self.assertEqual({name: (directory / name).read_bytes() for name in before}, before)
        self.assertFalse((directory / 'sessions.jsonl').exists())
        with patch.object(Tmux, 'snapshot', return_value=({'%1': pane}, None)), \
                patch.object(native, 'session_snapshot', side_effect=ValueError('Cannot read the session journal')), \
                patch.object(native, 'usage_snapshot') as reader:
            for enabled in (False, True):
                failed = lifecycle.listing(self.root, usage=enabled)[0]
                self.assertEqual(failed['identity_error'], 'Cannot read the session journal')
                self.assertLessEqual(failed.keys(), lifecycle.ROW_FIELDS)
            reader.assert_not_called()

    def test_absent_panes_share_one_server_refresh_per_listing(self):
        """An abandoned pane per agent must not trigger fresh server recovery and process queries for every row."""
        for number in range(1, 11):
            self.record(number, state='running')
        with patch.object(Tmux, 'snapshot', return_value=({}, None)) as snapshot:
            rows = lifecycle.listing(self.root, usage=False)
            self.assertEqual([row['live'] for row in rows], ['dormant'] * 10)
            self.assertEqual(snapshot.call_count, 2)
            lifecycle.listing(self.root, usage=False)
            self.assertEqual(snapshot.call_count, 4)  # no cache survives into the next listing

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

    def test_listing_handles_both_sidecars_overtaking_their_runtime_snapshot(self):
        """Registration and request publication can each invalidate an earlier snapshot in the same listing."""
        directory, starting, pane = self.record(1, terminal=False)
        path = directory / 'runtime.json'
        launch = directory / 'launches' / starting['launch_id']
        preparing = {**starting, 'preparing': True}
        starting = {**starting, 'host': 'fixture'}
        terminal = {**starting, 'tmux': {'socket': self.socket, 'pane': '%1', 'session': '$1'}}
        write(path, preparing)

        original_preparation = agents.preparation_snapshot
        original_damage = missing_runtime.damage

        def prepare(agent_dir, facts):
            if facts == preparing:
                write(path, starting)
                write(launch / 'registration.json', {})
            return original_preparation(agent_dir, facts)

        def damage(agent_dir, facts):
            if facts.get('host') == 'fixture' and 'tmux' not in facts:
                write(path, terminal)
                write(launch / 'request.json', {})
            return original_damage(agent_dir, facts)

        with lock(directory / '.launch.lock'), \
                patch.object(agents, 'preparation_snapshot', side_effect=prepare), \
                patch.object(missing_runtime, 'damage', side_effect=damage), \
                patch.object(Tmux, 'snapshot', return_value=({'%1': pane}, None)):
            row = lifecycle.listing(self.root, usage=False)[0]
        self.assertNotIn('damaged', row)
        self.assertEqual(row['live'], 'alive', row)
        self.assertEqual(read(path), terminal)


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
