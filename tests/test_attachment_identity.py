"""Recovery never waits forever on an unclaimed role, and never drops a claimed one."""
from datetime import datetime, timedelta, timezone
import os
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

    def test_when_a_pending_role_expires(self):
        """An unclaimed pending attachment role expires, but one with an unknown or future date does not, and a claim
        is timed after the worker exists, not when it was scheduled; fragile because it is clock arithmetic on saved
        timestamps, and a break wedges resume or drops a live worker (1022b20)."""
        worker = attachments.Role(self.launch, 'recovery worker', pending=True)
        value = {**read(worker.path), 'at': self.old}
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for at, expired in ((self.old, True), ('bad-date', False), (None, False), (future, False)):
            with self.subTest(at=at):
                write(worker.path, {**value, 'at': at})
                problem = attachments.problem(self.directory, self.facts)
                if expired:
                    self.assertIsNone(problem)
                else:
                    self.assertIn('recovery worker', problem)
        with self.subTest('claimed after an old schedule'):
            write(worker.path, value)
            worker.claimed(os.getpid(), None)
            self.assertNotEqual(read(worker.path)['at'], self.old)
            self.assertIn('recovery worker', attachments.problem(self.directory, self.facts))
