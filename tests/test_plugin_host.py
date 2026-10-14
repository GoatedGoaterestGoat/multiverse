"""Starting and checking the plugin host never races itself (PR #20 review)."""
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import host
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import now, write


class HostStartTest(PluginTestCase):
    def status(self, **values):
        write(host.host_directory(self.root) / "status.json", {"state": "running", "pid": os.getpid(),
                                                               "updated_at": now(), **values})

    def test_a_status_check_never_takes_the_supervisors_lock(self):
        held = host._held(self.root)  # a supervisor holds it
        self.addCleanup(held.close)
        with mock.patch.object(host.fcntl, "flock", side_effect=AssertionError("a status check took the lock")):
            self.assertFalse(host.running(self.root))  # no heartbeat yet
            self.status()
            self.assertTrue(host.running(self.root))

    def test_running_needs_a_live_pid_and_a_recent_heartbeat(self):
        self.status()
        self.assertTrue(host.running(self.root))
        self.status(updated_at="2000-01-01T00:00:00+00:00")
        self.assertFalse(host.running(self.root))  # stale: the supervisor stopped beating
        self.status(pid=2 ** 22 + 12345)
        self.assertFalse(host.running(self.root))  # no such process
        self.status(state="stopped")
        self.assertFalse(host.running(self.root))

    def test_a_second_start_that_finds_the_session_is_already_starting(self):
        from multiverse.terminal import tmux

        def duplicate(*arguments, **options):
            self.status()  # the first start's supervisor comes up
            raise RuntimeError("duplicate session: mv2-plugins-1234abcd")
        with mock.patch.object(tmux.Tmux, "create", duplicate):
            result = host.start(self.root)
        self.assertEqual((result["started"], result["running"]), (False, True))
