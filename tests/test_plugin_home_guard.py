"""No test writes to the real ~/.multiverse: the guard that refuses it, and the slow-load leak it caught."""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader, testing
from multiverse.plugins.testing import PluginTestCase

HANGS = """
import os
import time

def register(mv):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not os.path.exists(os.environ["HANG_RELEASE"]):
        time.sleep(0.02)
"""


class StandInHome(PluginTestCase):
    """A directory protected as if it were the real home, so these tests never go near the real one."""

    def setUp(self):
        super().setUp()
        outside = tempfile.TemporaryDirectory(prefix="mv2-real-home-")
        self.addCleanup(outside.cleanup)
        self.real = Path(outside.name).resolve()
        self.protected = testing.install(home=self.real / ".multiverse")
        self.addCleanup(testing._protected.difference_update, {str(self.protected), str(self.real / ".multiverse")})
        self.before = len(testing.violations())

    def new_violations(self):
        for item in testing._violations[self.before:]:  # this test's own, made on purpose: not reported again
            item["unreported"] = False
        return testing.violations()[self.before:]


class GuardTest(StandInHome):
    def test_a_write_under_the_protected_home_is_refused_and_recorded(self):
        with self.assertRaises(PermissionError):
            (self.real / ".multiverse").mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(self.real)}):
            with self.assertRaises(PermissionError):
                (self.real / ".multiverse" / "x.json").write_text("{}")
        self.assertFalse((self.real / ".multiverse").exists())
        found = self.new_violations()
        self.assertEqual({item["event"] for item in found}, {"os.mkdir", "open"})
        self.assertTrue(all(item["test"] == self.id() for item in found))
        (self.work / "elsewhere.txt").write_text("fine")  # anywhere else is left alone

    def test_check_raises_once_for_what_is_not_reported(self):
        testing._record("open", str(self.protected / "late.json"))
        with self.assertRaisesRegex(AssertionError, "late.json"):
            testing.check()
        testing.check()  # reported now: nothing more to say


class SlowLoadLeakTest(StandInHome):
    def test_a_load_that_times_out_after_its_test_writes_to_the_home_it_started_under(self):
        release = self.work / "release"
        os.environ["HANG_RELEASE"] = str(release)
        self.addCleanup(release.touch)
        self.write_plugin("slow", {"module": "plugin"}, {"plugin.py": HANGS})
        self.enable("slow")
        with mock.patch.object(loader, "LOAD_SECONDS", 0.4):
            self.assertEqual(loader.active(self.root, wait=0), [])  # its load goes on in the background
            # What a test's cleanup does before the load times out: HOME is the real one again.
            os.environ["HOME"] = str(self.real)
            time.sleep(0.8)
        self.assertEqual(self.new_violations(), [], "the late timeout wrote into the real home")
        self.assertFalse((self.real / ".multiverse").exists())
        self.assertTrue((self.home / ".multiverse" / loader.LOAD_TIMEOUTS).is_file())  # where it started


class PluginTestCaseGuardsTest(PluginTestCase):
    def test_plugin_test_cases_run_guarded(self):
        self.assertTrue(testing._protected)
        self.assertEqual(testing._current, self.id())


class PluginModuleTest(StandInHome):
    """plugin_module() at a test module's import, before any test has a home, loads exactly the plugin it is given:
    no plugin discovery, so no installed-plugins scan and no cache write to the home it finds (compaction's
    measurement: test_tui_compaction's import wrote ~/.multiverse/installed-plugins.json)."""

    def test_plugin_module_writes_nothing_to_the_home(self):
        directory = self.write_plugin("imported", {"module": "plugin"},
                                      {"plugin.py": "def register(mv):\n    pass\n", "engine.py": "VALUE = 7\n"},
                                      where=self.work / "elsewhere")
        with mock.patch.dict(os.environ, {"HOME": str(self.real)}), mock.patch.object(loader, "_installed", None), \
                mock.patch.object(loader, "_environment_signature", return_value=[["/a/new/path", 1]]):
            module = testing.plugin_module(directory, "engine")
        self.assertEqual(module.VALUE, 7)
        self.assertEqual(self.new_violations(), [])
        self.assertFalse((self.real / ".multiverse").exists())
