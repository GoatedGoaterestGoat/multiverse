"""PR #20 review 4: a slow load is never failed for good, a plugin that hung costs later callers little, and what
waits on a load (problems, a launch plan) says why it left a plugin out."""
import json
import os
from pathlib import Path
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import harness, loader, trust
from multiverse.plugins.testing import PluginTestCase

SLOW = """
import os
import time

def register(mv):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not os.path.exists(os.environ["SLOW_RELEASE"]):
        time.sleep(0.02)
    mv.launch.contribute("claude", lambda info: None)
"""


class SlowLoadTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.release = self.work / "release"
        os.environ["SLOW_RELEASE"] = str(self.release)
        self.addCleanup(self.release.touch)
        self.write_plugin("slow", {"module": "plugin", "extends": ["launch"]}, {"plugin.py": SLOW})
        self.enable("slow")
        self.enterContext(mock.patch.object(loader, "LOAD_SECONDS", 0.3))  # past it, a load fails for now

    def longer_limit(self):
        """A limit well past the short waits, as the real 10 s is."""
        return mock.patch.object(loader, "LOAD_SECONDS", 3.0)

    def wait_for(self, check, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.02)
        return False

    def test_a_load_past_the_limit_lands_when_it_finishes(self):
        with self.assertRaisesRegex(loader.PluginError, "took longer than 0.3 s to load"):
            loader.load(self.entry("slow"))
        self.assertEqual(self.entry("slow").state, "failed")
        self.release.touch()  # a slow cold import that does finish, later
        self.assertTrue(self.wait_for(lambda: self.entry("slow").state == "loaded"), self.entry("slow").problem)
        self.assertEqual([loaded.name for loaded in loader.active(self.root)], ["slow"])

    def test_a_plugin_that_hung_before_costs_a_new_process_little(self):
        with self.assertRaises(loader.PluginError):
            loader.load(self.entry("slow"))
        loader.reset()  # a new process: nothing in memory, the record of the hang on disk
        with self.longer_limit():
            start = time.monotonic()
            self.assertEqual(loader.active(self.root), [])
            self.assertLess(time.monotonic() - start, loader.KNOWN_SLOW_WAIT + 0.4)
            # Its launch plan says why it is left out.
            plan = harness.launch_plan(harness.LaunchInfo(self.root, "a", "w", "spawn", "claude", "claude",
                                                          (2, 1, 300), True))
        self.assertEqual(plan.plugins["slow"]["state"], "skipped")
        self.assertIn("still loading", plan.plugins["slow"]["reason"])
        self.assertTrue(any("plugin slow was left out" in problem for problem in plan.problems), plan.problems)

    def test_the_record_of_a_hang_goes_once_it_loads_or_its_files_change(self):
        with self.assertRaises(loader.PluginError):
            loader.load(self.entry("slow"))
        record = trust.user_directory() / loader.LOAD_TIMEOUTS
        self.assertTrue(record.is_file())
        directory = self.entry("slow").directory
        (Path(directory) / "plugin.py").write_text(SLOW.replace("time.sleep(0.02)", "time.sleep(0.01)"))
        loader.reset()
        self.assertFalse(loader._known_slow(self.entry("slow")))  # changed files: waited for in full again
        self.release.touch()
        loader.reset()
        loader.load(self.entry("slow"))
        self.assertNotIn(str(Path(directory).resolve()), json.loads(record.read_text()).get("plugins", {}))

    def test_problems_with_a_short_wait_say_a_plugin_is_still_loading(self):
        start = time.monotonic()
        with self.longer_limit():
            found = plugins.problems(self.root, wait=loader.REFRESH_WAIT)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertIn("slow is still loading", found)


class TrustFirstTest(PluginTestCase):
    def test_doctor_says_to_trust_a_plugin_before_its_old_file_can_be_converted(self):
        directory = self.write_plugin("forked", {"settings": {"version": 2, "legacy": "forked.json"}},
                                      where=self.root / "plugins")
        (self.root / "forked.json").write_text("{}")
        found = [problem for problem in plugins.problems(self.root) if "forked.json" in problem]
        self.assertTrue(found, plugins.problems(self.root))
        self.assertIn("mv2 plugins trust forked", found[0])
        self.assertNotIn("converted the next time mv2 runs here,", found[0])
        trust.trust(directory)
        loader.reset()
        found = [problem for problem in plugins.problems(self.root) if "forked.json" in problem]
        self.assertIn("converted the next time mv2 runs here", found[0])


class LateAttemptAfterResetTest(PluginTestCase):
    """An attempt still running late never touches a later attempt's modules, even after loader.reset() (which
    PluginTestCase calls between tests while such threads may still run)."""

    def test_a_late_attempt_leaves_a_later_attempts_modules_alone(self):
        release = self.work / "release"
        os.environ["SLOW_RELEASE"] = str(release)
        self.addCleanup(release.touch)
        directory = self.write_plugin("slow", {"module": "plugin", "extends": ["launch"]}, {"plugin.py": SLOW})
        self.enable("slow")
        with mock.patch.object(loader, "LOAD_SECONDS", 0.3), self.assertRaises(loader.PluginError):
            loader.load(self.entry("slow"))  # still in its register(), waiting for `release`
        loader.reset()
        (Path(directory) / "plugin.py").write_text("def register(mv):\n    pass\n")
        loaded = loader.load(self.entry("slow"))
        name = loaded.module.__name__
        release.touch()  # the first attempt ends now, late, and is dropped
        time.sleep(0.3)
        self.assertIs(sys.modules.get(name), loaded.module, "a late attempt removed a later one's modules")
