"""Run with: .venv/bin/python -m unittest discover -s examples/plugins/notifier/tests"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
from multiverse.plugins import api
from multiverse.plugins.testing import PluginTestCase

PLUGIN = Path(__file__).resolve().parents[1]


class NotifierTest(PluginTestCase):
    plugin = PLUGIN

    def setUp(self):
        super().setUp()
        self.shown, self.now = [], [100.0]
        loaded = self.loaded()
        loaded.registrations.handlers.clear()
        loaded.module.register(api.Plugin(loaded), show=lambda title, text: self.shown.append((title, text)) or True,
                               clock=lambda: self.now[0])
        self.handler = loaded.registrations.handlers[0][1]  # one function handles the three types
        self.universe = api.Plugin(loaded).universe(self.root)

    def event(self, kind, data, agent="worker", age=1.0):
        return api.Event(kind, 1, "", self.universe, {"id": agent, "name": agent}, "L1", data, age)

    def test_what_is_worth_a_notification(self):
        self.handler(self.event("agent.status", {"from": "working", "to": "waiting"}))
        self.handler(self.event("agent.status", {"from": "waiting", "to": "working"}))
        self.handler(self.event("agent.exited", {"state": "failed", "exit_code": 2, "reason": None}, agent="b"))
        self.handler(self.event("agent.exited", {"state": "stopped", "exit_code": 0}, agent="c"))
        self.handler(self.event("agent.reported", {"phase": "done", "summary": "Merged"}, agent="d"))
        self.handler(self.event("agent.reported", {"phase": "working", "summary": "x"}, agent="e"))
        self.assertEqual(self.shown, [("worker needs you", "A permission prompt, a question or a plan is waiting."),
                                      ("b stopped with an error", "exit code 2"), ("d is done", "Merged")])

    def test_old_events_repeats_and_settings(self):
        self.handler(self.event("agent.status", {"to": "waiting"}, age=900))  # the host was down
        self.handler(self.event("agent.status", {"to": "waiting"}))
        self.handler(self.event("agent.status", {"to": "waiting"}))  # within 30 s
        self.now[0] += 31
        self.handler(self.event("agent.status", {"to": "waiting"}))
        self.assertEqual(len(self.shown), 2)
        from multiverse import plugins
        plugins.edit_settings(self.root, "notifier", {"notify.waiting": False})
        self.now[0] += 31
        self.handler(self.event("agent.status", {"to": "waiting"}))
        self.assertEqual(len(self.shown), 2)
