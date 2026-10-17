"""Run with: .venv/bin/python -m unittest discover -s examples/plugins/cost-item/tests"""
from pathlib import Path
import sys
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
from multiverse.plugins import api, harness
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write

PLUGIN = Path(__file__).resolve().parents[1]


class CostItemTest(PluginTestCase):
    plugin = PLUGIN

    def test_cost_text(self):
        module = self.loaded().module
        self.assertEqual(module.cost_text({"state": "ok", "value": {"cost_usd": 1.234}}), "$1.23")
        self.assertEqual(module.cost_text({"state": "ok", "value": {}}), "")
        self.assertEqual(module.cost_text({"state": "unavailable"}), "")

    def test_a_status_change_writes_the_item(self):
        identity = str(uuid4())
        directory = self.root / "state" / "agents" / identity
        write(directory / "properties.json", {"id": identity, "name": "worker", "harness": "claude", "task": "",
                                             "universe_id": read(self.root / "universe.json")["id"],
                                             "workspace": str(self.work)})
        write(directory / "runtime.json", {"launch_id": "L1", "state": "stopped"})
        loaded = self.loaded()
        [(_, handler)] = loaded.registrations.handlers
        universe = api.Plugin(loaded).universe(self.root)
        event = api.Event("agent.status", 1, "", universe, {"id": identity, "name": "worker"}, "L1", {"to": "idle"})
        with mock.patch("multiverse.tools.get_usage.run", return_value={"state": "ok", "value": {"cost_usd": 0.42}}):
            handler(event)
        self.assertEqual(harness.item_path(directory, "cost").read_text(), "$0.42\n")
