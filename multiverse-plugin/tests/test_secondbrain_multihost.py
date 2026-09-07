"""One SecondBrain connection serving a workspace that spans several remotes.

The repo, ``~/.claude/projects`` and ``~/.multiverse`` are the same files on every
machine, but a live session is pinned to the remote that started it. The chosen model
is ONE connector per workspace, running on whichever remote happens to hold the lock,
delivering events for all of them.

That only works because event production is host-independent: hooks on every remote
write signals into one shared directory, and nothing in the delivery path asks which
machine produced a signal. These tests pin that property, because the way it breaks is
silent — a daemon simply never looks where the events are, and no error is raised
anywhere. (An earlier draft of this feature keyed the state directory on host id and
did exactly that.)

Actions are the deliberate exception: driving a tmux pane needs the owning host, and
that is enforced per-verb at the delivery sites, not here.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import sb_connector, sb_notify


class OneConnectionServesEveryRemoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.dict(os.environ, {"MULTIVERSE_ROOT": self.temp.name}))
        self.cwd = str(Path(self.temp.name, "workspace").resolve())
        Path(self.cwd).mkdir()
        # One enrolment, shared by every remote on this filesystem.
        sb = Path(self.temp.name) / "secondbrain"
        sb.mkdir(parents=True, exist_ok=True)
        (sb / "device.key.json").write_text(json.dumps({
            "device_id": "dev", "pinned_server_fingerprint": "FP",
            "granted_workspaces": [self.cwd], "device_name": "shared",
        }))
        (sb / "state.json").write_text(json.dumps(
            {"seq_hw": 0, "seeded": [], "exposed": [self.cwd]}
        ))

    def _as_host(self, host: str):
        return patch.dict(os.environ, {"MULTIVERSE_HOST_ID": host})

    # -- one connector, not one per machine --------------------------------------

    def test_every_remote_resolves_the_same_state_directory(self) -> None:
        with self._as_host("remote-a"):
            a = sb_connector.sb_dir()
        with self._as_host("remote-b"):
            b = sb_connector.sb_dir()
        self.assertEqual(a, b)

    def test_there_is_one_lock_for_the_whole_filesystem(self) -> None:
        # A per-host lock would let two daemons run, and they would then share one
        # device_id (SB evicts the older socket), one seq cursor (colliding
        # allocations corrupt resume, invisibly, because SB dedups on event_id) and
        # one outbox. The single lock is what prevents all three.
        with self._as_host("remote-a"):
            a = sb_connector.lock_path()
        with self._as_host("remote-b"):
            b = sb_connector.lock_path()
        self.assertEqual(a, b)

    # End-to-end lock CONTENTION needs real process separation -- the owner file is
    # keyed on pid, so two simulated hosts in one process share it. That is covered by
    # test_atomic_state's subprocess test. What is testable here is the decision the
    # contention relies on: whether a foreign lock reads as live.

    def test_a_live_remotes_lock_reads_as_held(self) -> None:
        with self._as_host("remote-a"):
            sb_connector.acquire_lock()
        with self._as_host("remote-b"):
            self.assertIsNotNone(
                sb_connector.lock_holder(), "a live remote's lock must not be taken"
            )

    def test_a_dead_remotes_lock_reads_as_reclaimable(self) -> None:
        # The counterpart: a preempted container must not hold the workspace's only
        # connector slot forever. Its pid means nothing here, so freshness decides.
        with self._as_host("remote-a"):
            sb_connector.acquire_lock()
        stale = json.loads(sb_connector.lock_path().read_text())
        stale["heartbeat"] = "2020-01-01T00:00:00Z"
        sb_connector.lock_path().write_text(json.dumps(stale))
        with self._as_host("remote-b"):
            self.assertIsNone(
                sb_connector.lock_holder(), "a dead remote must not block the workspace"
            )

    # -- events produced anywhere are deliverable from anywhere ------------------

    def test_hooks_on_every_remote_write_to_the_same_queue(self) -> None:
        with self._as_host("remote-a"):
            a_dir = sb_notify.response_signals_dir()
        with self._as_host("remote-b"):
            b_dir = sb_notify.response_signals_dir()
        self.assertEqual(a_dir, b_dir)
        self.assertTrue(str(a_dir).startswith(str(sb_connector.sb_dir())),
                        "hooks must write where the daemon reads")

    def test_a_signal_written_on_one_remote_is_visible_on_another(self) -> None:
        with self._as_host("remote-a"):
            sb_notify.record_response_signal(self.cwd, "Alice")
        with self._as_host("remote-b"):
            pending = sb_notify.list_response_signals()
        self.assertEqual([s["agent"] for s in pending], ["Alice"],
                         "a daemon on remote-b must deliver remote-a's events")

    def test_a_signal_records_no_host_so_it_cannot_be_mis_routed(self) -> None:
        # Nothing in the record ties it to a machine, so there is no field a consumer
        # could filter on and accidentally skip another remote's work.
        with self._as_host("remote-a"):
            sb_notify.record_response_signal(self.cwd, "Alice")
            [rec] = sb_notify.list_response_signals()
        self.assertNotIn("host", rec)

    def test_recording_is_gated_on_exposure_not_on_which_remote_ran_the_hook(self) -> None:
        for host in ("remote-a", "remote-b", "remote-c"):
            with self.subTest(host=host), self._as_host(host):
                self.assertEqual(
                    sb_notify._recordable_workspace_id(self.cwd), f"dev:{self.cwd}"
                )

    def test_an_unexposed_workspace_is_still_dropped_on_every_remote(self) -> None:
        other = str(Path(self.temp.name, "other").resolve())
        for host in ("remote-a", "remote-b"):
            with self.subTest(host=host), self._as_host(host):
                self.assertIsNone(sb_notify._recordable_workspace_id(other))


if __name__ == "__main__":
    unittest.main()
