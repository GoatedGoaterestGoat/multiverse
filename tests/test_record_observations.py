"""Fleet observations share missing-file waits without lending absence to action paths."""
from collections import Counter
from contextlib import contextmanager, redirect_stdout
import errno
import io
import sys
import json
import os
from pathlib import Path
import shutil
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest
from multiverse import store
from multiverse.harnesses.types import ObservedBinding
from multiverse.runtime import lifecycle, native, slash
from multiverse.tools import doctor
from multiverse.universe import agents, mailbox, sessions


class RecordObservationsTest(BaseTest):
    def setUp(self):
        super().setUp()
        # A real configured adapter with no executable/native queries; no session is launched.
        shutil.rmtree(self.root / 'harnesses')
        store.write(self.root / 'harnesses' / 'fixture' / 'harness.json',
                    {'schema': 1, 'spawn': ['unused-fixture'], 'resume': ['unused-fixture']})

    def agent(self, name='worker'):
        directory = self.root / 'state' / 'agents' / str(uuid4())
        properties = {'id': directory.name, 'name': name, 'harness': 'fixture', 'workspace': str(self.work),
                      'universe_id': store.read(self.root / 'universe.json')['id'],
                      'native_session_id': str(uuid4()), 'native_session_home': str(self.work)}
        facts = {'launch_id': str(uuid4()), 'state': 'stopped', 'status': 'idle', 'exit_code': 0}
        store.write(directory / 'properties.json', properties)
        store.write(directory / 'runtime.json', facts)
        return directory, properties, facts

    def row(self, properties, facts, target=None):
        return {'schema': 1, 'id': str(uuid4()), 'at': store.now(), 'harness': properties['harness'],
                'launch': facts['launch_id'], 'reason': 'detected', 'home': str(self.work),
                'from': properties['native_session_id'], 'to': target or str(uuid4())}

    @contextmanager
    def clock(self):
        elapsed, sleeps = [0.], []
        self.wait_stacks = []

        def sleep(delay):
            sleeps.append(delay)
            self.wait_stacks.append("".join(traceback.format_stack(limit=12)))
            elapsed[0] += delay

        with patch.object(store, 'time', SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep)):
            yield elapsed, sleeps

    def test_one_retry_window_covers_one_twenty_or_two_hundred_absent_records(self):
        """Permanent optional absence must cost one retry window, rather than making fleet refresh time proportional to rows."""
        original = Path.stat
        for count in (1, 20, 200):
            paths = [self.work / 'missing' / str(index) for index in range(count)]
            calls = Counter()

            def stat(path, *args, **kwargs):
                calls[path] += 1
                return original(path, *args, **kwargs)

            with self.subTest(count=count), self.clock() as (_, sleeps), patch.object(Path, 'stat', stat):
                self.assertEqual(store.missing_records(paths + paths), frozenset(paths))
            self.assertEqual(sleeps, [store.MISSING_DELAY] * (store.MISSING_ATTEMPTS - 1))
            self.assertEqual(set(calls.values()), {store.MISSING_ATTEMPTS})

    def test_batch_retries_only_missing_paths_and_preserves_unreadable_evidence(self):
        """A transient gap must recover, while permissions and other stat errors must reach the normal reader instead of becoming absence."""
        transient, absent, denied, wrong_parent, present = [self.work / name for name in
                                                         ('transient', 'absent', 'denied', 'wrong-parent', 'present')]
        for path in (transient, present):
            path.write_text('{}')
        original, calls = Path.stat, Counter()

        def stat(path, *args, **kwargs):
            calls[path] += 1
            number = (errno.ENOENT if path == transient and calls[path] <= 2 or path == absent else
                      errno.EACCES if path == denied else errno.ENOTDIR if path == wrong_parent else None)
            if number:
                raise OSError(number, 'fixture error', str(path))
            return original(path, *args, **kwargs)

        with self.clock(), patch.object(Path, 'stat', stat):
            self.assertEqual(store.missing_records((transient, absent, denied, wrong_parent, present)), frozenset({absent}))
        self.assertEqual(calls[transient], 3)
        self.assertEqual(calls[absent], store.MISSING_ATTEMPTS)
        self.assertTrue(all(calls[path] == 1 for path in (denied, wrong_parent, present)))

    def test_retry_budget_starts_after_the_first_round_and_bounds_later_delays(self):
        """A large initial inventory must not consume the retry budget before any missing path can be checked again."""
        paths = [self.work / str(index) for index in range(20)]
        calls = Counter()
        with self.clock() as (elapsed, sleeps):
            def stat(path, *args, **kwargs):
                calls[path] += 1
                if calls[path] == 1:
                    elapsed[0] += 1
                raise FileNotFoundError(errno.ENOENT, 'missing', str(path))

            with patch.object(Path, 'stat', stat), patch.object(store, 'MISSING_BUDGET', .012):
                self.assertEqual(store.missing_records(paths), frozenset(paths))
            self.assertEqual(len(sleeps), 3)
            self.assertAlmostEqual(sum(sleeps), .012)
            self.assertEqual(set(calls.values()), {4})

    def test_real_fifteen_millisecond_rename_gap_is_not_observed_as_absence(self):
        """The batch must retain protection for a real rename-aside gap, not just mocked ENOENT exceptions."""
        path, aside = self.work / 'record.json', self.work / 'aside.json'
        store.write(path, {'value': 'intact'})
        os.replace(path, aside)
        errors = []

        def restore():
            try:
                os.replace(aside, path)
            except OSError as error:
                errors.append(error)

        worker = threading.Timer(.015, restore)
        worker.start()
        try:
            self.assertEqual(store.missing_records((path, self.work / 'absent.json')),
                             frozenset({self.work / 'absent.json'}))
        finally:
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(store.read(path), {'value': 'intact'})

    def test_a_present_ledger_that_later_goes_missing_uses_its_normal_retry(self):
        """Preflight presence cannot shorten retries for an atomically replaced ledger that disappears only at its read."""
        directory, _, _ = self.agent()
        path = directory / 'inbox-state.json'
        state = {'saved': {'submission_state': 'accepted'}}
        store.write(path, state)
        observed = store.missing_records((path, directory / 'slash' / 'pending.json'))
        self.assertNotIn(path, observed)
        original, attempts = store.open_regular, []

        def opening(candidate, *args, **kwargs):
            if candidate == path:
                attempts.append(candidate)
                if len(attempts) <= 2:
                    raise FileNotFoundError(errno.ENOENT, 'replacement gap', str(path))
            return original(candidate, *args, **kwargs)

        with self.clock(), patch.object(store, 'open_regular', opening):
            self.assertEqual(mailbox._ledger(path, observed_missing=observed), state)
        self.assertEqual(len(attempts), 3)
        path.unlink()
        with self.clock():
            self.assertEqual(mailbox._ledger(path, observed_missing=observed), {})

    def test_default_session_readers_do_not_inherit_a_diagnostic_absence(self):
        """A later session claim/recovery must read a newly published lineage even after a diagnostic saw no journal."""
        directory, properties, facts = self.agent()
        path = directory / 'sessions.jsonl'
        with self.clock():
            observed = store.missing_records((path,))
        row = self.row(properties, facts)
        path.write_text(json.dumps(row) + '\n')
        self.assertEqual(sessions.effective(directory, properties, observed_missing=observed), (properties, []))
        current, rows = sessions.effective(directory, properties)
        self.assertEqual(rows, [row])
        self.assertEqual(current['native_session_id'], row['to'])
        recovered, _ = sessions.recover(directory, properties)
        self.assertEqual(recovered['native_session_id'], row['to'])
        self.assertEqual(store.read(directory / 'properties.json')['native_session_id'], row['to'])

    def test_locked_native_recheck_discards_observation_before_lineage_or_slash_writes(self):
        """A late journal or slash index must be reread under the launch lock before diagnostic reconciliation can write."""
        for already_locked in (False, True):
            with self.subTest(already_locked=already_locked):
                directory, properties, facts = self.agent(str(already_locked))
                journal, pending = directory / 'sessions.jsonl', directory / 'slash' / 'pending.json'
                with self.clock():
                    observed = store.missing_records((journal, pending))
                row = self.row(properties, facts)
                adapter = SimpleNamespace(name='fixture', observed_binding=lambda session:
                    ObservedBinding(native_id=row['to'], home=str(self.work), evidence='fixture'))

                def publish():
                    journal.write_text(json.dumps(row) + '\n')
                    store.write(pending, {'ids': [str(uuid4())]})

                @contextmanager
                def snapshot_lock(selected):
                    self.assertEqual(selected, directory)
                    publish()
                    with store.lock(directory / '.launch.lock', create_parent=False):
                        yield

                if already_locked:
                    publish()
                with patch.object(native, '_snapshot_lock', snapshot_lock), \
                        patch.object(sessions, 'effective', wraps=sessions.effective) as effective, \
                        patch.object(slash, 'reconcile', side_effect=lambda root, selected, props, runtime, **kw: props) as reconcile, \
                        patch.object(agents, '_rebind_session', side_effect=AssertionError('Repaired lineage must precede rebind')):
                    current, _ = native.session_snapshot(self.root, directory, properties, facts, adapter=adapter,
                                                          observed_missing=observed, launch_locked=already_locked)
                self.assertEqual(current['native_session_id'], row['to'])
                reconcile.assert_called_once()
                self.assertEqual(reconcile.call_args.args[2]['native_session_id'], row['to'])
                snapshots = [call.kwargs.get('observed_missing', ()) for call in effective.call_args_list]
                self.assertEqual(snapshots[0], () if already_locked else observed)
                self.assertTrue(all(not snapshot for snapshot in snapshots[1:]))

    def test_native_snapshot_without_a_batch_keeps_fresh_optional_checks(self):
        """Control callers that do not request diagnostic batching must retain retries for sessions and slash metadata."""
        directory, properties, facts = self.agent()
        (directory / 'slash').mkdir()
        with self.clock() as (_, sleeps):
            self.assertEqual(native.session_snapshot(self.root, directory, properties, facts,
                                                      adapter=SimpleNamespace(name='fixture')), (properties, facts))
        self.assertEqual(len(sleeps), 2 * (store.MISSING_ATTEMPTS - 1))

    def test_known_record_helpers_distinguish_missing_parent_from_a_file_gap(self):
        """A missing container needs no per-file wait, but a missing name in an existing container must retain replacement retries."""
        for helper in (store.open_replaced, store.exists_replaced, store.is_file_replaced):
            with self.subTest(helper=helper.__name__):
                path = self.work / helper.__name__ / 'record.json'
                with self.clock() as (_, sleeps):
                    if helper is store.open_replaced:
                        with self.assertRaises(FileNotFoundError):
                            helper(path)
                    else:
                        self.assertFalse(helper(path))
                self.assertEqual(sleeps, [])

                path.parent.mkdir()
                with self.clock() as (_, sleeps):
                    if helper is store.open_replaced:
                        with self.assertRaises(FileNotFoundError):
                            helper(path)
                    else:
                        self.assertFalse(helper(path))
                self.assertEqual(len(sleeps), store.MISSING_ATTEMPTS - 1)

                # Publish after two genuine missing-name attempts, without hiding the parent.
                with self.clock() as (_, sleeps):
                    clock_sleep = store.time.sleep

                    def publish(delay):
                        clock_sleep(delay)
                        if len(sleeps) == 2:
                            store.write(path, {'intact': True})

                    store.time.sleep = publish
                    result = helper(path)
                    if helper is store.open_replaced:
                        with result:
                            self.assertEqual(json.load(result), {'intact': True})
                    else:
                        self.assertTrue(result)
                self.assertEqual(len(sleeps), 2)

    def test_parent_probe_errors_do_not_turn_a_record_gap_into_early_absence(self):
        """Unreadable or malformed parent probes cannot corroborate absence or replace the original reader error."""
        path = self.work / 'record.json'
        original = Path.stat
        for number in (errno.EACCES, errno.ENOTDIR):
            with self.subTest(errno=number):
                error = FileNotFoundError(errno.ENOENT, 'record gap', str(path))

                def stat(candidate, *args, **kwargs):
                    if candidate == path.parent:
                        raise OSError(number, 'parent probe failed', str(candidate))
                    return original(candidate, *args, **kwargs)

                with self.clock() as (_, sleeps), patch.object(Path, 'stat', stat):
                    with self.assertRaises(FileNotFoundError) as caught:
                        store.retry_missing(lambda: (_ for _ in ()).throw(error), path=path)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(sleeps), store.MISSING_ATTEMPTS - 1)

    def test_real_fleet_listing_and_doctor_share_optional_waits(self):
        """Real native snapshot traversal must not reintroduce per-agent delays after the diagnostic batch."""
        real_sleep = time.sleep
        baseline, previous = {}, 0
        for count in (1, 20, 200):
            for index in range(previous, count):
                directory, _, facts = self.agent('worker-' + str(index))
                # Exercise both normal stopped rows and legacy rows without a prepared launch folder.
                if index % 2:
                    store.write(directory / 'runtime.json', {**facts, 'tmux': {
                        'socket': str(self.work / 'private-fixture.sock'), 'session': '$1', 'pane': '%' + str(index)}})
            previous = count
            for name, operation in (('listing', lambda: lifecycle.listing(self.root)),
                                    ('doctor', lambda: doctor.check(self.root))):
                waits = []

                def sleeping(delay):
                    waits.append((delay, ''.join(traceback.format_stack(limit=8))))
                    real_sleep(delay)

                started = time.monotonic()
                with patch.object(store.time, 'sleep', side_effect=sleeping), \
                        patch('multiverse.terminal.tmux.Tmux._run', side_effect=AssertionError('No terminal query expected')), \
                        patch('multiverse.terminal.tmux.Tmux.snapshot', return_value=({}, None)), \
                        patch.object(lifecycle, 'missing_records', wraps=store.missing_records) as listing_batch, \
                        patch.object(doctor, 'missing_records', wraps=store.missing_records) as doctor_batch:
                    result = operation()
                measurement = {'elapsed': round(time.monotonic() - started, 4),
                               'sleeps': len(waits), 'delay': round(sum(item[0] for item in waits), 4)}
                print(f'{count} stopped fixture rows {name}:', json.dumps(measurement, sort_keys=True), flush=True)
                self.assertEqual(len(result), count if name == 'listing' else 0, result)
                self.assertEqual(listing_batch.call_count, 1 if name == 'listing' else 0)
                self.assertEqual(doctor_batch.call_count, 1 if name == 'doctor' else 0)
                batch_sleeps = sum('in missing_records' in stack for _, stack in waits)
                self.assertEqual(batch_sleeps, store.MISSING_ATTEMPTS - 1)
                if name == 'listing':
                    self.assertEqual(len(waits), batch_sleeps)
                # Doctor's constant plugin configuration checks remain fresh authority reads.
                if count == 1:
                    baseline[name] = len(waits)
                self.assertLessEqual(len(waits), baseline[name],
                                     '\n'.join(stack for _, stack in waits[baseline[name]:baseline[name] + 4]))

    def test_diagnostic_paths_follow_only_the_requested_universe_topology(self):
        """Listing metadata must not scan other universes or mistake retained launch artifacts for subverses."""
        from multiverse import cli
        child = self.root / 'subverses' / 'child'
        grandchild = child / 'subverses' / 'nested'
        artifact = child / 'state' / 'agents' / store.uid() / 'launches' / store.uid() / 'artifact'
        for scope in (child, grandchild, artifact):
            store.write(scope / 'universe.json', {'schema': 1, 'id': store.uid(), 'workspace': str(self.work)})
        local = cli._diagnostic_paths(self.root)
        self.assertIn(self.root / 'plugins.json', local)
        self.assertFalse(any(path == child or child in path.parents for path in local))
        recursive = cli._diagnostic_paths(self.root, recursive=True)
        self.assertIn(child / 'plugins.json', recursive)
        self.assertIn(grandchild / 'plugins.json', recursive)
        self.assertFalse(any(path == artifact or artifact in path.parents for path in recursive))
        for index in range(20):
            (self.root / 'subverses' / ('abandoned-' + str(index))).mkdir()
        marker, original, attempts = child / 'universe.json', Path.stat, []

        def stat(path, *args, **kwargs):
            if path == marker:
                attempts.append(path)
                if len(attempts) <= 2:
                    raise FileNotFoundError(errno.ENOENT, 'replacement gap', str(path))
            return original(path, *args, **kwargs)

        with self.clock() as (_, sleeps), patch.object(Path, 'stat', stat):
            recovered = cli._diagnostic_paths(self.root, recursive=True)
        self.assertEqual(len(sleeps), store.MISSING_ATTEMPTS - 1)
        self.assertIn(child / 'plugins.json', recovered)
        self.assertIn(grandchild / 'plugins.json', recovered)
        # Even within an unrelated diagnostic snapshot, a new topology read recovers a newly published marker.
        from multiverse import universe
        attempts.clear()
        with store.observations([marker]), self.clock() as (_, sleeps), patch.object(Path, 'stat', stat):
            self.assertEqual(universe.children(self.root), [child])
        self.assertEqual(len(sleeps), store.MISSING_ATTEMPTS - 1)

    def test_cli_diagnostics_share_one_window_and_never_lend_it_to_authority(self):
        """Diagnostics share absent metadata even on cold plugin loads, while bookkeeping and later authority reads remain fresh."""
        from multiverse import cli
        from multiverse.plugins import config, loader, trust
        from multiverse.universe import records
        for index in range(20):
            self.agent('cli-' + str(index))
        (self.root / 'plugins.json').unlink(missing_ok=True)
        trust.user_directory().mkdir(parents=True, exist_ok=True)
        (trust.user_directory() / loader.LOAD_TIMEOUTS).unlink(missing_ok=True)
        for command in ('list', 'doctor'):
            loader.reset()
            for cold in (True, False):
                with self.subTest(command=command, cold=cold), self.clock() as (_, sleeps), \
                        patch.object(sys, 'argv', ['mv2', '-u', str(self.work), command, '--json']), \
                        patch('multiverse.terminal.tmux.Tmux._run', side_effect=AssertionError('No terminal query')), \
                        patch('multiverse.terminal.tmux.Tmux.snapshot', return_value=({}, None)), redirect_stdout(io.StringIO()):
                    cli.main()
                bookkeeping = sum('in _remember_slow' in stack for stack in self.wait_stacks)
                self.assertEqual(len(sleeps) - bookkeeping, store.MISSING_ATTEMPTS - 1,
                                 "\n".join(stack for stack in self.wait_stacks if 'in _remember_slow' not in stack))
                if command == 'doctor' and cold:
                    self.assertGreater(bookkeeping, 0)  # The real first load still checks its success bookkeeping.
                if not cold:
                    self.assertEqual(bookkeeping, 0)
        path = self.root / 'plugins.json'
        with self.clock():
            missing = store.missing_records([path])
        with store.observations(missing), self.clock() as (_, sleeps):
            self.assertFalse(config.load(self.root, observed=True).exists)
            self.assertEqual(sleeps, [])
            store.write(path, {'schema': 1, 'enabled': {'fixture': False}})
            self.assertTrue(config.load(self.root, observed=True).exists)  # Read first, even in a diagnostic scope.
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                store.retry_missing(path.read_text)  # Default readers never borrow diagnostic absence.
            self.assertEqual(len(sleeps), store.MISSING_ATTEMPTS - 1)
        with self.clock() as (_, sleeps):
            self.assertFalse(config.load(self.root).exists)
        self.assertEqual(len(sleeps), store.MISSING_ATTEMPTS - 1)  # Nothing escapes the operation.
        found = SimpleNamespace(on=True, name='fixture', source='bundled',
                                manifest=SimpleNamespace(default='on', access=SimpleNamespace(request='act')))
        with store.observations([path]), self.clock() as (_, sleeps):
            real_sleep = store.time.sleep
            def revoke(delay):
                real_sleep(delay)
                if len(sleeps) == 2:
                    store.write(path, {'schema': 1, 'enabled': {'fixture': False}})
            store.time.sleep = revoke
            self.assertIsNone(loader.access_level(self.root, found))
        self.assertEqual(len(sleeps), 2)  # Permission/dispatch checks read through a newly published revocation.
        from multiverse.plugins import events
        # No manifest can subscribe in this fixture: an absent switch cannot affect event routing.
        with self.clock() as (_, sleeps):
            self.assertEqual(events.subscribers(self.root, 'message.saved'), [])
        self.assertEqual(sleeps, [])
        directory, _, facts = self.agent('authority')
        runtime = directory / 'runtime.json'
        runtime.unlink()
        with store.observations([runtime]), self.clock() as (_, sleeps):
            real_sleep = store.time.sleep
            def publish(delay):
                real_sleep(delay)
                if len(sleeps) == 2:
                    store.write(runtime, facts)
            store.time.sleep = publish
            self.assertEqual(records.runtime(runtime, required=True), facts)
        self.assertEqual(len(sleeps), 2)
