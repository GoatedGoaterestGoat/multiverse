"""Run with: .venv/bin/python -m unittest discover -s examples/plugins/hello-column/tests"""
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
from multiverse.plugins.testing import PluginTestCase

PLUGIN = Path(__file__).resolve().parents[1]


class HelloColumnTest(PluginTestCase):
    plugin = PLUGIN

    def test_registers_a_column_and_an_inspector_line(self):
        registrations = self.loaded().registrations
        column = registrations.columns["uptime"]
        self.assertEqual((column.title, column.width), ("Uptime", 8))
        row = {"live": "alive", "runtime": {"started_at": "2026-10-11T10:00:00+00:00"}}
        now = datetime(2026, 10, 11, 12, 5, tzinfo=timezone.utc)
        self.assertEqual(self.loaded().module.uptime(row, now), "2h 05m")
        self.assertEqual(column.value({"live": "dormant"}), "—")
        [line] = registrations.inspector
        self.assertEqual(line(row), "Launched 2026-10-11 10:00 UTC")
        self.assertIsNone(line({}))
