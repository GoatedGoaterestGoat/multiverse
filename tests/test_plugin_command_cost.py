"""Built-in commands never pay for plugins: no trust check, no plugin lookup, no importlib.metadata scan."""
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from multiverse import cli, plugins
from multiverse.plugins import events, harness, loader, trust
from multiverse.plugins.testing import PluginTestCase


class BuiltInCostTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        # A trusted universe plugin that subscribes to events: the kind whose trust check walks every file.
        self.directory = self.write_plugin("big", {"extends": ["events"], "events": ["agent.*", "message.*"]},
                                           {"data/a.txt": "a"}, where=self.root / "plugins")
        trust.trust(self.directory)
        self.enable("big")
        loader.reset()
        self.walks = mock.patch.object(trust, "signature", side_effect=AssertionError("trust check on a built-in path"))

    def test_a_built_in_command_looks_up_no_plugin_commands(self):
        with mock.patch.object(cli, "plugin_commands", side_effect=AssertionError("plugin lookup")):
            for argv in (["-u", str(self.root), "list"], ["send", "x", "--text", "hi"], ["status", "--phase", "done"],
                         []):
                cli.parser(argv).parse_known_args(argv)
        with mock.patch.object(cli, "plugin_commands", return_value=(self.root, {})) as looked:
            cli.parser(["-h"])  # the top-level help lists plugin commands
            cli.parser(["no-such-command"])  # a name mv2 does not have may be a plugin's
        self.assertEqual(looked.call_count, 2)

    def test_the_first_use_check_and_recording_events_make_no_trust_check(self):
        with self.walks:
            self.assertEqual(plugins.migrate_on_first_use(self.root), [])
            self.assertIsNotNone(events.emit(self.root, "agent.reported", {"id": "a", "name": "w"}))
            self.assertIsNone(events.emit(self.root, "universe.created"))  # nobody subscribes

    def test_usage_is_left_alone_without_an_adjuster(self):
        info = harness.SessionInfo(self.root, "a", "claude", "claude")
        value = {"state": "ok", "value": {"context": {"tokens": 5}}}
        with self.walks, mock.patch.object(loader, "active", side_effect=AssertionError("loaded plugins")):
            self.assertIs(harness.adjust_usage(info, value), value)

    def test_an_old_settings_file_still_takes_the_full_path(self):
        (self.root / "compaction.json").write_text("{}")
        with mock.patch.object(plugins, "_say_not_installed", return_value=["said"]) as said:
            self.assertEqual(plugins.migrate_on_first_use(self.root)[:1], ["said"])
        said.assert_called_once()

    def test_an_untrusted_subscriber_still_has_its_events_recorded(self):
        (self.directory / "data" / "a.txt").write_text("changed")
        loader.reset()
        self.assertEqual(plugins.entry(self.root, "big").state, "changed")
        self.assertEqual(events.subscribers(self.root, "agent.status"), ["big"])


class InstalledScanTest(PluginTestCase):
    def test_installed_plugins_are_scanned_again_only_when_the_environment_changes(self):
        site = self.work / "site"
        site.mkdir()
        sys.path.append(str(site))
        self.addCleanup(sys.path.remove, str(site))
        found = self.write_plugin("pip-made", where=self.work / "packages")
        with mock.patch.object(loader, "_scan_installed", return_value=[found]) as scan:
            for _ in range(2):
                self.assertEqual(loader._installed_remembered(), [found])
            self.assertEqual(scan.call_count, 1)  # the second process reads the last scan
            (site / "new_package").mkdir()  # pip installs something there: its directory changes
            os.utime(site, (1, 2))
            self.assertEqual(loader._installed_remembered(), [found])
            self.assertEqual(scan.call_count, 2)
            (found / "plugin.json").unlink()  # a remembered plugin that is gone means scanning again
            loader._installed_remembered()
            self.assertEqual(scan.call_count, 3)
        remembered = json.loads((self.home / ".multiverse" / loader.INSTALLED).read_text())
        self.assertIn(sys.executable, remembered["environments"])
