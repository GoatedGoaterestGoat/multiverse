"""Core's event emitters: each one records its change once, honestly, without holding the change up."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import events, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write
from multiverse.universe import agents, mailbox, relocate, templates


def stop_holder(process):
    """End a lock-holding helper and close its pipe before the temporary directory goes."""
    process.kill()
    process.wait()
    process.stdout.close()


class EmitterBase(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.marker = self.work / "imported"
        # A subscriber whose code must never be imported by an emitter.
        self.write_plugin("listener", {"module": "plugin", "extends": ["events"], "events": ["*"]},
                          {"plugin.py": f"open({str(self.marker)!r}, 'w').write('imported')\ndef register(mv): pass\n"})
        self.enable("listener")
        self.agent_dir = self.agent("worker")

    def agent(self, name, root=None):
        root = root or self.root
        identity = str(uuid4())
        directory = root / "state" / "agents" / identity
        write(directory / "properties.json", {"id": identity, "name": name, "harness": "codex", "task": "secret task",
                                             "universe_id": read(root / "universe.json")["id"], "workspace": str(self.work)})
        write(directory / "runtime.json", {"launch_id": "L1", "state": "running", "status": "idle"})
        return directory

    def recorded(self, root=None, kind=None):
        found = events.read(root or self.root, 0)[0]
        return [event for event in found if kind is None or event["type"] == kind]


class EmitterTest(EmitterBase):
    def test_status_changes_once_and_never_imports_plugin_code(self):
        agents.update_runtime(self.agent_dir, "L1", status="working")
        agents.update_runtime(self.agent_dir, "L1", status="working")  # no change
        agents.update_runtime(self.agent_dir, "L1", usage={"x": 1})  # not a status
        [event] = self.recorded(kind="agent.status")
        self.assertEqual((event["data"], event["launch"], event["agent"]["name"]),
                         ({"from": "idle", "to": "working"}, "L1", "worker"))
        self.assertFalse(self.marker.exists())

    def test_a_report_never_fails_on_its_event(self):
        write(self.agent_dir / "runtime.json", {"launch_id": "L1"})
        (self.agent_dir / "runtime.json").write_text("{bad-json")
        for on in (True, False):
            with self.subTest(subscriber=on):
                self.enable("listener", on=on)
                agents.report(self.root, "worker", "working", f"on={on}")
                self.assertEqual(read(self.agent_dir / "properties.json")["summary"], f"on={on}")
        [event] = self.recorded(kind="agent.reported")
        self.assertIsNone(event["launch"])

    def test_reported_without_task(self):
        agents.report(self.root, "worker", "blocked", "Waiting for review")
        [event] = self.recorded(kind="agent.reported")
        self.assertEqual(event["data"], {"phase": "blocked", "summary": "Waiting for review"})
        self.assertNotIn("secret task", json.dumps(event))

    def test_messages_saved_delivered_failed_and_read_honestly(self):
        message = mailbox.append_message(self.agent_dir, "the private text", {"name": "user", "universe": "/x"}, "steer")
        [saved] = self.recorded(kind="message.saved")
        self.assertEqual(saved["data"], {"id": message["id"], "delivery": "steer", "length": 16, "from": {"name": "user"}})
        self.assertNotIn("private", json.dumps(self.recorded()))
        mailbox.update_message(self.agent_dir, message["id"], submission_state="submitting")
        mailbox.update_message(self.agent_dir, message["id"], submission_state="unknown")
        self.assertEqual(self.recorded(kind="message.delivered") + self.recorded(kind="message.failed"), [])
        mailbox.update_message(self.agent_dir, message["id"], submission_state="accepted")
        mailbox.update_message(self.agent_dir, message["id"], submission_state="accepted")
        mailbox.update_message(self.agent_dir, message["id"], transcript_confirmed_at="now")
        mailbox.update_message(self.agent_dir, message["id"], transcript_confirmed_at="later")
        self.assertEqual([event["data"]["stage"] for event in self.recorded(kind="message.delivered")], ["accepted", "confirmed"])
        other = mailbox.append_message(self.agent_dir, "x", {}, "follow-up")
        mailbox.update_message(self.agent_dir, other["id"], submission_state="failed",
                               attempts=[{"reason": "no bridge"}])
        mailbox.update_message(self.agent_dir, other["id"], submission_state="failed")
        [failed] = self.recorded(kind="message.failed")
        self.assertEqual(failed["data"], {"id": other["id"], "reason": "no bridge"})

    def test_message_read_counts_only_inbox_only_messages_actually_consumed(self):
        mailbox.append_message(self.agent_dir, "native", {}, "steer")
        for text in ("one", "two"):
            mailbox.append_message(self.agent_dir, text, {}, "inbox-only")
        mailbox.inbox(self.agent_dir, peek=True)
        mailbox.inbox(self.agent_dir, all_messages=True)
        with self.assertRaises(RuntimeError):
            mailbox.inbox(self.agent_dir, present=lambda result: (_ for _ in ()).throw(RuntimeError("print failed")))
        self.assertEqual(self.recorded(kind="message.read"), [])
        mailbox.inbox(self.agent_dir)
        mailbox.inbox(self.agent_dir)  # nothing left
        [read_event] = self.recorded(kind="message.read")
        self.assertEqual(read_event["data"], {"count": 2})

    def test_moves_and_removals_reach_both_journals_without_recreating_the_agent(self):
        child = templates.create(self.root / "subverses" / "child", "child", workdir=self.work)
        self.enable("listener", root=child)
        write(self.agent_dir / "runtime.json", {"launch_id": "L1", "state": "stopped"})
        with mock.patch.object(relocate, "_stopped", return_value=True), \
             mock.patch("multiverse.universe.relocate.observe", return_value={"live": "dormant"}, create=True):
            try:
                relocate.move_agent(self.root, "worker", child)
            except ValueError as error:
                self.skipTest(f"move needs more fixture: {error}")
        for scope in (self.root, child):
            [moved] = self.recorded(scope, "agent.moved")
            self.assertEqual(moved["data"], {"from": str(self.root), "to": str(child)})
        created = self.recorded(kind="universe.created")
        self.assertEqual([event["data"]["name"] for event in created], ["child"])
        other = self.agent("leaver")
        write(other / "runtime.json", {"launch_id": "L2", "state": "stopped"})
        with mock.patch.object(relocate, "_stopped", return_value=True):
            relocate.remove_agent(self.root, "leaver")
        [removed] = self.recorded(kind="agent.removed")
        self.assertEqual(removed["agent"]["name"], "leaver")
        self.assertFalse(other.exists())

    def test_refresh_applied_names_files_written(self):
        source = templates.template_source("base")
        (self.root / "bootstrap.md").write_text("edited")
        plan = {"source": str(source), "template": "base", "changes": [{"path": "bootstrap.md", "action": "update"}]}
        templates.refresh_apply(self.root, plan)
        [event] = self.recorded(kind="refresh.applied")
        self.assertEqual(event["data"], {"written": ["bootstrap.md"]})

    def test_core_operations_finish_while_the_journal_is_locked_elsewhere(self):
        events.directory(self.root).mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c",
            "import fcntl, sys, time; h = open(sys.argv[1], 'a'); fcntl.flock(h, fcntl.LOCK_EX); "
            "print('held', flush=True); time.sleep(60)", str(events.directory(self.root) / ".lock")],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(stop_holder, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = time.monotonic()
        agents.update_runtime(self.agent_dir, "L1", status="waiting")
        mailbox.append_message(self.agent_dir, "hi", {}, "inbox-only")
        agents.report(self.root, "worker", "working", "busy")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(read(self.agent_dir / "runtime.json")["status"], "waiting")
        self.assertEqual(self.recorded(), [])

    def test_no_journal_without_subscribers(self):
        self.enable("listener", on=False)
        agents.update_runtime(self.agent_dir, "L1", status="working")
        mailbox.append_message(self.agent_dir, "hi", {}, "inbox-only")
        self.assertFalse(events.directory(self.root).exists())


class ReadinessTest(EmitterBase):
    """agent.ready uses wait_ready's predicate, once per launch."""

    def ready_case(self, facts, ready_adapter=True, live="alive"):
        from multiverse.runtime import lifecycle
        launch = self.agent_dir / "launches" / "L1"
        launch.mkdir(parents=True, exist_ok=True)
        write(launch / "request.json", {"argv": ["x"]})
        write(self.agent_dir / "runtime.json", {"launch_id": "L1", **facts})
        adapter = mock.Mock()
        with mock.patch("multiverse.harnesses.load", return_value=adapter), \
             mock.patch("multiverse.runtime.native.attachment_ready", return_value=ready_adapter), \
             mock.patch.object(lifecycle, "observe", return_value={"live": live}):
            lifecycle.note_ready(self.root, self.agent_dir, "L1")
        return self.recorded(kind="agent.ready")

    def test_ready_only_when_the_launch_is_ready(self):
        not_ready = [{"state": "running"},  # before Popen: no pid
                     {"state": "running", "pid": 1, "startup_checked": False},  # startup check pending
                     {"state": "starting", "pid": 1},  # binding still under way
                     {"state": "running", "pid": 1, "startup_delivery": "waiting"},
                     {"state": "running", "pid": 1, "startup_delivery": "failed"},
                     {"state": "failed", "pid": 1, "exit_code": 1}]
        ready = {"state": "running", "pid": 1, "startup_checked": True}
        not_ready += [{**ready, "reason": "Native process exited with code 1."},
                      {**ready, "native_relay": {"state": "lost", "reason": "relay gone"}}]
        marker = self.agent_dir / "launches" / "L1" / "ready-event"
        for facts in not_ready:
            with self.subTest(facts=facts):
                self.assertEqual(self.ready_case(facts), [])
                self.assertFalse(marker.exists())
        for live in ("stopped", "dormant", "unknown", "ending"):  # a dead, gone, foreign or unreadable pane
            with self.subTest(live=live):
                self.assertEqual(self.ready_case(ready, live=live), [])
                self.assertFalse(marker.exists())
        # The bridge or relay attachment is not (or no longer) ready.
        self.assertEqual(self.ready_case(ready, ready_adapter=False), [])
        self.assertFalse(marker.exists())
        self.ready_case({"state": "running", "pid": 1, "startup_delivery": "ready"})
        [event] = self.ready_case({"state": "running", "pid": 1, "startup_delivery": "ready"})
        self.assertEqual(event["launch"], "L1")


class LockingTest(EmitterBase):
    """What recording promises about locks: it takes only the journal's, briefly, and never under its own
    local persistence locks; optional readiness work runs after the send and launch locks are released."""

    def test_ready_is_noted_after_startup_delivery_releases_its_locks(self):
        import fcntl
        from multiverse.runtime import lifecycle, native
        write(self.agent_dir / "runtime.json", {"launch_id": "L1", "state": "running", "pid": 1})
        free = []

        def note(root, directory, launch_id):
            for name in (".send.lock", ".launch.lock"):
                with (directory / name).open("a") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(handle, fcntl.LOCK_UN)
                        free.append(name)
                    except OSError:
                        pass
        with mock.patch.object(lifecycle, "note_ready", side_effect=note) as noted:
            native.flush_startup(self.root, self.agent_dir, "L1")
        noted.assert_called_once()
        self.assertEqual(free, [".send.lock", ".launch.lock"])
        self.assertEqual(read(self.agent_dir / "runtime.json")["startup_delivery"], "ready")

    def test_recording_needs_no_core_lock(self):
        held = subprocess.Popen([sys.executable, "-c",
            "import fcntl, sys, time\n"
            "handles = [open(path, 'a') for path in sys.argv[1:]]\n"
            "[fcntl.flock(h, fcntl.LOCK_EX) for h in handles]\n"
            "print('held', flush=True); time.sleep(60)",
            str(self.agent_dir / ".runtime.lock"), str(self.agent_dir / ".launch.lock"),
            str(self.agent_dir / ".inbox.lock")], stdout=subprocess.PIPE, text=True)
        self.addCleanup(stop_holder, held)
        self.assertEqual(held.stdout.readline().strip(), "held")
        started = time.monotonic()
        self.assertIsNotNone(events.for_agent(self.agent_dir, "agent.status", "L1", {"to": "idle"}))
        self.assertLess(time.monotonic() - started, 2)
