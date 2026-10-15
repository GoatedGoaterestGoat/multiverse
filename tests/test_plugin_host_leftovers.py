"""The plugin host's smaller leftovers from the PR #20 re-check: heartbeat, start, gap notices."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, events, host, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import now, write


class HeartbeatTest(PluginTestCase):
    def test_a_stalled_supervisor_loop_still_looks_alive(self):
        stopping, stalled, calls = threading.Event(), threading.Event(), []

        def needed(root):
            calls.append(1)
            if len(calls) == 2:
                stalled.set()
                stopping.wait(4)  # a slow scan, stopping workers, the hourly prune
            return {}
        with mock.patch.object(host, "needed", needed), mock.patch.object(host, "HEARTBEAT_SECONDS", 1.0), \
                mock.patch.object(host, "SUPERVISE_SECONDS", 0.1):
            thread = threading.Thread(target=host.supervise, args=(self.root,),
                                      kwargs={"exit_when_idle": False, "stopping": stopping}, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 10)
            self.addCleanup(stopping.set)
            self.assertTrue(stalled.wait(5))
            time.sleep(2.5)  # well past HEARTBEAT_SECONDS with the loop stuck
            self.assertTrue(host.running(self.root))

    def test_start_never_writes_starting_over_a_supervisor_that_came_up(self):
        from multiverse.terminal import tmux
        path = host.host_directory(self.root) / "status.json"

        def create(*arguments, **options):
            # The supervisor comes up and writes its status before create() even returns.
            write(path, {"state": "running", "pid": os.getpid(), "updated_at": now()})
            return {"socket": "s", "session": "mv2-plugins-x", "pane": "%1"}
        with mock.patch.object(tmux.Tmux, "create", create):
            start = time.monotonic()
            result = host.start(self.root)
        recorded = json.loads(path.read_text())
        self.assertEqual((recorded["state"], recorded["pid"], recorded["session"]),
                         ("running", os.getpid(), "mv2-plugins-x"))
        self.assertTrue(result["running"])
        self.assertLess(time.monotonic() - start, 5)


ONE_TYPE = """
SEEN = []

def register(mv):
    @mv.events.on("agent.status")
    def status(event):
        SEEN.append((event.type, event.seq))
"""


class GapNoticeTest(PluginTestCase):
    def test_a_handler_of_one_type_is_told_what_it_missed(self):
        self.write_plugin("narrow", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                          {"plugin.py": ONE_TYPE})
        self.enable("narrow")
        loaded = loader.load(self.entry("narrow"))
        mv = api.Plugin(loaded)
        host._write_cursor(self.root, "narrow", 0)
        events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": "idle"})
        with mock.patch.object(events, "read", return_value=([], {"missed": 3, "from": 1, "to": 3})):
            host._deliver(loaded, mv, self.root, "narrow")
        self.assertEqual(loaded.module.SEEN, [("events.gap", 3)])
        self.assertEqual(host._read_cursor(self.root, "narrow"), 3)
