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
import uuid
from pathlib import Path
from unittest.mock import patch

from src import hosts, sb_actions, sb_connector, sb_notify, sb_queries, sessions


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


class ActionsAreHostBoundTests(unittest.TestCase):
    """Delivery needs the owning machine, and must say so.

    Both verbs here reached tmux with no host check. They still failed for a foreign
    target -- the pane simply is not on this machine -- but with a reason that was
    false: "no live tmux session" for an agent that is running perfectly well
    somewhere else. That sends the user after the wrong problem.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.dict(os.environ, {"MULTIVERSE_ROOT": self.temp.name}))
        self.calls: list = []
        self.enterContext(patch.object(
            sb_actions, "_run_tmux", lambda *a, **k: self.calls.append(a) or (True, "")))

    def _row(self, host: str, *, fresh_heartbeat: bool) -> sessions.DashboardRow:
        # A unique id per row, because Session.dir resolves MULTIVERSE_ROOT once per
        # process: a fixed id would make every test share one session directory, and a
        # heartbeat written by one test would silently make another read the session as
        # alive. Patching the env cannot prevent that.
        sess = sessions.Session(
            id=f"s-{uuid.uuid4().hex[:8]}", cwd="/ws", created_at=1,
            status="waiting", status_ts=1,
            name="Alice", runtime=sessions.RUNTIME_CLAUDE, host=host,
        )
        if fresh_heartbeat:
            sess.dir.mkdir(parents=True, exist_ok=True)
            hosts.touch_heartbeat(sess.dir)
        return sessions.DashboardRow(
            name="Alice", cwd="/ws", status="waiting", status_ts=1, session=sess,
            claude_uuid="u-1", preview="", is_ghost=False, folded_at=None,
        )

    def _as(self, host: str):
        return patch.dict(os.environ, {"MULTIVERSE_HOST_ID": host})

    def test_permission_refuses_a_foreign_target_and_types_nothing(self) -> None:
        row = self._row("remote-a", fresh_heartbeat=True)
        conn = object.__new__(sb_connector.SBConnector)
        with patch.object(sessions, "find_agent_by_name", lambda c, n: row), \
                self._as("remote-b"):
            code, _, msg = conn._deliver_permission_blocking(
                "/ws", "Alice", "t1", "deny", None)
        self.assertEqual(code, "deliver_failed")
        self.assertIn("remote-a", msg)
        self.assertEqual(self.calls, [], "must not drive a pane it cannot reach")

    def test_peek_names_the_host_instead_of_claiming_no_session(self) -> None:
        row = self._row("remote-a", fresh_heartbeat=True)
        with patch.object(sessions, "find_agent_by_name", lambda c, n: row), \
                self._as("remote-b"):
            kind, payload = sb_queries.peek_agent("/ws", "Alice")
        self.assertEqual(kind, "deliver_failed")
        self.assertIn("remote-a", payload["message"])
        self.assertNotIn("no live tmux", payload["message"])

    def test_peek_flags_a_remote_that_stopped_checking_in(self) -> None:
        # foreign_stale is reporting only -- it must never read as terminal, because
        # recovering a preempted container is the user's explicit force-kill call.
        row = self._row("remote-a", fresh_heartbeat=False)
        with patch.object(sessions, "find_agent_by_name", lambda c, n: row), \
                self._as("remote-b"):
            kind, payload = sb_queries.peek_agent("/ws", "Alice")
        self.assertEqual(kind, "deliver_failed")
        self.assertIn("not checked in", payload["message"])
        for terminal in ("ended", "dead", "gone forever"):
            self.assertNotIn(terminal, payload["message"].replace("may be gone", ""))

    def test_a_local_agent_is_not_refused(self) -> None:
        row = self._row("remote-a", fresh_heartbeat=True)
        with patch.object(sessions, "find_agent_by_name", lambda c, n: row), \
                self._as("remote-a"), \
                patch.object(sessions, "tmux_session_exists", lambda n: False):
            kind, _ = sb_queries.peek_agent("/ws", "Alice")
        self.assertEqual(kind, "not_running", "local rows keep their existing path")


class HostReportingTests(unittest.TestCase):
    """SB must be able to say WHERE an agent is, not just how it is.

    Without this a row reads ``working``, SB tells the user the agent is working, and
    then an action fails with "running on host X" that SB could neither anticipate nor
    explain. The worse case is a remote that died: nothing corrects the row, so SB
    would keep reporting ``working`` for a machine that no longer exists.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.dict(os.environ, {"MULTIVERSE_ROOT": self.temp.name}))

    def _row(self, *, host: str | None, fresh: bool = True, status: str = "working"):
        sess = None
        if host is not None:
            sess = sessions.Session(
                id=f"s-{uuid.uuid4().hex[:8]}", cwd="/ws", created_at=1, status=status,
                status_ts=1, name="Alice", runtime=sessions.RUNTIME_CLAUDE, host=host,
            )
            if fresh:
                sess.dir.mkdir(parents=True, exist_ok=True)
                hosts.touch_heartbeat(sess.dir)
        return sessions.DashboardRow(
            name="Alice", cwd="/ws", status=status, status_ts=1, session=sess,
            claude_uuid=None, preview="", is_ghost=False, folded_at=None,
        )

    def _report(self, row) -> dict:
        with patch.object(sb_queries, "_supported_rows", lambda cwd: [row]), \
                patch.object(sessions, "tmux_session_exists", lambda n: True):
            return sb_queries.list_agents("/ws")[0]

    def test_a_local_agent_is_reported_local(self) -> None:
        with patch.dict(os.environ, {"MULTIVERSE_HOST_ID": "remote-a"}):
            row = self._report(self._row(host="remote-a"))
        self.assertEqual(row["host_id"], "remote-a")
        self.assertEqual(row["host_state"], hosts.HOST_LOCAL)

    def test_an_agent_on_another_remote_names_that_remote(self) -> None:
        row = self._row(host="remote-a")
        with patch.dict(os.environ, {"MULTIVERSE_HOST_ID": "remote-b"}):
            out = self._report(row)
        self.assertEqual(out["host_id"], "remote-a")
        self.assertEqual(out["host_state"], hosts.HOST_FOREIGN)

    def test_a_remote_that_stopped_checking_in_is_flagged(self) -> None:
        row = self._row(host="remote-a", fresh=False)
        with patch.dict(os.environ, {"MULTIVERSE_HOST_ID": "remote-b"}):
            out = self._report(row)
        self.assertEqual(out["host_state"], hosts.HOST_FOREIGN_STALE)

    def test_status_still_means_status_for_a_foreign_agent(self) -> None:
        # The contract point: working on another machine IS working. Folding host into
        # status would destroy the distinction needed to explain a refused action.
        row = self._row(host="remote-a", status="working")
        with patch.dict(os.environ, {"MULTIVERSE_HOST_ID": "remote-b"}):
            out = self._report(row)
        self.assertEqual(out["status"], "working")

    def test_a_row_with_no_session_claims_no_host(self) -> None:
        out = self._report(self._row(host=None, status="dormant"))
        self.assertEqual(out["host_id"], "")
        self.assertEqual(out["host_state"], hosts.HOST_LOCAL, "startable here")

    def test_the_dashboard_shape_carries_it_too(self) -> None:
        row = self._row(host="remote-a")
        with patch.object(sb_queries, "_supported_rows", lambda cwd: [row]), \
                patch.object(sessions, "tmux_session_exists", lambda n: True), \
                patch.dict(os.environ, {"MULTIVERSE_HOST_ID": "remote-b"}):
            out = sb_queries.dashboard("/ws")[0]
        self.assertEqual(out["host_id"], "remote-a")
        self.assertEqual(out["host_state"], hosts.HOST_FOREIGN)


if __name__ == "__main__":
    unittest.main()
