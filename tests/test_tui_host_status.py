"""A damaged plugin host status file shows on the Plugins screen as an unknown status, with its reason."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Static
from multiverse.plugins import host
from multiverse.plugins.testing import PluginTestCase


class HostStatusTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("quiet", {"module": "plugin", "extends": ["dashboard.action"]},
                          {"plugin.py": "def register(mv):\n    mv.dashboard.action('Hi', lambda c: 'hi')\n"})
        self.enable("quiet")
        self.status = host.host_directory(self.root) / "status.json"
        self.status.parent.mkdir(parents=True, exist_ok=True)

    async def details(self):
        """The Plugins screen's text, with the real plugin service (the dashboard test double never reads the host)."""
        from multiverse.ui.app import MultiverseApp
        from multiverse.ui.backend import LocalBackend
        from multiverse.ui.plugin_service import PluginService
        from multiverse.ui.plugins_screen import PluginsScreen
        app = MultiverseApp(self.root, LocalBackend(), refresh_interval=0, plugin_service=PluginService())
        async with app.run_test(size=(120, 30)) as pilot:
            for _ in range(300):
                if app.snapshot is not None and app.visible_rows:
                    break
                await pilot.pause(0.02)
            app.action_plugins()
            for _ in range(300):
                screen = app.screen
                if isinstance(screen, PluginsScreen) and screen.items and screen.host is not None:
                    break
                await pilot.pause(0.02)
            self.assertIsInstance(app.screen, PluginsScreen)
            return str(app.screen.query_one("#plugin-details", Static).render()), [item["name"] for item in app.screen.items]

    async def test_a_damaged_status_file_is_shown_as_an_unknown_status_with_its_reason(self):
        for damage in ("[]", "", '{"state":', '"text"'):
            with self.subTest(damage=damage):
                self.status.write_text(damage)
                shown, names = await self.details()
                self.assertIn("Plugin host: off · status unknown", shown)
                self.assertIn("state/plugins/host/status.json cannot be used", shown)
                self.assertIn("the host's status is unknown", shown)
                self.assertIn("quiet", names)  # the plugins are still listed

    async def test_a_good_or_missing_status_file_says_nothing_about_the_host_when_none_is_needed(self):
        for text in (None, json.dumps({"state": "stopped"})):
            with self.subTest(text=text):
                self.status.unlink(missing_ok=True)
                text is not None and self.status.write_text(text)
                shown, _ = await self.details()
                self.assertNotIn("Plugin host", shown)

if __name__ == "__main__":
    unittest.main()
