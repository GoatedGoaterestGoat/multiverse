"""Core's event emitters: each one records its change once, honestly, without holding the change up."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import events
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write
from multiverse.universe import agents, mailbox


def hold(*paths):
    """A separate process holding an exclusive flock on each of `paths` until stop_holder; returns it once held."""
    process = subprocess.Popen([sys.executable, "-c",
        "import fcntl, sys, time\n"
        "handles = [open(path, 'a') for path in sys.argv[1:]]\n"
        "[fcntl.flock(h, fcntl.LOCK_EX) for h in handles]\n"
        "print('held', flush=True); time.sleep(60)", *map(str, paths)], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "held"
    return process


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
    def ready_case(self, directory, facts, ready_adapter=True, live="alive"):
        """The agent.ready events recorded after note_ready, the launch's facts being `facts`."""
        from multiverse.runtime import lifecycle
        launch = directory / "launches" / "L1"
        launch.mkdir(parents=True, exist_ok=True)
        write(launch / "request.json", {"argv": ["x"]})
        write(directory / "runtime.json", {"launch_id": "L1", **facts})
        with mock.patch("multiverse.harnesses.load", return_value=mock.Mock()), \
                mock.patch("multiverse.runtime.native.attachment_ready", return_value=ready_adapter), \
                mock.patch.object(lifecycle, "observe", return_value={"live": live}):
            lifecycle.note_ready(self.root, directory, "L1")
        return self.recorded(kind="agent.ready")

    def test_messages_saved_delivered_failed_and_read_honestly(self):
        """Message events follow delivery states once each (delivered only when accepted or confirmed, failed once),
        agent.ready is recorded once per launch and never for a launch that is not ready, and no event carries
        message text, task text or a terminal tail, a core event's reason cut to its first line; a break goes
        unnoticed because core never reads the journal, while subscribers such as notifier see false receipts,
        a missing or doubled ready, or private text (PR #20 review item 7)."""
        with self.subTest("messages"):
            message = mailbox.append_message(self.agent_dir, "the private text", {"name": "user", "universe": "/x"},
                                             "steer")
            [saved] = self.recorded(kind="message.saved")
            self.assertEqual(saved["data"], {"id": message["id"], "delivery": "steer", "length": 16,
                                             "from": {"name": "user"}})
            self.assertNotIn("private", json.dumps(self.recorded()))
            mailbox.update_message(self.agent_dir, message["id"], submission_state="submitting")
            mailbox.update_message(self.agent_dir, message["id"], submission_state="unknown")
            self.assertEqual(self.recorded(kind="message.delivered") + self.recorded(kind="message.failed"), [])
            mailbox.update_message(self.agent_dir, message["id"], submission_state="accepted")
            mailbox.update_message(self.agent_dir, message["id"], submission_state="accepted")
            mailbox.update_message(self.agent_dir, message["id"], transcript_confirmed_at="now")
            mailbox.update_message(self.agent_dir, message["id"], transcript_confirmed_at="later")
            self.assertEqual([event["data"]["stage"] for event in self.recorded(kind="message.delivered")],
                             ["accepted", "confirmed"])
            other = mailbox.append_message(self.agent_dir, "x", {}, "follow-up")
            mailbox.update_message(self.agent_dir, other["id"], submission_state="failed",
                                   attempts=[{"reason": "no bridge"}])
            mailbox.update_message(self.agent_dir, other["id"], submission_state="failed")
            [failed] = self.recorded(kind="message.failed")
            self.assertEqual(failed["data"], {"id": other["id"], "reason": "no bridge"})
        with self.subTest("a report"):
            agents.report(self.root, "worker", "blocked", "Waiting for review")
            [event] = self.recorded(kind="agent.reported")
            self.assertEqual(event["data"], {"phase": "blocked", "summary": "Waiting for review"})
            self.assertNotIn("secret task", json.dumps(event))
        with self.subTest("reasons"):
            tail = "Launch failed: exit 1\nLast terminal output:\n> SECRET PROMPT TEXT\n" + "x" * 8000
            events.emit(self.root, "agent.exited", {"id": "a", "name": "w"}, data={"state": "failed", "reason": tail})
            events.emit(self.root, "message.failed", {"id": "a", "name": "w"}, data={"id": "m", "reason": "y" * 500})
            events.emit(self.root, "listener.note", None, data={"reason": "keep\nall of it"})  # a plugin's own event
            exited, failed, own = self.recorded()[-3:]
            self.assertEqual(exited["data"]["reason"], "Launch failed: exit 1")
            self.assertEqual(len(failed["data"]["reason"]), 200)
            self.assertTrue(failed["data"]["reason"].endswith("…"))
            self.assertEqual(own["data"]["reason"], "keep\nall of it")
            self.assertNotIn("SECRET", json.dumps([exited, failed]))
        with self.subTest("ready"):
            starter = self.agent("starter")
            marker = starter / "launches" / "L1" / "ready-event"
            ready = {"state": "running", "pid": 1, "startup_checked": True}
            not_ready = [{"state": "running"},  # before Popen: no pid
                         {"state": "running", "pid": 1, "startup_checked": False},  # startup check pending
                         {"state": "starting", "pid": 1},  # binding still under way
                         {"state": "running", "pid": 1, "startup_delivery": "waiting"},
                         {"state": "running", "pid": 1, "startup_delivery": "failed"},
                         {"state": "failed", "pid": 1, "exit_code": 1},
                         {**ready, "reason": "Native process exited with code 1."},
                         {**ready, "native_relay": {"state": "lost", "reason": "relay gone"}}]
            cases = ([(facts, True, "alive") for facts in not_ready]
                     + [(ready, True, live) for live in ("stopped", "dormant", "unknown", "ending")]  # pane not live
                     + [(ready, False, "alive")])  # the bridge or relay attachment is not (or no longer) ready
            for facts, ready_adapter, live in cases:
                with self.subTest(facts=facts, ready_adapter=ready_adapter, live=live):
                    self.assertEqual(self.ready_case(starter, facts, ready_adapter, live), [])
                    self.assertFalse(marker.exists())
            self.ready_case(starter, {"state": "running", "pid": 1, "startup_delivery": "ready"})
            [event] = self.ready_case(starter, {"state": "running", "pid": 1, "startup_delivery": "ready"})  # once
            self.assertEqual((event["launch"], event["agent"]["name"]), ("L1", "starter"))

    def test_core_operations_finish_while_the_journal_is_locked_elsewhere(self):
        """Recording an event never blocks, fails or holds runtime updates, message append, report or startup
        delivery, with the journal locked by another process or unusable, or core's own locks held; fragile because
        of lock order between the journal and the send, launch and runtime locks, and a break stalls delivery only
        under contention."""
        events_log = self.root / "state" / "plugins" / "events.log"
        with self.subTest("the journal locked by another process"):
            events.directory(self.root).mkdir(parents=True, exist_ok=True)
            holder = hold(events.directory(self.root) / ".lock")
            self.addCleanup(stop_holder, holder)
            started = time.monotonic()
            agents.update_runtime(self.agent_dir, "L1", status="waiting")
            mailbox.append_message(self.agent_dir, "hi", {}, "inbox-only")
            agents.report(self.root, "worker", "working", "busy")
            self.assertIsNone(events.emit(self.root, "agent.status", {"id": "a", "name": "w"}))
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(read(self.agent_dir / "runtime.json")["status"], "waiting")
            self.assertEqual(self.recorded(), [])
            self.assertIn("dropped: the journal was busy", events_log.read_text())
            stop_holder(holder)
            self.assertIsNotNone(events.emit(self.root, "agent.status", {"id": "a", "name": "w"}))
        with self.subTest("core's own locks held by another process"):
            holder = hold(*(self.agent_dir / name for name in (".runtime.lock", ".launch.lock", ".inbox.lock")))
            self.addCleanup(stop_holder, holder)
            started = time.monotonic()
            self.assertIsNotNone(events.for_agent(self.agent_dir, "agent.status", "L1", {"to": "idle"}))
            self.assertLess(time.monotonic() - started, 2)
            stop_holder(holder)
        with self.subTest("readiness is noted after startup delivery releases its locks"):
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
        with self.subTest("a report with a damaged runtime, subscribed to or not"):
            (self.agent_dir / "runtime.json").write_text("{bad-json")
            for on in (False, True):
                self.enable("listener", on=on)
                agents.report(self.root, "worker", "working", f"on={on}")
                self.assertEqual(read(self.agent_dir / "properties.json")["summary"], f"on={on}")
            [event] = [event for event in self.recorded(kind="agent.reported")
                       if event["data"]["summary"].startswith("on=")]
            self.assertEqual(event["data"]["summary"], "on=True")
            self.assertIsNone(event["launch"])
        with self.subTest("an unusable journal"):
            events.directory(self.root).rename(self.work / "journal")
            events.directory(self.root).write_text("not a directory")
            logged = len(events_log.read_text())
            self.assertIsNone(events.emit(self.root, "agent.status", {"id": "a"}))
            self.assertIn("agent.status", events_log.read_text()[logged:])
