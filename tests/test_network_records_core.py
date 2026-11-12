"""Offline replacement-window checks for core readers and their real-absence policies."""
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from test_multiverse import BaseTest
from multiverse import store
from multiverse.harnesses.types import PromptList, PromptResult, SessionHandle, WaitingPrompt
from multiverse.runtime import (attachments, prompts, queued_prompts, recovery, registration,
                                run_files, runner, storage_adoption)


class NetworkCoreRecordsTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.native_id, self.launch_id = (str(uuid4()) for _ in range(3))
        self.directory = self.root / 'state' / 'agents' / self.agent
        self.launch = self.directory / 'launches' / self.launch_id
        self.launch.mkdir(parents=True)
        self.properties = {'id': self.agent, 'name': 'worker', 'harness': 'fixture',
                           'workspace': str(self.work), 'native_session_id': self.native_id}
        self.facts = {'launch_id': self.launch_id, 'state': 'running', 'status': 'idle'}
        store.write(self.directory / 'properties.json', self.properties)
        store.write(self.directory / 'runtime.json', self.facts)
        self.session = SessionHandle.from_records(self.properties, self.facts, self.directory)
        self.enterContext(patch.object(store.time, 'sleep'))

    @contextmanager
    def missing(self, path, method='read_text', *, lasting=False, after=0):
        """Inject ENOENT at one read boundary, without modifying the record or any writer."""
        original = getattr(Path, method)
        calls = []

        def observed(candidate, *args, **kwargs):
            if candidate == path:
                calls.append(candidate)
                if len(calls) > after and (lasting or len(calls) == after + 1):
                    raise FileNotFoundError(errno.ENOENT, 'replacement not visible', str(path))
            return original(candidate, *args, **kwargs)

        with patch.object(Path, method, observed):
            yield calls

    @contextmanager
    def missing_open(self, path, *, lasting=False):
        original = os.open
        calls = []

        def observed(candidate, flags, *args, **kwargs):
            if candidate == path or (candidate == path.name and kwargs.get('dir_fd') is not None):
                calls.append(flags)
                if lasting or len(calls) == 1:
                    raise FileNotFoundError(errno.ENOENT, 'replacement not visible', str(path))
            return original(candidate, flags, *args, **kwargs)

        with patch.object(os, 'open', observed):
            yield calls

    def test_required_record_readers_retry_without_defaulting_missing_identity(self):
        """Shared core record readers must recover a replacement miss without turning lasting loss into authority."""
        readers = [(attachments._read_record, self.launch / name)
                   for name in ('cleanup.json', 'roles/worker.json', 'roles/connection-worker.json')]
        readers += [(runner._read_record, path) for path in
                    (self.directory / 'properties.json', self.directory / 'runtime.json', self.launch / 'request.json')]
        readers += [(lambda path: recovery._properties(path.parent), self.directory / 'properties.json')]
        for reader, path in readers:
            with self.subTest(reader=reader, path=path):
                store.write(path, {'preserved': path.name})
                with self.missing(path) as calls:
                    self.assertEqual(reader(path), {'preserved': path.name})
                self.assertEqual(len(calls), 2)
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    reader(path)
                self.assertFalse(path.exists())

    def test_cleanup_and_role_evidence_survive_replacement_without_relaxing_absence(self):
        """Skipping a cleanup or role record can authorize a new launch while its old connections still run."""
        cleanup = self.launch / 'cleanup.json'
        store.write(cleanup, {'schema': 1, 'launch_id': self.launch_id, 'state': 'active', 'pid': 42})
        with patch.object(attachments, '_gone', return_value=False), \
                patch.object(attachments, '_blocked', return_value='still running'):
            with self.missing(cleanup):
                self.assertEqual(attachments.problem(self.directory, {'launch_id': self.launch_id}), 'still running')
            cleanup.unlink()
            # A truly missing legacy cleanup still uses the retained runner proof.
            self.assertEqual(attachments.problem(self.directory, {'launch_id': self.launch_id, 'runner_pid': 42}),
                             'still running')
            self.assertIsNone(attachments.problem(self.directory, {'launch_id': self.launch_id}))
            store.write(cleanup, None)
            self.assertIn('invalid cleanup record', attachments.problem(self.directory, {'launch_id': self.launch_id}))
            store.write(cleanup, {'schema': 1, 'launch_id': self.launch_id, 'state': 'complete'})
            role = self.launch / 'roles' / 'worker.json'
            store.write(role, {'schema': 1, 'launch_id': self.launch_id, 'state': 'active', 'pid': 42})
            with self.missing(role):
                self.assertEqual(attachments.problem(self.directory, {'launch_id': self.launch_id}), 'still running')
            with self.missing(role, lasting=True), self.assertRaises(FileNotFoundError):
                attachments.problem(self.directory, {'launch_id': self.launch_id})

    def test_attachment_mutations_retry_only_their_reads(self):
        """A transient missing read must neither strand an active role nor discard a connection's original metadata."""
        cleanup = self.launch / 'cleanup.json'
        attachments.begin(self.launch, 'runner-start')
        for action in (lambda: attachments.record_child(self.launch, 51, 'child-start'),
                       lambda: attachments.finish(self.launch)):
            with self.missing(cleanup), patch.object(attachments, 'write', wraps=store.write) as writing:
                action()
            writing.assert_called_once()
        self.assertEqual(store.read(cleanup)['child_pid'], 51)
        cleanup.unlink()
        for action in (lambda: attachments.record_child(self.launch, 51, 'child-start'),
                       lambda: attachments.finish(self.launch)):
            with self.assertRaises(FileNotFoundError):
                action()
        role = attachments.Role(self.launch, 'worker', pending=True)
        with self.missing(role.path):
            role.claimed(52, 'worker-start')
        with self.missing(role.path):
            self.assertEqual(attachments.Role.existing(role.path).record['pid'], 52)
        with self.missing(role.path):
            role.close()
        self.assertEqual(store.read(role.path)['state'], 'closed')
        role.path.unlink()
        role.close()
        self.assertFalse(role.path.exists())
        with self.assertRaises(FileNotFoundError):
            role.claimed(52, 'worker-start')
        with self.assertRaises(FileNotFoundError):
            attachments.Role.existing(role.path)

        logger = attachments.connection_logger(self.launch, started='connection-start')
        value = {'id': str(uuid4()), 'role': 'watch', 'endpoint': 'private', 'state': 'active'}
        logger(value)
        path = self.launch / 'roles' / ('connection-' + value['id'] + '.json')
        previous = store.read(path)
        with self.missing(path), patch.object(attachments, 'write', wraps=store.write) as writing:
            logger({**value, 'state': 'closed'})
        writing.assert_called_once()
        self.assertEqual(store.read(path)['at'], previous['at'])
        path.unlink()
        logger(value)  # Genuine first publication remains supported.
        self.assertEqual(store.read(path)['state'], 'active')

    def test_connection_pruning_retains_unreadable_records(self):
        """Pruning may recover a replaced closed record but cannot discard evidence that remains unreadable."""
        paths = [self.launch / 'roles' / ('connection-' + str(uuid4()) + '.json') for _ in range(2)]
        for index, path in enumerate(paths):
            store.write(path, {'schema': 1, 'launch_id': self.launch_id, 'state': 'closed', 'closed_at': str(index)})
        with self.missing(paths[0], lasting=True), patch.object(attachments, 'CLOSED_CONNECTION_LIMIT', 1):
            attachments._prune_connections(self.launch)
        self.assertTrue(all(path.exists() for path in paths))
        with self.missing(paths[0]), patch.object(attachments, 'CLOSED_CONNECTION_LIMIT', 1):
            attachments._prune_connections(self.launch)
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[1].exists())

    def test_nonmissing_errors_stay_immediate_and_cannot_hide_cleanup_evidence(self):
        """Retrying replacement misses must never reinterpret unreadable cleanup evidence as a legacy absent file."""
        path = self.launch / 'cleanup.json'
        failure = PermissionError(errno.EACCES, 'denied', str(path))
        with patch.object(attachments, 'read', side_effect=failure) as reading:
            self.assertIn('cannot be read', attachments.problem(self.directory, {'launch_id': self.launch_id}))
        reading.assert_called_once_with(path)
        with patch.object(Path, 'lstat', side_effect=failure) as probing, self.assertRaises(PermissionError):
            prompts._record_present(path)
        probing.assert_called_once()

    def prompt_record(self):
        identifier = str(uuid4())
        record = {'schema': 1, 'id': identifier, 'agent': self.agent, 'session': asdict(self.session),
                  'result': asdict(PromptResult(id=identifier, state='answered'))}
        path = self.directory / 'prompts' / (identifier + '.json')
        store.write(path, record)
        return identifier, path, record

    def test_prompt_results_never_turn_a_replacement_miss_into_an_unattempted_prompt(self):
        """A missing probe during result replacement must retain the saved answer and never invoke unanswered handling."""
        identifier, path, record = self.prompt_record()
        with self.missing(path, 'lstat'), patch.object(prompts, '_unanswered') as unanswered:
            self.assertEqual(prompts.result(self.root, self.agent, identifier).state, 'answered')
        unanswered.assert_not_called()
        with self.missing_open(path):
            self.assertEqual(prompts._record(self.directory, identifier, missing_ok=True), record)
        path.unlink()
        absent = PromptResult(id=identifier, state='refused')
        with patch.object(prompts, '_unanswered', return_value=absent) as unanswered:
            self.assertEqual(prompts.result(self.root, self.agent, identifier), absent)
        unanswered.assert_called_once()
        path.symlink_to(path.parent / 'missing-target')
        self.assertTrue(prompts._record_present(path))
        with self.assertRaises(prompts.records.RecordError):
            prompts._record(self.directory, identifier, missing_ok=True)

    def test_prompt_admission_and_catalog_keep_replaced_answers_reserved(self):
        """An existing nonwaiting answer must stay unavailable in both prompt admission and its catalog."""
        identifier, path, _ = self.prompt_record()
        prompt = WaitingPrompt(id=identifier, kind='question', tool='Ask', summary='Question', requires_waiting=False)
        with self.missing(path, 'lstat'), patch.object(prompts, '_connect') as connect:
            self.assertEqual(prompts.answer(self.root, self.agent, identifier, wait=False).cause, 'already_answered')
        connect.assert_not_called()
        with patch.object(prompts, '_connect', return_value=(None, self.session, None)), \
                patch.object(prompts, '_catalog', return_value=PromptList(state='ok', prompts=(prompt,))):
            with self.missing(path, 'lstat'):
                self.assertFalse(prompts.waiting(self.root, self.agent).prompts[0].answerable)
            path.unlink()
            self.assertTrue(prompts.waiting(self.root, self.agent).prompts[0].answerable)
        with patch.object(prompts, '_connect', return_value=None) as connect, \
                patch.object(prompts, '_stopped', return_value=PromptResult(id=identifier, state='gone')):
            self.assertEqual(prompts.answer(self.root, self.agent, identifier, wait=False).state, 'gone')
        connect.assert_called_once()

    def test_registration_recovers_progress_but_still_refuses_a_disappeared_owner(self):
        """One missed progress replacement must not abort launch registration, while lasting loss remains fatal."""
        path = self.launch / 'registration.json'
        record = {'schema': 1, 'launch_id': self.launch_id, 'parent_pid': 41, 'parent_started': None,
                  'sequence': 0, 'stage': 'preparing', 'state': 'preparing'}
        store.write(path, record)
        original = registration._owner
        observed = []

        def owner(value, launch):
            observed.append(value)
            if len(observed) == 2:
                (launch / 'ready').touch()
            return original(value, launch)

        with self.missing(path, after=1) as calls, patch.object(registration, '_owner', side_effect=owner), \
                patch.object(registration, '_parent_gone', return_value=False):
            registration.wait_for_release(self.launch)
        self.assertEqual(len(calls), 3)
        (self.launch / 'ready').unlink()
        with self.missing(path, lasting=True, after=1), \
                patch.object(registration, '_parent_gone', return_value=False), \
                self.assertRaisesRegex(RuntimeError, 'disappeared'):
            registration.wait_for_release(self.launch)
        path.unlink()
        with patch.object(registration, 'LEGACY_TIMEOUT', 0), self.assertRaises(RuntimeError):
            registration.wait_for_release(self.launch)

    def test_run_journal_retry_preserves_nofollow_and_missing_cleanup_policy(self):
        """Transient run journal loss must not create leftovers, and lasting loss must never authorize cleanup."""
        path = self.launch / 'run.json'
        record = {'phase': 'ended', 'supervisor': {'pid': 41}}
        store.write(path, record)
        with self.missing_open(path) as calls:
            self.assertEqual(run_files._read(path), record)
        self.assertTrue(all(flags & os.O_NOFOLLOW for flags in calls))
        with patch.object(run_files, '_candidates', return_value=[self.launch]), \
                patch.object(run_files, '_validate'), patch.object(run_files, 'ended', return_value=True), \
                patch.object(run_files, 'cleanup') as cleanup:
            with self.missing_open(path):
                self.assertEqual(run_files.leftovers(self.work, sweep=True), [])
            cleanup.assert_called_once_with(self.launch, record)
            cleanup.reset_mock()
            path.unlink()
            self.assertEqual(len(run_files.leftovers(self.work, sweep=True)), 1)
            cleanup.assert_not_called()
        target = self.launch / 'other.json'
        store.write(target, record)
        path.symlink_to(target)
        with self.assertRaises(OSError):
            run_files._read(path)

    def test_status_watcher_reads_through_a_replacement_but_stops_on_real_loss(self):
        """A single property or runtime miss must not permanently end the status watcher."""
        for path in (self.directory / 'properties.json', self.directory / 'runtime.json'):
            with self.subTest(path=path):
                watcher = runner.StatusWatcher(self.directory, self.launch_id, self.root)
                adapter = SimpleNamespace(status_watch=Mock(return_value=None))
                with patch('multiverse.harnesses.load', return_value=adapter), \
                        patch.object(watcher, '_publish'), self.missing(path):
                    asyncio.run(watcher._observe())
                adapter.status_watch.assert_called_once()
                with patch('multiverse.harnesses.load', return_value=adapter), \
                        patch.object(watcher, '_publish'), self.missing(path, lasting=True), \
                        self.assertRaises(FileNotFoundError):
                    asyncio.run(watcher._observe())

    def test_relay_and_startup_publication_do_not_discard_transient_identity(self):
        """Transient identity loss must not suppress relay recovery or prematurely fail durable startup delivery."""
        path = self.directory / 'properties.json'
        for lasting in (False, True):
            with self.subTest(lasting=lasting), self.missing(path, lasting=lasting), \
                    patch.object(runner, 'mutate_runtime', return_value={}) as mutate, \
                    patch.object(recovery, 'schedule') as schedule:
                runner.RelayPublisher(self.directory, self.launch_id)('connection ended')
                self.assertEqual(mutate.call_count, 0 if lasting else 1)
                self.assertEqual(schedule.call_count, 0 if lasting else 1)
            delivery = runner.StartupDelivery(self.directory, self.launch_id, self.root)
            with self.subTest(startup_lasting=lasting), self.missing(path, lasting=lasting), \
                    patch('multiverse.harnesses.load'), \
                    patch('multiverse.runtime.lifecycle.wait_ready', return_value={'ready': True}), \
                    patch('multiverse.runtime.native.flush_startup') as flush:
                delivery._run()
                self.assertEqual(flush.call_count, 1)
                self.assertEqual(flush.call_args.args[-1] is not None, lasting)

    def test_wrapper_and_recovery_presence_gates_retry_without_recreating_removed_agents(self):
        """Presence gates must not abandon existing cleanup/recovery work or revive genuinely absent identity."""
        path = self.directory / 'properties.json'
        (self.directory / '.launch.lock').touch()
        for lasting in (False, True):
            with self.subTest(wrapper_lasting=lasting), self.missing(path, 'stat', lasting=lasting), \
                    patch.object(runner, 'runtime', return_value={}) as read_runtime:
                runner.cleanup_wrapper(self.launch)
                self.assertEqual(read_runtime.call_count, 0 if lasting else 1)
            with self.subTest(recovery_lasting=lasting), self.missing(path, 'stat', lasting=lasting), \
                    patch.object(recovery, 'process_started', return_value='worker-start'), \
                    patch.object(attachments, 'Role') as role, patch.object(recovery, '_run', return_value='recovered'):
                result = recovery.run(self.directory, self.launch_id)
                self.assertEqual(result, None if lasting else 'recovered')
                self.assertEqual(role.call_count, 0 if lasting else 1)

    def test_storage_adoption_retries_only_replaced_startup_evidence(self):
        """Missing delivery or launch evidence must not falsely qualify an old assigned conversation as never started."""
        store.write(self.directory / 'runtime.json', {'launch_id': self.launch_id, 'state': 'starting'})
        properties = {**self.properties, 'native_session_id': self.agent, 'phase': 'starting'}
        adapter = SimpleNamespace(session_mode='assigned')
        cases = [(self.directory / 'native-delivery.json', 'read_bytes', {'message': ['submitted']}),
                 (self.directory / 'inbox-state.json', 'stat', {'message': {'submission_state': 'accepted'}}),
                 (self.launch / 'request.json', 'lstat', {}), (self.launch / 'cleanup.json', 'lstat', {})]
        for path, method, value in cases:
            with self.subTest(path=path):
                store.write(path, value)
                with self.missing(path, method):
                    self.assertFalse(storage_adoption._never_started(adapter, properties, {}, self.directory))
                path.unlink()
                self.assertTrue(storage_adoption._never_started(adapter, properties, {}, self.directory))

    def test_storage_launch_request_discovery_preserves_absent_preparation_policy(self):
        """A replaced launch request must remain storage evidence without requiring requests from failed preparations."""
        path = self.launch / 'request.json'
        value = {'env': {'HOME': str(self.work)}}
        store.write(path, value)
        with self.missing(path, 'lstat'):
            self.assertEqual(storage_adoption._launch_requests(self.directory), [(path, value)])
        with self.missing_open(path):
            self.assertEqual(storage_adoption._launch_requests(self.directory), [(path, value)])
        path.unlink()
        self.assertEqual(storage_adoption._launch_requests(self.directory), [])
        path.symlink_to(self.launch / 'missing-request')
        with self.assertRaises(ValueError):
            storage_adoption._launch_requests(self.directory)

    def test_queue_ledger_keeps_message_identity_through_replacement(self):
        """A transient missing delivery ledger must not hide a queued mv2 follow-up from its sender guard."""
        message = {'id': str(uuid4()), 'delivery': 'follow-up', 'from': {'agent_id': 'sender'}}
        (self.directory / 'inbox.jsonl').write_text(json.dumps(message) + '\n')
        path = self.directory / 'inbox-state.json'
        store.write(path, {message['id']: {'attempts': [{'launch_id': self.launch_id,
                    'native_session_id': self.native_id, 'state': 'unknown'}]}})
        with self.missing_open(path):
            joined, unconfirmed, _ = queued_prompts._ledger(self.directory, self.session)
        self.assertEqual(joined[message['id']], (message['id'], 'follow-up', 'sender'))
        self.assertEqual(unconfirmed, frozenset({'sender'}))
        path.unlink()
        self.assertEqual(queued_prompts._ledger(self.directory, self.session), ({}, frozenset(), frozenset()))
        history = self.directory / 'inbox.jsonl'
        with self.missing_open(history) as calls:
            self.assertEqual(queued_prompts._bytes(history), b'')
        self.assertEqual(len(calls), 1, 'Append-only history is outside replacement retry scope')
