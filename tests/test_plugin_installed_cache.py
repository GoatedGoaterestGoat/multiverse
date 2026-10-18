"""The pip-installed plugins cache keeps a few sys.path signatures per Python, so processes with different paths
(a runner and the CLI) do not make each other scan again and rewrite it; and older mv2 still reads the file."""
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase


class InstalledCacheTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.found = self.write_plugin("pip-made", where=self.work / "packages")
        self.path = self.home / ".multiverse" / loader.INSTALLED

    def remembered(self, signature):
        with mock.patch.object(loader, "_environment_signature", return_value=signature):
            return loader._installed_remembered()

    def test_installed_plugin_scan_cache_is_shared_bounded_and_readable_by_older_mv2(self):
        """The pip-installed plugin scan cache is keyed per sys.path, keeps four entries, rescans when a site
        directory changes or a remembered plugin is gone, and stays readable by an older mv2; a break goes unnoticed
        because results look the same while a new plugin is missed or every call rescans (2e26218, d6ca49e)."""
        with self.subTest("two processes with different paths do not make each other scan again"):
            runner, cli = [["/runner", 1]], [["/site", 2]]
            with mock.patch.object(loader, "_scan_installed", return_value=[self.found]) as scan:
                for signature in (runner, cli, runner, cli, runner, cli):
                    self.assertEqual(self.remembered(signature), [self.found])
            self.assertEqual(scan.call_count, 2)  # one scan each, then both are remembered
        with self.subTest("the least recently used of more than four is dropped"):
            self.path.unlink(missing_ok=True)
            signatures = [[[f"/path{index}", index]] for index in range(5)]
            with mock.patch.object(loader, "_scan_installed", return_value=[self.found]) as scan:
                for signature in signatures[:4]:
                    self.remembered(signature)
                with mock.patch.object(loader.time, "time", return_value=10 ** 10):  # path0 used again, much later
                    self.remembered(signatures[0])
                    self.remembered(signatures[4])  # a fifth: the least recently used (path1) goes
                self.assertEqual(scan.call_count, 5)
                self.remembered(signatures[0])
                self.assertEqual(scan.call_count, 5)  # still remembered
                self.remembered(signatures[1])
                self.assertEqual(scan.call_count, 6)  # dropped, so scanned again
        with self.subTest("an older mv2 reads the newest entry"):
            self.path.unlink(missing_ok=True)
            with mock.patch.object(loader, "_scan_installed", return_value=[self.found]):
                self.remembered([["/runner", 1]])
                self.remembered([["/site", 2]])
            # 2e26218's reader, quoted: environments[executable] is one {signature, directories}.
            remembered = json.loads(self.path.read_text()).get("environments", {}).get(sys.executable) or {}
            self.assertEqual(remembered.get("signature"), [["/site", 2]])
            self.assertEqual(remembered.get("directories"), [str(self.found)])
        with self.subTest("a site directory that changes, or a remembered plugin that is gone, means scanning again"):
            self.path.unlink(missing_ok=True)
            site = self.work / "site"
            site.mkdir()
            sys.path.append(str(site))
            self.addCleanup(sys.path.remove, str(site))
            with mock.patch.object(loader, "_scan_installed", return_value=[self.found]) as scan:
                for _ in range(2):
                    self.assertEqual(loader._installed_remembered(), [self.found])
                self.assertEqual(scan.call_count, 1)  # the second process reads the last scan
                (site / "new_package").mkdir()  # pip installs something there: its directory changes
                os.utime(site, (1, 2))
                self.assertEqual(loader._installed_remembered(), [self.found])
                self.assertEqual(scan.call_count, 2)
                (self.found / "plugin.json").unlink()  # a remembered plugin that is gone means scanning again
                loader._installed_remembered()
                self.assertEqual(scan.call_count, 3)
            self.assertIn(sys.executable, json.loads(self.path.read_text())["environments"])
