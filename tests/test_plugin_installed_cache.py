"""The pip-installed plugins cache keeps a few sys.path signatures per Python, so processes with different paths
(a runner and the CLI) do not make each other scan again and rewrite it; and older mv2 still reads the file."""
import json
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

    def test_two_processes_with_different_paths_do_not_make_each_other_scan_again(self):
        runner, cli = [["/runner", 1]], [["/site", 2]]
        with mock.patch.object(loader, "_scan_installed", return_value=[self.found]) as scan:
            for signature in (runner, cli, runner, cli, runner, cli):
                self.assertEqual(self.remembered(signature), [self.found])
        self.assertEqual(scan.call_count, 2)  # one scan each, then both are remembered

    def test_the_least_recently_used_of_more_than_four_is_dropped(self):
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

    def test_an_older_mv2_reads_the_newest_entry(self):
        with mock.patch.object(loader, "_scan_installed", return_value=[self.found]):
            self.remembered([["/runner", 1]])
            self.remembered([["/site", 2]])
        # 2e26218's reader, quoted: environments[executable] is one {signature, directories}.
        remembered = json.loads(self.path.read_text()).get("environments", {}).get(sys.executable) or {}
        self.assertEqual(remembered.get("signature"), [["/site", 2]])
        self.assertEqual(remembered.get("directories"), [str(self.found)])
