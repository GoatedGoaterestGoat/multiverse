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
        """The inspector's state block on `name`, opening the inspector the first time."""
        d.select(name)
        if d.app.inspector.row is None:
            await d.press("p")
        state = d.app.query_one("#inspect-state", Static)
        await d.until(lambda: name in str(d.app.query_one("#inspect-name", Static).render())
                      and "Mode:" in str(state.render()), f"the inspector on {name}")
        return " ".join(" ".join(visible_lines(state)).split())

    async def test_the_inspector_names_the_launch_mode_fallback_and_left_out_plugins(self):
        """The inspector and the details show the launch mode, why it fell back to plain, and which plugins the launch
        left out and why, in the smallest layout; kept because these were cut off or missing until fixed, and nothing
        else shows an agent why it is launch-only (f7e6b38, e799872)."""
        why = "its settings cannot be used: k must be a whole number of at least 0"
        self.agent("bridged", runtime={
            "native_mode": {"policy": "auto", "mode": "bridge", "fallback_reason": f"plugin compaction was left out: {why}"},
            "plugins": {"plugins": {"compaction": {"version": "1", "state": "skipped", "reason": why}}, "status_items": []}})
        self.agent("other", harness="codex", runtime={
            "native_mode": {"policy": "auto", "mode": "plain", "fallback_reason": None},
            "plugins": {"plugins": {"tidy": {"version": "2", "state": "skipped", "reason": "needs Codex 0.170"},
                                    "fine": {"version": "1", "state": "loaded"}},
                        "status_items": [], "error": "plugins were left out of this launch: RuntimeError: boom"}})
        async with self.dashboard(size=(60, 20)) as d:
            for name, shown, hidden in (
                    ("old", ["Fallback: Claude Code 2.1.100 is older", "which the multiverse mod needs"], []),
                    ("bridged", ["Fallback: plugin compaction was left out: its settings cannot be used"], []),
                    # A plugin that loaded is not a problem.
                    ("other", ["Plugin tidy left out: needs Codex 0.170",
                               "plugins were left out of this launch: RuntimeError: boom"], ["fine"]),
                    ("fine", ["Mode: bridge (auto)"], ["Fallback"])):
                with self.subTest(inspector=name):
                    state = await self.inspect(d, name)
                    for text in shown:
                        self.assertIn(text, state)
                    for text in hidden:
                        self.assertNotIn(text, state)
            for name, shown, hidden in (("old", ["plain (auto)", "FALLBACK", REASON], []),
                                        ("fine", ["bridge (auto)"], ["FALLBACK"])):
                with self.subTest(details=name):
                    d.select(name)
                    await d.press("v")
                    self.assertIsInstance(d.screen, Details)
                    text = " ".join(" ".join(str(widget.render()) for widget in d.screen.query(Static)).split())
                    self.assertIn("LAUNCH MODE", text)
                    for part in shown:
                        self.assertIn(part, text)
                    for part in hidden:
                        self.assertNotIn(part, text)
                    await d.close_screen()

if __name__ == "__main__":
    unittest.main()
