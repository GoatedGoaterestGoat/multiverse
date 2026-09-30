"""Context, cost and rate limits on the dashboard; harness-neutral, read from each agent's usage reading."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import Static
from multiverse import dashboard, usage
from multiverse.dashboard import COLUMNS, agent_key, view
from multiverse.tui import MultiverseApp
from tui_demo import DemoBackend


def reading(tokens=None, window=None, percent=None, cost=None, limits=(), at="2026-09-30T10:00:00Z"):
    context = {key: value for key, value in (("tokens", tokens), ("window", window), ("percent", percent)) if value is not None}
    value = {"total_tokens": 999999, "context_reported_at": at, "rate_limits": list(limits)}
    if context:
        value["context"] = context
    if cost is not None:
        value["cost_usd"] = cost
    return {"state": "ok", "value": value}


class UsageDisplayTests(unittest.TestCase):
    def test_context_reads_the_latest_request_never_the_cumulative_total(self):
        self.assertEqual(usage.context_display(reading(82000, 200000, 41)), "82k/200k")
        self.assertEqual(usage.context_display(reading(820000, 1000000)), "820k/1.0m")
        self.assertEqual(usage.context_display(reading(8200, 200000)), "8.2k/200k")
        self.assertEqual(usage.context_display(reading(82000)), "82k")
        self.assertEqual(usage.context_display(reading()), "N/A")
        self.assertEqual(usage.context_display({"state": "unavailable"}), "N/A")
        self.assertEqual(usage.context_display({}), "N/A")

    def test_levels_line_cost_and_percent_fallback(self):
        self.assertEqual([usage.context_level(reading(1, 100, p)) for p in (69, 70, 89, 90)], [None, "warn", "warn", "high"])
        self.assertEqual(usage.context_level(reading(75, 100)), "warn")
        self.assertIsNone(usage.context_level(reading(75)))
        self.assertEqual(usage.context_line(reading(82000, 200000, 41, 1.234)), "Context: 82k/200k (41%) · Cost: $1.23")
        self.assertEqual(usage.context_line(reading(82000)), "Context: 82k")
        self.assertEqual(usage.cost_display(reading(1, 2)), "—")

    def test_details_keep_cumulative_totals_out_of_the_lead(self):
        text = usage.details(reading(82000, 200000, 41, 1.2, [{"window": "five_hour", "percent_used": 34.5}]))
        self.assertTrue(text.startswith("Context: 82k/200k (41%)"))
        self.assertIn("Rate limit 5h: 34.5% used", text)
        self.assertIn("total_tokens: 999999", text)


class UsageDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = DemoBackend()
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)

    async def settle(self, pilot):
        await pilot.pause()
        await self.app.workers.wait_for_complete()
        await pilot.pause()

    def test_views_with_the_old_tokens_key_still_show_the_context_column(self):
        self.assertEqual(dashboard.COLUMN_ALIASES["tokens"], "context")
        self.assertNotIn("tokens", COLUMNS)
        self.assertIn("context", dashboard.DEFAULT_COLUMNS)
        self.assertIn("cost", COLUMNS)
        self.assertNotIn("cost", dashboard.DEFAULT_COLUMNS)

    def test_view_file_with_tokens_key_loads(self):
        import tempfile, json
        from multiverse import universe
        with tempfile.TemporaryDirectory() as temp:
            root = universe.create(Path(temp) / ".multiverse", "top", workdir=Path(temp))
            (root / "views").mkdir(exist_ok=True)
            (root / "views" / "default.json").write_text(json.dumps({"columns": ["name", "tokens", "activity", "cost"]}))
            self.assertEqual(view(root)["columns"], ["name", "context", "status", "cost"])

    async def test_context_column_inspector_line_and_header_limits(self):
        soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        limits = [{"window": "five_hour", "percent_used": 34.4, "resets_at": soon},
                  {"window": "seven_day", "percent_used": 12, "resets_at": past}]
        by_name = {a["name"]: a for a in self.backend.agents}
        by_name["reviewer"]["usage"] = reading(82000, 200000, 41, 1.23, limits)
        by_name["kernel-worker"]["usage"] = reading(185000, 200000, 92)
        by_name["secondbrain"]["usage"] = reading(150000, 200000, 75)
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            rows = self.app.visible_rows
            cell = lambda name, field: MultiverseApp.cell(rows[agent_key(by_name[name])], field)
            self.assertEqual(cell("reviewer", "context").plain, "82k/200k")
            self.assertEqual(str(cell("reviewer", "context").style), "#a6adc8")
            self.assertEqual(cell("secondbrain", "context").style, "#f9e2af")
            self.assertIn("#f38ba8", str(cell("kernel-worker", "context").style))
            self.assertEqual(cell("harness-builder", "context").plain, "N/A")
            self.assertEqual(cell("reviewer", "cost").plain, "$1.23")
            header = str(self.app.query_one("#totals", Static).render())
            self.assertTrue(header.startswith("5h 34% · "), header)
            self.assertNotIn("7d", header)
            self.app.select_key(agent_key(by_name["reviewer"]))
            await pilot.press("p")
            await self.settle(pilot)
            self.assertIn("Context: 82k/200k (41%) · Cost: $1.23", str(self.app.query_one("#inspect-state", Static).render()))
            self.assertNotIn("Session total", str(self.app.query_one("#inspect-state", Static).render()))


if __name__ == "__main__":
    unittest.main()
