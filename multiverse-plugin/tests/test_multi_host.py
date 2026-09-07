"""Multi-host core: identity, proof of life, and the cross-host wake relay.

The deployment under test is one workspace whose ``MULTIVERSE_ROOT`` is shared
by several remotes. Every file is visible everywhere; no tmux server is. So
each test here is really the same question asked about a different subsystem:
when this host cannot see another host's processes, does the code say "I don't
know" — or does it say "dead" and act on it?
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import hosts, sessions, spawn_requests, wake_relay

LOCAL = "host-a"
REMOTE = "host-b"


class HostIdentityTests(unittest.TestCase):
    def test_env_override_beats_an_unstable_hostname(self) -> None:
        with patch.dict(os.environ, {"MULTIVERSE_HOST_ID": "pinned-id"}):
            self.assertEqual(hosts.current_host(), "pinned-id")
            self.assertTrue(hosts.host_is_foreign("somewhere-else"))
            self.assertFalse(hosts.host_is_foreign("pinned-id"))
        with patch.dict(os.environ, {"MULTIVERSE_HOST_ID": "   "}):
            with patch.object(hosts.socket, "gethostname", return_value="laptop.local"):
                self.assertEqual(hosts.current_host(), "laptop.local")

    def test_unstamped_host_counts_as_local(self) -> None:
        # Sessions predating the field, and any host whose id could not be
        # resolved at creation. Treating them as foreign would strand every
        # legacy row behind guards it was never meant to be behind.
        self.assertFalse(hosts.host_is_foreign(None))
        self.assertFalse(hosts.host_is_foreign(""))


class HeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_age_and_staleness_track_the_last_proof_of_life(self) -> None:
        self.assertIsNone(hosts.heartbeat_age(self.dir))
        hosts.touch_heartbeat(self.dir)
        self.assertLess(hosts.heartbeat_age(self.dir), 5.0)
        self.assertFalse(hosts.heartbeat_is_stale(self.dir))
        old = time.time() - hosts.STALE_HEARTBEAT_SECONDS - 1
        os.utime(hosts.heartbeat_path(self.dir), (old, old))
        self.assertTrue(hosts.heartbeat_is_stale(self.dir))

    def test_never_written_reads_as_stale_not_as_alive(self) -> None:
        # The alternative — "unmeasured, so believe the status" — is what
        # leaves a preempted remote reporting `working` forever, because with
        # no local tmux and no heartbeat nothing else about the row ever
        # changes again.
        self.assertTrue(hosts.heartbeat_is_stale(self.dir))
        self.assertEqual(hosts.host_state(REMOTE, self.dir), hosts.HOST_FOREIGN_STALE)

    def test_throttle_skips_a_fresh_heartbeat_and_refreshes_an_old_one(self) -> None:
        self.assertTrue(hosts.touch_heartbeat_throttled(self.dir))
        self.assertFalse(hosts.touch_heartbeat_throttled(self.dir))
        old = time.time() - hosts.HEARTBEAT_INTERVAL_SECONDS - 1
        os.utime(hosts.heartbeat_path(self.dir), (old, old))
        self.assertTrue(hosts.touch_heartbeat_throttled(self.dir))

    def test_local_rows_never_consult_the_heartbeat(self) -> None:
        # Local liveness has better evidence available (tmux, PIDs), so a
        # local row with no heartbeat must not be reported as suspect.
        with patch.object(hosts, "current_host", return_value=LOCAL):
            self.assertEqual(hosts.host_state(LOCAL, self.dir), hosts.HOST_LOCAL)
            self.assertEqual(hosts.host_state(None, self.dir), hosts.HOST_LOCAL)

    def test_unwritable_heartbeat_never_raises(self) -> None:
        blocked = self.dir / "file" / "under" / "a" / "file"
        (self.dir / "file").write_text("not a directory")
        hosts.touch_heartbeat(blocked)  # must not raise
        self.assertIsNone(hosts.heartbeat_age(blocked))


class SessionHostTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "sessions"
        self.state.mkdir()
        patcher = patch.object(sessions, "STATE_DIR", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        host = patch.object(sessions, "current_host", return_value=LOCAL)
        host.start()
        self.addCleanup(host.stop)

    def write_session(
        self,
        session_id: str = "sid1",
        *,
        host: str | None = REMOTE,
        status: str = "idle",
        name: str = "remote-agent",
        runtime: str = "claude",
        cwd: str = "/workspace",
    ) -> sessions.Session:
        directory = self.state / session_id
        directory.mkdir(parents=True, exist_ok=True)
        meta = {
            "cwd": cwd, "created_at": time.time(), "runtime": runtime, "name": name,
        }
        if host is not None:
            meta["host"] = host
        (directory / "meta.json").write_text(json.dumps(meta))
        (directory / "status.json").write_text(
            json.dumps({"status": status, "ts": time.time()})
        )
        loaded = sessions.load_session(session_id)
        assert loaded is not None
        return loaded


class ForeignSessionTests(SessionHostTestCase):
    def test_classification_and_shared_vocabulary(self) -> None:
        foreign = self.write_session()
        self.assertTrue(sessions.is_foreign_host(foreign))
        self.assertEqual(
            sessions.session_host_state(foreign), hosts.HOST_FOREIGN_STALE
        )
        hosts.touch_heartbeat(foreign.dir)
        self.assertEqual(sessions.session_host_state(foreign), hosts.HOST_FOREIGN)

        mine = self.write_session("sid2", host=LOCAL)
        self.assertFalse(sessions.is_foreign_host(mine))
        self.assertEqual(sessions.session_host_state(mine), hosts.HOST_LOCAL)

        legacy = self.write_session("sid3", host=None)
        self.assertFalse(sessions.is_foreign_host(legacy))
        self.assertEqual(sessions.session_host_state(legacy), hosts.HOST_LOCAL)

    def test_patching_sessions_current_host_governs_every_decision(self) -> None:
        # The host comparison must read this module's name, not another
        # module's globals — a guard that quietly ignores the identity it was
        # handed is both untestable and the bug class this file guards.
        foreign = self.write_session()
        with patch.object(sessions, "current_host", return_value=REMOTE):
            self.assertFalse(sessions.is_foreign_host(foreign))
            self.assertEqual(sessions.session_host_state(foreign), hosts.HOST_LOCAL)

    def test_a_dashboard_never_buries_a_session_it_cannot_find(self) -> None:
        """The reported bug: a dashboard on a second remote sees every session
        under the shared root but only its own tmux, so every session on
        another remote looks missing — and ``ended`` is terminal, so writing
        it reaches across the mount and kills a running agent for good."""
        elsewhere = self.write_session("sid-remote", host=REMOTE, status="working")
        unstamped = self.write_session("sid-legacy", host=None, status="idle")
        vouched = self.write_session("sid-vouched", host=LOCAL, status="idle")
        hosts.touch_heartbeat(vouched.dir)

        with patch.object(sessions, "tmux_session_exists", return_value=False):
            found = {s.id: s for s in sessions.list_sessions()}

        for session in (elsewhere, unstamped, vouched):
            with self.subTest(session=session.id):
                self.assertNotEqual(found[session.id].status, "ended")
                persisted = json.loads((session.dir / "status.json").read_text())
                self.assertNotEqual(persisted["status"], "ended")

    def test_identical_host_ids_do_not_defeat_the_liveness_signal(self) -> None:
        """Two containers reporting the SAME hostname, which is the shape that
        broke in production. Every identity comparison collapses — the
        recorded host matches ours, and the heartbeat carries our own id — so
        the decision cannot rest on identity at all. What still distinguishes
        them: this host's tmux does not have the session, so whoever is
        keeping that heartbeat warm, it is not us."""
        theirs = self.write_session("sid-twin", host=LOCAL, status="working")
        with patch.object(hosts, "current_host", return_value=LOCAL):
            hosts.touch_heartbeat(theirs.dir)
        self.assertEqual(hosts.heartbeat_host(theirs.dir), LOCAL)  # our own id
        self.assertFalse(sessions.is_foreign_host(theirs))  # looks local
        self.assertFalse(sessions.may_declare_dead(theirs, visible_here=False))

        with patch.object(sessions, "tmux_session_exists", return_value=False):
            found = {s.id: s for s in sessions.list_sessions()}
        self.assertEqual(found["sid-twin"].status, "working")

    def test_our_own_crashed_session_is_still_reaped(self) -> None:
        # The protection must not cost the thing liveness correction is for.
        # A heartbeat that has aged out is no longer evidence of anything.
        mine = self.write_session("sid-mine", host=LOCAL, status="working")
        hosts.touch_heartbeat(mine.dir)
        old = time.time() - hosts.STALE_HEARTBEAT_SECONDS - 1
        os.utime(hosts.heartbeat_path(mine.dir), (old, old))
        self.assertTrue(sessions.may_declare_dead(mine, visible_here=False))
        with patch.object(sessions, "tmux_session_exists", return_value=False):
            found = {s.id: s for s in sessions.list_sessions()}
        self.assertEqual(found["sid-mine"].status, "ended")

    def test_a_visible_session_is_judged_on_local_evidence_alone(self) -> None:
        # We can see it in our own tmux, so the heartbeat is ours and adds
        # nothing — a Codex runtime whose PID is gone must still be reapable.
        mine = self.write_session("sid-visible", host=LOCAL, status="working")
        hosts.touch_heartbeat(mine.dir)
        self.assertTrue(sessions.may_declare_dead(mine, visible_here=True))

    def test_live_foreign_session_is_not_reaped_by_ordinary_teardown(self) -> None:
        foreign = self.write_session()
        with patch.object(sessions, "tmux_session_exists", return_value=False):
            with self.assertRaises(RuntimeError) as caught:
                sessions.kill_session(foreign)
        self.assertIn(REMOTE, str(caught.exception))
        self.assertTrue(foreign.dir.is_dir())

    def test_force_kill_reaps_state_without_touching_anything_local(self) -> None:
        # The user's escape hatch after a container is preempted: its agents
        # never wrote `ended`, so nothing else can free their names.
        foreign = self.write_session()
        (foreign.dir / "runtime_pid").write_text("4242")
        with (
            patch.object(sessions, "tmux_session_exists", return_value=False) as tmux,
            patch.object(sessions, "_terminate_codex_runtime") as codex_kill,
            patch.object(sessions, "_terminate_tmux_pane_runtime") as pane_kill,
            patch.object(sessions.subprocess, "run") as ran,
        ):
            sessions.kill_session(foreign, force=True)
        self.assertFalse(foreign.dir.exists())
        codex_kill.assert_not_called()
        pane_kill.assert_not_called()
        ran.assert_not_called()
        tmux.assert_not_called()

    def test_ended_foreign_session_needs_no_force(self) -> None:
        # Its own SessionEnd hook wrote `ended`, wherever it ran — that is
        # first-hand evidence, so freeing the name is not a guess.
        ended = self.write_session(status="ended")
        with patch.object(sessions, "tmux_session_exists", return_value=False):
            sessions.kill_session(ended)
        self.assertFalse(ended.dir.exists())

    def test_force_decision_survives_into_the_detached_teardown(self) -> None:
        # mark_session_ending and kill_session run in different processes, so
        # an intent held only in memory would be lost in between.
        foreign = self.write_session()
        sessions.mark_session_ending(foreign)
        self.assertFalse(sessions.ending_was_forced(foreign))
        sessions.mark_session_ending(foreign, force=True)
        self.assertTrue(sessions.ending_was_forced(foreign))
        self.assertTrue(foreign.ending_marker_path.is_file())

    def test_an_ended_foreign_row_is_never_marked_doubtful(self) -> None:
        # host_state reads the heartbeat alone, so a finished agent on a
        # switched-off remote reports foreign_stale like any other. Nothing
        # about it is uncertain: its own SessionEnd hook wrote that status.
        from src import monitor

        for status in ("ended", "retiring", "retired"):
            with self.subTest(status=status):
                done = self.write_session(f"sid-{status}", status=status)
                self.assertEqual(
                    sessions.session_host_state(done), hosts.HOST_FOREIGN_STALE
                )
                self.assertNotIn("?[/]", monitor._host_note(done))
                self.assertIn(REMOTE, monitor._host_note(done))

        live = self.write_session("sid-live", status="working")
        self.assertIn("?[/]", monitor._host_note(live))

    def test_status_writes_publish_proof_of_life(self) -> None:
        mine = self.write_session("sid9", host=LOCAL)
        hosts.heartbeat_path(mine.dir).unlink(missing_ok=True)
        sessions.write_status_path(mine.status_path, "working")
        self.assertIsNotNone(hosts.heartbeat_age(mine.dir))


class SingleInstanceTests(SessionHostTestCase):
    """Exactly one wrapper per agent, across every remote sharing the root.

    Two agents on two remotes can message the same dormant agent at the same
    moment, and each auto-resume runs on its own sender's machine. Nothing
    about tmux or PIDs can arbitrate that, so the exclusion has to live in the
    shared filesystem itself.
    """

    def setUp(self) -> None:
        super().setUp()
    def claim(self, session_id: str, *, name: str, native: str, host: str) -> None:
        """What create_session does before it spawns anything."""
        with patch.object(sessions, "current_host", return_value=host):
            (self.state / session_id).mkdir(parents=True, exist_ok=True)
            (self.state / session_id / "meta.json").write_text(json.dumps({
                "cwd": "/workspace", "created_at": time.time(),
                "runtime": "claude", "name": name, "host": host,
                "resumed_from": native,
            }))
            for kind, value in sessions._session_claim_specs(
                "/workspace", "claude", name, native
            ):
                sessions._acquire_claim(sessions._claim_path(kind, value), session_id)
            sessions._assert_no_existing_wrapper_claim(
                session_id, "/workspace", "claude", name, native
            )

    def test_a_second_remote_cannot_resume_the_same_agent(self) -> None:
        self.claim("on-host-a", name="agent", native="uuid-1", host=LOCAL)
        with self.assertRaises(RuntimeError) as caught:
            self.claim("on-host-b", name="agent", native="uuid-1", host=REMOTE)
        self.assertIn("claimed", str(caught.exception))

    def test_the_native_transcript_is_claimed_even_under_a_new_name(self) -> None:
        # Same underlying conversation, different agent name: two writers on
        # one transcript would interleave turns irrecoverably.
        self.claim("on-host-a", name="agent", native="uuid-1", host=LOCAL)
        with self.assertRaises(RuntimeError):
            self.claim("on-host-b", name="different-name", native="uuid-1", host=REMOTE)

    def test_the_name_is_claimed_even_for_a_different_transcript(self) -> None:
        self.claim("on-host-a", name="agent", native="uuid-1", host=LOCAL)
        with self.assertRaises(RuntimeError):
            self.claim("on-host-b", name="agent", native="uuid-2", host=REMOTE)

    def test_claims_live_on_the_shared_mount_not_per_host(self) -> None:
        # The whole guarantee rests on this: a claim another remote can't see
        # is not a claim at all.
        self.claim("on-host-a", name="agent", native="uuid-1", host=LOCAL)
        claim_dir = self.state.parent / sessions._CLAIM_DIR_NAME
        self.assertTrue(any(claim_dir.iterdir()))
        for path in claim_dir.iterdir():
            # A symlink, because its creation is atomic: two remotes racing
            # cannot both believe they won.
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), "on-host-a")

    def test_a_released_claim_frees_the_agent_for_another_remote(self) -> None:
        self.claim("on-host-a", name="agent", native="uuid-1", host=LOCAL)
        session = sessions.load_session("on-host-a")
        assert session is not None
        with patch.object(sessions, "current_host", return_value=LOCAL):
            sessions.kill_session(session)
        self.claim("on-host-b", name="agent", native="uuid-1", host=REMOTE)


def _load_heartbeat_module(path: Path):
    """Import the standalone writer script for in-process assertions."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("multiverse_heartbeat", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HeartbeatWriterTests(unittest.TestCase):
    """The per-session writer, which publishes on behalf of a process it does
    not control. Everything here is about it refusing to outlive that process:
    a heartbeat still being written for a dead session is a claim no other
    remote can disprove, and only force-kill could then clear the row."""

    SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "heartbeat.py"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def run_writer(self, *args: str, wait: float = 1.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args],
            capture_output=True, timeout=30, check=False,
        )

    def test_writes_while_its_wrapper_is_alive(self) -> None:
        # Popen makes this test process the parent, which is what the wrapper
        # would be, so the pid it is told to expect is genuinely correct.
        proc = subprocess.Popen(
            [sys.executable, str(self.SCRIPT), str(self.dir), str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        deadline = time.time() + 10
        while time.time() < deadline and not hosts.heartbeat_path(self.dir).exists():
            time.sleep(0.05)
        self.assertTrue(hosts.heartbeat_path(self.dir).exists())
        self.assertEqual(hosts.heartbeat_host(self.dir), hosts.current_host())

    def test_a_wrapper_already_gone_is_never_vouched_for(self) -> None:
        # The startup race: if the wrapper dies between spawning this writer
        # and the writer starting, reading getppid() here would record init as
        # "the wrapper" and match it forever. Being told the pid up front is
        # what closes that, so a stale expectation must produce no write at
        # all — not even the first one.
        result = self.run_writer(str(self.dir), str(os.getpid() + 1))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(hosts.heartbeat_path(self.dir).exists())

    def test_pid_one_is_a_legitimate_wrapper(self) -> None:
        # Rejecting init by number would break containers, where a wrapper
        # really can be PID 1. Whether the parent is still THE parent is the
        # test, not what number it happens to have.
        module = _load_heartbeat_module(self.SCRIPT)
        with patch.object(module.os, "getppid", return_value=1), \
             patch.object(module, "touch_heartbeat") as touched, \
             patch.object(module.time, "sleep", side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):
                module.main([str(self.dir), "1"])
        touched.assert_called_once_with(self.dir)

    def test_a_wrapper_replaced_after_startup_stops_the_writer(self) -> None:
        # Ordinary re-parenting: the wrapper was alive when we began and is
        # gone now. One heartbeat published, then nothing further.
        module = _load_heartbeat_module(self.SCRIPT)
        parents = iter([4242, 4242, 1])
        with patch.object(module.os, "getppid", side_effect=lambda: next(parents)), \
             patch.object(module, "touch_heartbeat") as touched, \
             patch.object(module.time, "sleep"):
            self.assertEqual(module.main([str(self.dir), "4242"]), 0)
        self.assertEqual(touched.call_count, 2)

    def test_bad_arguments_write_nothing(self) -> None:
        for args in ([], [str(self.dir)], [str(self.dir), "not-a-pid"]):
            with self.subTest(args=args):
                result = self.run_writer(*args)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(hosts.heartbeat_path(self.dir).exists())


class SpawnRequestHostTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(spawn_requests, "SPAWN_REQUESTS_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        host = patch.object(spawn_requests, "current_host", return_value=LOCAL)
        host.start()
        self.addCleanup(host.stop)

    def test_a_foreign_caller_is_never_reaped_as_an_orphan(self) -> None:
        # Its tmux lives on another machine, so `tmux has-session` always says
        # no — and reaping DELETES the request while the caller blocks on it
        # forever, waiting for an answer no monitor will ever be asked for.
        spawn_requests.write_spawn_request(
            "req-1", "remote-agent", "/workspace", "do a thing", "newbie",
            "multiverse-sid1",
        )
        path = spawn_requests.request_path("req-1")
        data = json.loads(path.read_text())
        data["caller_host"] = REMOTE
        path.write_text(json.dumps(data))
        with patch.object(spawn_requests.subprocess, "run") as ran:
            pending = spawn_requests.list_pending_requests("/workspace")
        self.assertEqual([item["id"] for item in pending], ["req-1"])
        ran.assert_not_called()  # never even asks local tmux about a foreign caller
        self.assertTrue(path.is_file())

    def test_a_local_caller_that_is_gone_is_still_reaped(self) -> None:
        spawn_requests.write_spawn_request(
            "req-2", "local-agent", "/workspace", "do a thing", "newbie",
            "multiverse-sid2",
        )
        path = spawn_requests.request_path("req-2")
        data = json.loads(path.read_text())
        data["caller_host"] = LOCAL
        path.write_text(json.dumps(data))
        with patch.object(spawn_requests, "_caller_alive", return_value=False):
            self.assertEqual(spawn_requests.list_pending_requests("/workspace"), [])
        self.assertFalse(path.is_file())


class WakeRelayTests(SessionHostTestCase):
    def setUp(self) -> None:
        super().setUp()
        requests_dir = self.root / "wake_requests"
        patcher = patch.object(wake_relay, "WAKE_REQUESTS_DIR", requests_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        inbox = patch.object(wake_relay, "pending_inbox_count", return_value=1)
        self.pending = inbox.start()
        self.addCleanup(inbox.stop)

    def own(self, session: sessions.Session) -> None:
        """Become the host that owns ``session``, with its pane present."""
        patcher = patch.object(sessions, "current_host", return_value=session.host)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmux = patch.object(sessions, "tmux_session_exists", return_value=True)
        tmux.start()
        self.addCleanup(tmux.stop)

    def test_one_request_per_session_however_many_senders(self) -> None:
        session = self.write_session()
        self.assertTrue(wake_relay.request_wake(session, requested_by="sender-1"))
        self.assertFalse(wake_relay.request_wake(session, requested_by="sender-2"))
        self.assertEqual(len(wake_relay.list_requests()), 1)
        # Reading an inbox drains all of it, so a second nudge would only burn
        # a turn arriving at an empty one.

    def test_only_the_owning_host_can_claim_a_request(self) -> None:
        session = self.write_session()
        wake_relay.request_wake(session)
        with patch.object(sessions, "tmux_session_exists", return_value=True):
            self.assertEqual(wake_relay.claimable_wakes(), [])  # we are host-a
        self.own(session)
        self.assertEqual([r.session_id for r in wake_relay.claimable_wakes()], ["sid1"])

    def test_dispatch_fields_come_from_the_session_not_the_request(self) -> None:
        # A request is a note saying "nudge session X". Trusting the rest of
        # it would let a stale pane name send keystrokes into whatever agent
        # now holds that pane.
        session = self.write_session()
        wake_relay.request_wake(session)
        path = wake_relay.request_path(session.id)
        forged = json.loads(path.read_text())
        forged.update(
            tmux_name="multiverse-someone-else",
            name="someone-else",
            cwd="/elsewhere",
            runtime="codex",
        )
        path.write_text(json.dumps(forged))
        self.own(session)
        claimed = wake_relay.claimable_wakes()
        self.assertEqual(
            [(c.tmux_name, c.name, c.cwd, c.runtime) for c in claimed],
            [(session.tmux_name, "remote-agent", "/workspace", "claude")],
        )

    def test_states_that_cannot_take_a_prompt_keep_the_request_pending(self) -> None:
        for status in ("starting", "waiting", "compacting"):
            with self.subTest(status=status):
                session = self.write_session(status=status)
                wake_relay.request_wake(session)
                self.own(session)
                self.assertEqual(wake_relay.claimable_wakes(), [])
                self.assertEqual(len(wake_relay.list_requests()), 1)
                wake_relay.request_path(session.id).unlink()

    def test_a_session_being_torn_down_is_never_typed_into(self) -> None:
        for status in ("ended", "ending", "retiring", "retired"):
            with self.subTest(status=status):
                session = self.write_session(status=status)
                wake_relay.request_wake(session)
                self.own(session)
                self.assertEqual(wake_relay.claimable_wakes(), [])
                wake_relay.request_path(session.id).unlink()

    def test_delivery_reports_and_retires_the_request(self) -> None:
        session = self.write_session()
        wake_relay.request_wake(session)
        self.own(session)
        typed: list[str] = []
        delivered = wake_relay.relay_pending_wakes(
            lambda request: typed.append(request.tmux_name) or True
        )
        self.assertEqual((delivered, typed), (1, [session.tmux_name]))
        self.assertEqual(wake_relay.list_requests(), [])

    def test_a_failed_or_raising_nudge_is_retried_not_dropped(self) -> None:
        session = self.write_session()
        wake_relay.request_wake(session)
        self.own(session)
        self.assertEqual(wake_relay.relay_pending_wakes(lambda _r: False), 0)
        self.assertEqual(len(wake_relay.list_requests()), 1)

        def explode(_request):
            raise RuntimeError("composer busy")

        self.assertEqual(wake_relay.relay_pending_wakes(explode), 0)
        self.assertEqual(len(wake_relay.list_requests()), 1)
        self.assertEqual(wake_relay.relay_pending_wakes(lambda _r: True), 1)

    def test_an_already_delivered_message_is_retired_without_typing(self) -> None:
        session = self.write_session()
        wake_relay.request_wake(session)
        self.own(session)
        self.pending.return_value = 0  # their own hooks collected it
        typed: list[str] = []
        delivered = wake_relay.relay_pending_wakes(
            lambda request: typed.append(request.tmux_name) or True
        )
        self.assertEqual((delivered, typed), (0, []))
        self.assertEqual(wake_relay.list_requests(), [])

    def test_an_outstanding_prompt_suppresses_a_second_one(self) -> None:
        session = self.write_session()
        wake_relay.request_wake(session)
        self.own(session)
        typed: list[str] = []
        with patch.object(wake_relay, "pending_inbox_count", return_value=1):
            with patch("src.inbox.wake_is_outstanding", return_value=True):
                wake_relay.relay_pending_wakes(
                    lambda request: typed.append(request.tmux_name) or True
                )
        self.assertEqual(typed, [])
        self.assertEqual(wake_relay.list_requests(), [])

    def test_an_outage_never_expires_a_still_unread_nudge(self) -> None:
        # The case the relay exists for: the owning remote is down, the
        # message is unread, and this request is the only thing that will make
        # its agent look when the host comes back.
        session = self.write_session()
        wake_relay.request_wake(session)
        path = wake_relay.request_path(session.id)
        aged = json.loads(path.read_text())
        aged["requested_at"] = time.time() - wake_relay.MAX_AGE_SECONDS * 10
        path.write_text(json.dumps(aged))
        self.assertEqual(wake_relay.gc(), 0)
        self.assertEqual(len(wake_relay.list_requests()), 1)

    def test_gc_clears_requests_that_can_no_longer_do_any_good(self) -> None:
        session = self.write_session()
        wake_relay.request_wake(session)
        self.pending.return_value = 0
        self.assertEqual(wake_relay.gc(), 1)

        other = self.write_session("sid7", name="gone-agent")
        wake_relay.request_wake(other)
        for child in other.dir.iterdir():
            child.unlink()
        other.dir.rmdir()
        self.assertEqual(wake_relay.gc(), 1)
        self.assertEqual(wake_relay.list_requests(), [])

    def test_an_unreadable_share_decides_nothing(self) -> None:
        session = self.write_session()
        wake_relay.request_wake(session)
        self.pending.side_effect = OSError("share is down")
        self.assertEqual(wake_relay.gc(), 0)
        self.assertEqual(len(wake_relay.list_requests()), 1)

    def test_malformed_request_files_are_ignored_not_fatal(self) -> None:
        directory = wake_relay.WAKE_REQUESTS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "broken.json").write_text("{not json")
        (directory / "list.json").write_text("[1, 2]")
        (directory / "nameless.json").write_text(json.dumps({"host": REMOTE}))
        (directory / "notjson.txt").write_text("ignored")
        self.assertEqual(wake_relay.list_requests(), [])
        self.assertEqual(wake_relay.claimable_wakes(), [])

    def test_missing_request_directory_is_not_an_error(self) -> None:
        self.assertEqual(wake_relay.list_requests(), [])
        self.assertEqual(wake_relay.claimable_wakes(), [])
        self.assertEqual(wake_relay.gc(), 0)


if __name__ == "__main__":
    unittest.main()
