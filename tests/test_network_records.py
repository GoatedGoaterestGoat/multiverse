"""Transient filesystem absence must not erase durable authority or optional record state."""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from multiverse import store, universe
from multiverse.runtime import lifecycle, missing_runtime, native, slash, delivery_evidence, attachments, runs
from multiverse.tools import _common
from multiverse.universe import agents, records, focus, mailbox, sessions, templates, blocked, agent_hints, catalog
from test_multiverse import BaseTest


@contextmanager
def missing_once(path, *, probe='open', failures=1, recurring=False):
    """Miss a chosen pathname without removing its data or changing another reader."""
    path, seen = Path(path), []
    if probe == 'open':
        owner, name = os, 'open'
    else:
        owner, name = Path, 'open' if probe == 'path_open' else probe
    original = getattr(owner, name)

    def call(value, *args, **kwargs):
        if not isinstance(value, int) and Path(value) == path:
            seen.append(value)
            if ((len(seen) - 1) % (failures + 1) < failures if recurring else len(seen) <= failures):
                raise FileNotFoundError(errno.ENOENT, 'fixture replacement gap', str(path))
        return original(value, *args, **kwargs)
    with patch.object(owner, name, call), patch.object(store, 'MISSING_DELAY', 0):
        yield seen


class ReplacedRecordsTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state' / 'agents' / store.uid()
        self.directory.mkdir()
        self.launch = store.uid()
        self.properties = {'id': self.directory.name, 'name': 'reader', 'harness': 'codex',
                           'universe_id': store.read(self.root / 'universe.json')['id'], 'workspace': str(self.work)}
        self.facts = {'launch_id': self.launch, 'state': 'running', 'status': 'working',
                      'tmux': {'socket': str(self.work / 'test.sock'), 'session': '$1', 'pane': '%1'}}
        store.write(self.directory / 'properties.json', self.properties)
        store.write(self.directory / 'runtime.json', self.facts)

    def test_retry_bounds_and_only_missing_errors(self):
        """Only ENOENT may delay a reader; bounded exhaustion must retain its original failure."""
        for number in (errno.EACCES, errno.EIO, errno.ENOTDIR, errno.ELOOP):
            with self.subTest(errno=number):
                operation = Mock(side_effect=OSError(number, 'fixture'))
                with self.assertRaises(OSError) as raised:
                    store.retry_missing(operation)
                self.assertEqual(raised.exception.errno, number)
                self.assertEqual(operation.call_count, 1)
        for number in (errno.ENOTDIR, errno.EBADF, errno.ELOOP):
            for predicate in (store.exists_replaced, store.is_file_replaced):
                with self.subTest(predicate=predicate.__name__, errno=number), \
                        patch.object(Path, 'stat', side_effect=OSError(number, 'fixture')) as checked:
                    self.assertFalse(predicate(self.work / 'not-a-file'))
                    self.assertEqual(checked.call_count, 1)
        missing = FileNotFoundError(errno.ENOENT, 'fixture')
        with patch.object(store, 'MISSING_DELAY', 0):
            operation = Mock(side_effect=missing)
            with self.assertRaises(FileNotFoundError) as raised:
                store.retry_missing(operation)
            self.assertIs(raised.exception, missing)
            self.assertEqual(operation.call_count, 5)
        operation = Mock(side_effect=missing)
        with patch.object(store.time, 'monotonic', side_effect=[10, 11]), patch.object(store.time, 'sleep') as sleep:
            with self.assertRaises(FileNotFoundError):
                store.retry_missing(operation)
        self.assertEqual(operation.call_count, 1)
        sleep.assert_not_called()
        # Generic reads remain a single attempt; only the audited readers opt in.
        path = self.work / 'generic.json'
        store.write(path, {})
        with missing_once(path, probe='read_text'), self.assertRaises(FileNotFoundError):
            store.read(path)

    def test_runtime_callbacks_and_launch_guards_retain_authority(self):
        """A replace gap must neither reject a current callback nor authorize a second launch or wake."""
        runtime = self.directory / 'runtime.json'
        properties = self.directory / 'properties.json'
        for path, read, expected in ((runtime, lambda: records.runtime(runtime), self.facts),
                                     (properties, lambda: records.properties(properties), self.properties)):
            with self.subTest(path=path.name), missing_once(path, failures=2):
                self.assertEqual(read(), expected)
        with patch.object(Path, 'exists', side_effect=AssertionError('record reads must open directly')):
            self.assertEqual(records.runtime(runtime), self.facts)
        with missing_once(runtime, failures=2), patch.dict(os.environ, {'MV2_LAUNCH_ID': self.launch}), \
                patch('multiverse.runtime.agent_identity.actor', return_value=None):
            self.assertEqual(_common.callback(self.root, self.directory.name)[2], self.launch)
        before = runtime.read_bytes()
        for permanent in (False, True):
            with self.subTest(permanent=permanent):
                if permanent:
                    runtime.unlink()
                launch = self.directory / 'launches' / self.launch
                store.write(launch / 'request.json', {'env': {}})
                with missing_once(runtime, failures=2 if not permanent else 0, recurring=True), \
                        patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                        patch.object(missing_runtime, 'discover', return_value=[('tagged',)]), \
                        patch.object(lifecycle.shutil, 'which', return_value='/fixture/tmux'), \
                        patch.object(native, 'start_shared_service', side_effect=AssertionError('no native start')), \
                        patch.object(lifecycle.Tmux, 'create', side_effect=AssertionError('no second pane')):
                    with self.assertRaisesRegex(ValueError, 'duplicate launch'):
                        lifecycle.start(self.root, resume='reader', socket_path=str(self.work / 'test.sock'))
                if permanent:
                    with patch.dict(os.environ, {'MV2_LAUNCH_ID': self.launch}), \
                            patch('multiverse.runtime.agent_identity.actor', return_value=None), \
                            self.assertRaisesRegex(records.RecordError, 'missing after retrying'):
                        _common.callback(self.root, self.directory.name)
                    message = mailbox.append_message(self.directory, 'wake', {'name': 'user'}, 'follow-up')
                    with patch.object(missing_runtime, 'discover', return_value=[('tagged',)]), \
                            patch.object(lifecycle, 'start', side_effect=AssertionError('no duplicate wake')):
                        receipt = native.send_or_wake(self.root, 'reader', message['id'], new_message=True)
                    self.assertEqual(receipt['state'], 'error')
                    self.assertIn('duplicate launch', receipt['reason'])
                else:
                    self.assertEqual(runtime.read_bytes(), before)
                    message = mailbox.append_message(self.directory, 'live input', {'name': 'user'}, 'follow-up')
                    executor = SimpleNamespace(properties=self.properties, facts=self.facts)
                    expected = {'state': 'native_queued'}
                    with missing_once(runtime, failures=2, recurring=True), \
                            patch.object(native, 'observe', return_value={'live': 'alive'}), \
                            patch.object(native, 'context', return_value=(self.directory, object(), None, executor)), \
                            patch.object(native, 'deliver', return_value=expected) as deliver, \
                            patch.object(lifecycle, 'start', side_effect=AssertionError('no duplicate wake')):
                        self.assertEqual(native.send_or_wake(self.root, 'reader', message['id'], new_message=True), expected)
                    deliver.assert_called_once()
        self.assertEqual(records.runtime(runtime), {})
        self.assertIsNotNone(missing_runtime.damage(self.directory, {}))
        fresh = self.work / 'never-launched'
        fresh.mkdir()
        self.assertEqual(records.runtime(fresh / 'runtime.json'), {})
        self.assertIsNone(missing_runtime.damage(fresh, {}))
        # The optional ledger does not acquire the runtime-only required-read policy.
        self.assertEqual(mailbox._read_marks(self.directory), {})

    def test_focus_cursor_ledgers_and_session_history_keep_their_absence_policies(self):
        """Brief missing reads must not release focus, replay consumed mail, or forget session ownership."""
        at = store.now()
        active = {'id': store.uid(), 'since': at, 'by': 'user', 'turns': 2, 'until': None}
        last = {'id': active['id'], 'since': at, 'held': 2, 'report': None}
        for name, data, read, empty in (
                ('focus.json', active, lambda: focus.current(self.directory), None),
                ('focus-last.json', last, lambda: focus._read(self.directory, focus.LAST), None),
                ('inbox.cursor', 7, lambda: mailbox._cursor(self.directory), 0),
                ('inbox-state.json', {'m': {'submission_state': 'accepted'}},
                 lambda: mailbox._states(self.directory), {}),
                ('inbox-read.json', {'m': at}, lambda: mailbox._read_marks(self.directory), {})):
            path = self.directory / name
            store.write(path, data)
            with self.subTest(name=name, missing='brief'), missing_once(path, failures=2):
                self.assertEqual(read(), data)
            path.unlink()
            with self.subTest(name=name, missing='lasting'), patch.object(store, 'MISSING_DELAY', 0):
                self.assertEqual(read(), empty)
        store.write(self.directory / focus.FILE, active)
        with missing_once(self.directory / focus.FILE, failures=2):
            self.assertIsNone(focus.turn_ended(self.directory))
        self.assertEqual(focus.current(self.directory)['turns'], 1)
        legacy = self.directory / 'native-delivery.json'
        store.write(legacy, {'legacy': [{'state': 'native_queued', 'native_session_id': store.uid()}]})
        with missing_once(legacy, failures=2):
            self.assertEqual(mailbox._states(self.directory)['legacy']['submission_state'], 'accepted')
        legacy.unlink()
        self.assertEqual(mailbox._states(self.directory), {})

    def test_absent_append_only_session_journal_never_waits_and_later_appends_are_visible(self):
        """Repeated transcript validation must not wait on a never-created append-only journal or cache its absence."""
        journal = self.directory / 'sessions.jsonl'
        row = {'schema': 1, 'id': store.uid(), 'at': store.now(), 'harness': 'codex', 'launch': self.launch,
               'reason': 'switch', 'home': str(self.work), 'from': store.uid(), 'to': store.uid()}
        from multiverse.plugins import api
        store.write(self.directory / 'runtime.json', {})
        with patch.object(store.time, 'sleep', side_effect=AssertionError('append-only absence must not wait')):
            for _ in range(100):
                self.assertEqual(sessions.journal(self.directory), [])
                self.assertTrue(api._never_launched(self.directory, self.properties))
                self.assertEqual(native._session_snapshot(self.root, self.directory, self.properties, {},
                                                           adapter=SimpleNamespace(name='codex')), (self.properties, {}))
            sessions._append(self.directory, row)
            self.assertEqual(sessions.journal(self.directory), [row])
            self.assertFalse(api._never_launched(self.directory, self.properties))
            journal.write_text('damaged\n')
            with self.assertRaises(sessions.JournalError):
                sessions.journal(self.directory)
            journal.unlink()
            self.assertEqual(sessions.journal(self.directory), [])
            self.assertTrue(api._never_launched(self.directory, self.properties))
        original_stat = os.stat

        def unreadable(path, *args, **kwargs):
            if not isinstance(path, int) and Path(path) == journal:
                raise OSError(number, 'cannot inspect journal', str(path))
            return original_stat(path, *args, **kwargs)

        journal.write_text('stored lineage\n')
        for number in (errno.EACCES, errno.EIO):
            with self.subTest(errno=number), patch.object(os, 'stat', unreadable):
                self.assertFalse(api._never_launched(self.directory, self.properties))
                with self.assertRaises(OSError) as error:
                    native._session_snapshot(self.root, self.directory, self.properties, {},
                                             adapter=SimpleNamespace(name='codex'))
                self.assertEqual(error.exception.errno, number)
        with patch.object(Path, 'open', side_effect=PermissionError('denied')), \
                self.assertRaisesRegex(sessions.JournalError, 'cannot read'):
            sessions.journal(self.directory)

    def test_optional_indexes_identity_and_universe_metadata_survive_replacement(self):
        """Missing metadata must not discard pending commands, reserve no identity, or select a parent universe."""
        pending = self.directory / 'slash' / 'pending.json'
        identifier = store.uid()
        store.write(pending, {'ids': [identifier]})
        with missing_once(pending, failures=2):
            self.assertEqual(slash._pending(self.directory), [identifier])
        store.write(pending.with_name(identifier + '.json'), {
            'command': 'model', 'session': {'launch_id': self.launch},
            'result': {'id': identifier, 'command': 'model', 'args': 'other', 'state': 'submitted'}})
        with missing_once(pending, probe='stat', failures=2), self.assertRaisesRegex(ValueError, 'no confirmed model'):
            slash.require_settled(self.directory, self.properties, self.facts)
        with missing_once(pending, probe='stat', failures=2), \
                patch.object(native, '_session_snapshot', return_value=(self.properties, self.facts)), \
                patch.object(slash, 'reconcile', return_value={'settled': True}) as reconcile:
            self.assertEqual(native.session_snapshot(self.root, self.directory, self.properties, self.facts,
                                                    launch_locked=True)[0], {'settled': True})
            reconcile.assert_called_once()
        pending.unlink()
        self.assertEqual(slash._pending(self.directory), [])
        slash.require_settled(self.directory, self.properties, self.facts)
        with patch.object(native, '_session_snapshot', return_value=(self.properties, self.facts)), \
                patch.object(slash, 'reconcile', side_effect=AssertionError('genuinely absent index')):
            self.assertEqual(native.session_snapshot(self.root, self.directory, self.properties, self.facts,
                                                    launch_locked=True), (self.properties, self.facts))
        prop = self.directory / 'properties.json'
        with missing_once(prop, probe='stat', failures=2):
            self.assertFalse(agents.properties_missing(self.directory))
        with missing_once(prop, probe='stat', failures=2):
            self.assertFalse(agents.orphaned(self.directory))
        prop.unlink()
        self.assertTrue(agents.properties_missing(self.directory))
        (self.directory / 'runtime.json').unlink()
        self.assertTrue(agents.orphaned(self.directory))
        child = templates.create(self.root / 'subverses' / 'child', 'child', workdir=self.work)
        meta = child / 'universe.json'
        with missing_once(meta, probe='stat', failures=2):
            self.assertEqual(universe.resolve(child), child)
        with missing_once(meta, probe='stat', failures=2):
            self.assertEqual(universe.exact(child), child)
        with missing_once(meta, probe='stat', failures=2):
            self.assertEqual(universe.children(self.root), [child])
        with missing_once(self.root / 'universe.json', probe='stat', failures=2):
            self.assertEqual(universe.parent(child), self.root)
        meta.unlink()
        self.assertEqual(universe.children(self.root), [])
        with self.assertRaises(ValueError):
            universe.exact(child)
        entry = self.root / 'commands' / 'replace-check' / 'entry.json'
        store.write(entry, {'description': 'retained metadata'})
        with missing_once(entry, probe='stat', failures=2):
            self.assertIn('replace-check', [row['name'] for row in catalog._own(self.root)])
        entry.unlink()
        self.assertNotIn('replace-check', [row['name'] for row in catalog._own(self.root)])
        manifest = self.root / templates.MANIFEST
        with missing_once(manifest, probe='stat', failures=2):
            self.assertTrue(templates.refresh_plan(self.root)['has_manifest'])
        manifest.unlink()
        self.assertFalse(templates.refresh_plan(self.root)['has_manifest'])
        store.write(prop, self.properties)
        with missing_once(prop, failures=2):
            self.assertEqual(blocked._find(self.root, self.directory.name), self.properties)
        with missing_once(prop, failures=2):
            self.assertEqual(agent_hints.Search().record(prop), self.properties)
        prop.unlink()
        self.assertIsNone(blocked._find(self.root, self.directory.name))
        self.assertIsNone(agent_hints.Search().record(prop))

    def test_required_readiness_and_cleanup_evidence_do_not_fail_on_one_miss(self):
        """Readiness and receipt reconciliation require the saved record, not a guessed default after replacement."""
        launch = self.directory / 'launches' / self.launch
        request = launch / 'request.json'
        store.write(request, {'saved': True})
        result = {**self.properties, 'runtime': self.facts}
        with patch.object(lifecycle, 'launch_result', return_value=result), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                patch.object(lifecycle, 'launch_ready', return_value=True):
            with missing_once(request, probe='read_text', failures=2):
                self.assertTrue(lifecycle.wait_ready(self.root, self.directory, self.launch, object(), 1)['ready'])
            request.unlink()
            with self.assertRaises(FileNotFoundError):
                lifecycle.wait_ready(self.root, self.directory, self.launch, object(), 1)
        cleanup = launch / 'cleanup.json'
        store.write(cleanup, {'schema': 1, 'launch_id': self.launch, 'pid': 123, 'child_pid': 456})
        with patch.object(attachments, 'recorded_process_problem', return_value=None), \
                patch.object(attachments, 'problem', return_value=None):
            with missing_once(cleanup, probe='read_text', failures=2):
                self.assertTrue(delivery_evidence.launch_ended(self.directory, self.launch))
            cleanup.unlink()
            with self.assertRaises(FileNotFoundError):
                delivery_evidence.launch_ended(self.directory, self.launch)

    def test_run_storage_keeps_a_replaced_universe_marker(self):
        """Only relevant markers may cost retries, but their gaps and protected-directory aliases must still refuse storage."""
        other = self.work / 'other-universe'
        marker = other / 'universe.json'
        store.write(marker, {'id': store.uid()})
        destination = other / 'state' / 'disposable-run'
        with missing_once(marker, probe='stat', failures=2):
            self.assertIn('outside', runs._storage_problem(self.root, destination))
        (other / 'actual-state').mkdir()
        (other / 'state').symlink_to(other / 'actual-state', target_is_directory=True)
        with missing_once(marker, probe='stat', failures=2):
            self.assertIn('outside', runs._storage_problem(self.root, other / 'actual-state' / 'run'))
        marker.unlink()
        with patch.object(store, 'MISSING_DELAY', 0):
            self.assertIsNone(runs._storage_problem(self.root, destination))
        for destination in (self.work / 'home' / '.multiverse' / 'runs' / 'plugin', other / 'ordinary-run'):
            with self.subTest(destination=destination), \
                    patch.object(runs, 'is_file_replaced', side_effect=AssertionError('irrelevant marker read')):
                self.assertIsNone(runs._storage_problem(self.root, destination))
        ordinary = self.work / 'ordinary'
        ordinary.mkdir()
        loop = ordinary / 'state'
        loop.symlink_to('state')
        destination = ordinary / 'runs' / 'plugin'
        self.assertIsNone(runs._storage_problem(self.root, destination))
        resolve = Path.resolve
        def broken(path, *args, **kwargs):
            if path == loop:
                raise RuntimeError('symlink loop')  # Python 3.11; 3.13's non-strict resolve may leave it unresolved.
            return resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', broken):
            self.assertIsNone(runs._storage_problem(self.root, destination))
            store.write(ordinary / 'universe.json', {'id': store.uid()})
            with self.assertRaisesRegex(RuntimeError, 'symlink loop'):
                runs._storage_problem(self.root, destination)
            with missing_once(ordinary / 'universe.json', probe='stat', failures=2), \
                    self.assertRaisesRegex(RuntimeError, 'symlink loop'):
                runs._storage_problem(self.root, destination)

    def test_cursor_retry_preserves_the_snapshot_read_bound(self):
        """A replaced cursor must retain its fstat-sized read instead of following a moving EOF."""
        path = self.directory / 'inbox.cursor'
        path.write_text('12')
        original, sizes = mailbox.open_replaced, []
        class Watched:
            def __enter__(self):
                self.stream = original(path)
                return self
            def __exit__(self, *unused):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            def read(self, size=-1):
                sizes.append(size)
                return self.stream.read(size)
        with patch.object(mailbox, 'open_replaced', lambda _: Watched()):
            self.assertEqual(mailbox._cursor(self.directory), 12)
        self.assertEqual(sizes, [2])

    def test_inbox_batches_absence_under_its_lock_without_forgetting_read_proof(self):
        """Batched missing probes must preserve cursor, ahead-read and confirmed-upgrade evidence."""
        first = mailbox.append_message(self.directory, 'consumed', {'name': 'user'})
        second = mailbox.append_message(self.directory, 'read ahead', {'name': 'user'})
        third = mailbox.append_message(self.directory, 'native input', {'name': 'user'}, 'follow-up',
                                       inbox_only_text='original input')
        data = (self.directory / 'inbox.jsonl').read_bytes()
        store.write(self.directory / 'inbox.cursor', len(data.splitlines(keepends=True)[0]))
        store.write(self.directory / 'inbox-read.json', {second['id']: store.now()})
        store.write(self.directory / 'inbox-state.json', {third['id']: {'native_receipt': {'transcript_confirmed': True}}})
        names = ('inbox.cursor', 'inbox-read.json', 'inbox-state.json')
        for name in names:
            with self.subTest(name=name), missing_once(self.directory / name, probe='stat', failures=2):
                self.assertEqual(mailbox.inbox(self.directory, peek=True)['messages'], [])
        for name in names:
            (self.directory / name).unlink()
        with patch.object(store, 'MISSING_DELAY', 0):
            unread = mailbox.inbox(self.directory, peek=True)['messages']
        self.assertEqual([message['id'] for message in unread], [first['id'], second['id'], third['id']])
