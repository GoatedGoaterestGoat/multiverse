"""Crash/reuse recovery never controls an unrelated process or waits forever on an unclaimed role."""
from datetime import datetime, timedelta, timezone
import os
from unittest.mock import patch
from uuid import uuid4

from multiverse.runtime import attachments
from multiverse.runtime.process import process_started
from multiverse.store import read, write
from test_multiverse import BaseTest


class AttachmentIdentityTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents' / str(uuid4())
        self.launch = self.directory / 'launches' / str(uuid4())
        self.launch.mkdir(parents=True)
        self.facts = {'launch_id': self.launch.name, 'state': 'stopped'}
        self.old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        attachments.begin(self.launch, process_started(os.getpid()))
        attachments.finish(self.launch)

    def legacy_role(self, name='old role', *, at=None):
        role = attachments.Role(self.launch, name)
        value = read(role.path)
        write(role.path, {**value, 'pid_started': None, 'at': at if at is not None else self.old})
        return role

    def test_new_roles_record_their_owner_start_and_connection_logger_samples_once(self):
        started = process_started(os.getpid())
        with patch.object(attachments, 'process_started', return_value=started) as sampled:
            role = attachments.Role(self.launch, 'new observer')
            self.assertEqual(read(role.path)['pid_started'], started)
            sampled.assert_called_once()
        with patch.object(attachments, 'process_started', return_value=started) as sampled:
            logger = attachments.connection_logger(self.launch)
            item = {'id': str(uuid4()), 'role': 'status stream', 'endpoint': '/private/service.sock', 'state': 'opening'}
            logger(item)
            logger({**item, 'state': 'closed'})
            sampled.assert_called_once()
        row = read(self.launch / 'roles' / ('connection-' + item['id'] + '.json'))
        self.assertEqual(row['pid_started'], started)

    def test_legacy_role_and_connection_started_after_their_records_are_reused(self):
        role = self.legacy_role()
        logger = attachments.connection_logger(self.launch)
        item = {'id': str(uuid4()), 'role': 'old connection', 'endpoint': '/private/service.sock', 'state': 'opening'}
        logger(item)
        path = self.launch / 'roles' / ('connection-' + item['id'] + '.json')
        write(path, {**read(path), 'pid_started': None, 'at': self.old})
        self.assertIsNone(attachments.problem(self.directory, self.facts))
        self.assertEqual(read(role.path)['state'], 'active', 'Observation must never signal or forge closure')

    def test_legacy_stopped_runner_reuse_uses_exit_time_not_pre_spawn_time(self):
        (self.launch / 'cleanup.json').unlink()
        old = {**self.facts, 'runner_pid': os.getpid(), 'pid': os.getpid(), 'exit_code': 0, 'stopped_at': self.old}
        self.assertIsNone(attachments.problem(self.directory, old))
        slow = {**old, 'started_at': self.old, 'stopped_at': datetime.now(timezone.utc).isoformat()}
        problem = attachments.problem(self.directory, slow)
        self.assertIsNotNone(problem, 'A legitimate process can start after pre-spawn started_at')
        self.assertIn(str(os.getpid()), problem)

    def test_unclaimed_pending_roles_expire_but_unknown_or_future_dates_do_not(self):
        worker = attachments.Role(self.launch, 'recovery worker', pending=True)
        value = read(worker.path)
        write(worker.path, {**value, 'at': self.old})
        self.assertIsNone(attachments.problem(self.directory, self.facts))
        for at in ('bad-date', None, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()):
            write(worker.path, {**value, 'at': at})
            with self.subTest(at=at):
                self.assertIn('recovery worker', attachments.problem(self.directory, self.facts))

    def test_denied_probe_still_recognizes_reuse_and_unknown_identity_is_actionable(self):
        role = self.legacy_role(at=datetime.now(timezone.utc).isoformat())
        with patch.object(attachments.os, 'kill', side_effect=PermissionError):
            self.assertTrue(attachments._gone(os.getpid(), 'Thu Jan 1 00:00:00 1970'))
            problem = attachments.problem(self.directory, self.facts)
        self.assertIn(str(os.getpid()), problem)
        self.assertIn(process_started(os.getpid()), problem)
        self.assertIn(str(role.path), problem)
        self.assertIn('retry', problem)

    def test_malformed_identity_is_not_proof_of_reuse_and_unknown_inspection_blocks(self):
        role = self.legacy_role()
        record = read(role.path)
        for token in ('invalid', [], 1, True):
            with self.subTest(token=token):
                write(role.path, {**record, 'pid_started': token})
                self.assertIn(str(role.path), attachments.problem(self.directory, self.facts))
        write(role.path, {**record, 'pid': []})
        self.assertIn(str(role.path), attachments.problem(self.directory, self.facts))
        write(role.path, record)
        with patch.object(attachments, 'process_snapshot', return_value=None), \
             patch.object(attachments.os, 'kill', side_effect=PermissionError):
            reason = attachments.problem(self.directory, self.facts)
        self.assertIn('cannot prove', reason)
        self.assertIn('inspection unavailable', reason)
        role.path.write_text('{damaged')
        self.assertIn(str(role.path), attachments.problem(self.directory, self.facts))

    def test_reused_cleanup_owner_and_many_connections_share_one_process_sample(self):
        record = read(self.launch / 'cleanup.json')
        write(self.launch / 'cleanup.json', {**record, 'state': 'active', 'pid_started': None, 'at': self.old})
        logger = attachments.connection_logger(self.launch, started=None)
        for _ in range(40):
            item = {'id': str(uuid4()), 'role': 'old connection', 'endpoint': '/private/service.sock', 'state': 'opening'}
            logger(item)
            path = self.launch / 'roles' / ('connection-' + item['id'] + '.json')
            write(path, {**read(path), 'at': self.old})
        snapshot = attachments.process_snapshot(os.getpid())
        with patch.object(attachments, 'process_snapshot', return_value=snapshot) as sampled:
            self.assertIsNone(attachments.problem(self.directory, self.facts))
        sampled.assert_called_once_with(os.getpid())

    def test_claim_timestamp_is_after_worker_creation_not_pending_scheduling(self):
        worker = attachments.Role(self.launch, 'recovery worker', pending=True)
        write(worker.path, {**read(worker.path), 'at': self.old})
        worker.claimed(os.getpid(), None)
        value = read(worker.path)
        self.assertNotEqual(value['at'], self.old)
        self.assertIn('recovery worker', attachments.problem(self.directory, self.facts))
