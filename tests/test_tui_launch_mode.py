"""An agent that did not launch the way it was asked to shows why, in the inspector and in the details."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rich.console import Console
from rich.text import Text
from textual.widgets import Static
from multiverse.plugins.testing import PluginTestCase
from multiverse.ui.screens import Details

REASON = "Claude Code 2.1.100 is older than 2.1.280, which the multiverse mod needs"


def visible_lines(widget):
    """The text lines the widget really shows: wrapped at its width, cut at its height."""
    lines = Text(str(widget.render())).wrap(Console(width=widget.size.width), widget.size.width)
    return [line.plain for line in lines][:widget.size.height]


class LaunchModeTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.agent("old", runtime={"native_mode": {"policy": "auto", "mode": "plain", "fallback_reason": REASON}})
        self.agent("fine", runtime={"native_mode": {"policy": "auto", "mode": "bridge", "fallback_reason": None}})

    async def inspect(self, d, name):
        d.select(name)
        await d.press("p")
        await d.until(lambda: "Mode:" in str(d.app.query_one("#inspect-state", Static).render()), "the inspector")
        return d.app.query_one("#inspect-state", Static)

    async def test_the_fallback_reason_is_visible_beside_the_plain_mode_at_every_size(self):
        for size in ((140, 36), (100, 30), (80, 24), (60, 20)):
            with self.subTest(size=size):
                async with self.dashboard(size=size) as d:
                    shown = " ".join(" ".join(visible_lines(await self.inspect(d, "old"))).split())
                    self.assertIn("Fallback: Claude Code 2.1.100 is older", shown)
                    self.assertIn("which the multiverse mod needs", shown)

    async def test_a_plugin_the_launch_left_out_is_named_with_its_reason_at_every_size(self):
        why = "its settings cannot be used: k must be a whole number of at least 0"
        self.agent("bridged", runtime={
            "native_mode": {"policy": "auto", "mode": "bridge", "fallback_reason": f"plugin compaction was left out: {why}"},
            "plugins": {"plugins": {"compaction": {"version": "1", "state": "skipped", "reason": why}}, "status_items": []}})
        self.agent("other", harness="codex", runtime={
            "native_mode": {"policy": "auto", "mode": "plain", "fallback_reason": None},
            "plugins": {"plugins": {"tidy": {"version": "2", "state": "skipped", "reason": "needs Codex 0.170"},
                                    "fine": {"version": "1", "state": "loaded"}},
                        "status_items": [], "error": "plugins were left out of this launch: RuntimeError: boom"}})
        for size in ((140, 36), (80, 24), (60, 20)):
            with self.subTest(size=size):
                async with self.dashboard(size=size) as d:
                    shown = " ".join(" ".join(visible_lines(await self.inspect(d, "bridged"))).split())
                    self.assertIn("Fallback: plugin compaction was left out: its settings cannot be used", shown)
                    self.assertEqual(shown.count("k must be a whole number"), 1, "the same reason was repeated")
                    d.select("other")
                    await d.until(lambda: "other" in str(d.app.query_one("#inspect-name", Static).render()), "the inspector on the other agent")
                    other = " ".join(" ".join(visible_lines(d.app.query_one("#inspect-state", Static))).split())
                    self.assertIn("Plugin tidy left out: needs Codex 0.170", other)
                    self.assertNotIn("fine", other)  # a plugin that loaded is not a problem
                    self.assertIn("plugins were left out of this launch: RuntimeError: boom", other)

    async def test_an_agent_with_nothing_to_explain_shows_no_fallback_line(self):
        async with self.dashboard() as d:
            shown = "\n".join(visible_lines(await self.inspect(d, "fine")))
            self.assertIn("Mode: bridge (auto)", shown)
            self.assertNotIn("Fallback", shown)

    async def test_the_details_name_the_launch_mode_and_the_fallback(self):
        async with self.dashboard() as d:
            d.select("old")
            await d.press("v")
            self.assertIsInstance(d.screen, Details)
            text = " ".join(str(widget.render()) for widget in d.screen.query(Static))
            self.assertIn("LAUNCH MODE", text)
            self.assertIn("plain (auto)", text)
            self.assertIn("FALLBACK", text)
            self.assertIn(REASON, " ".join(text.split()))
            await d.close_screen()
            d.select("fine")
            await d.press("v")
            text = " ".join(str(widget.render()) for widget in d.screen.query(Static))
            self.assertIn("bridge (auto)", text)
            self.assertNotIn("FALLBACK", text)


if __name__ == "__main__":
    unittest.main()
