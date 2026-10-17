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

    def unguarded(self):
        """The stand-in home without its protection, to set it up as a real one would be."""
        import contextlib

        @contextlib.contextmanager
        def lifted():
            homes = {str(self.protected), str(self.real / ".multiverse")}
            testing._protected.difference_update(homes)
            try:
                yield
            finally:
                testing._protected.update(homes)
        return lifted()

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
            testing.check(self.id())
        testing.check(self.id())  # reported now: nothing more to say


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


class AttributionTest(StandInHome):
    """A refused write fails the test it came from, never a later one; one no test can be charged with fails the
    run at its end (strict, the default)."""

    def test_a_write_from_no_test_does_not_fail_the_next_plugin_test(self):
        testing.running(None)  # a plain TestCase that does not name itself, writing the real home
        with self.assertRaises(PermissionError):
            (self.real / ".multiverse").mkdir()
        testing.running(self.id())
        testing.check(self.id())  # not this test's: nothing to fail here
        [found] = self.new_violations()
        self.assertIsNone(found["test"])

    def test_a_thread_belongs_to_the_test_that_started_it(self):
        import threading
        go, done = threading.Event(), threading.Event()

        def late():
            go.wait(5)
            try:
                (self.real / ".multiverse").mkdir()
            except PermissionError:
                pass
            done.set()
        threading.Thread(target=late, daemon=True).start()
        testing.running("an.earlier.test")  # its test ended; the next one runs now
        go.set()
        done.wait(5)
        testing.running(self.id())
        [found] = self.new_violations()
        self.assertEqual(found["test"], self.id())  # the thread was started here, not in the test running then

    def test_exit_fails_the_run_only_when_strict(self):
        testing._record("open", str(self.protected / "late.json"))
        try:
            self.assertEqual(testing._exit_status(strict=False), 0)
            self.assertEqual(testing._exit_status(strict=True), 1)
        finally:
            self.new_violations()


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


SOURCE = Path(__file__).resolve().parents[1] / "src"


class WorkingDirectoryInsideTheHomeTest(StandInHome):
    """PR #23 review, #11: run from a personal plugin's directory (~/.multiverse/plugins/NAME, as the scaffold
    says), the guard took rmtree's fd-relative removals in /tmp for writes there, and refused them."""

    def setUp(self):
        super().setUp()
        self.personal = self.real / ".multiverse" / "plugins" / "mine"  # a personal plugin's directory
        with self.unguarded():
            (self.personal / "tests").mkdir(parents=True)
        previous = os.getcwd()
        os.chdir(self.personal)
        self.addCleanup(os.chdir, previous)


    def test_removing_a_directory_elsewhere_by_fd_relative_names_is_not_a_write_here(self):
        elsewhere = Path(tempfile.mkdtemp(prefix="mv2-elsewhere-"))
        (elsewhere / "a" / "b").mkdir(parents=True)
        (elsewhere / "a" / "b" / "file.txt").write_text("x")
        __import__("shutil").rmtree(elsewhere)  # unlinks "file.txt", "b", "a" relative to directory fds
        self.assertFalse(elsewhere.exists())
        self.assertEqual(self.new_violations(), [])

    def test_a_write_relative_to_a_directory_fd_inside_the_home_is_still_refused(self):
        inside = os.open(self.personal, os.O_RDONLY)
        self.addCleanup(os.close, inside)
        with self.assertRaises(PermissionError):
            os.mkdir("made-by-fd", dir_fd=inside)
        self.assertEqual([item["path"] for item in self.new_violations()],
                         [os.path.join(os.path.realpath(self.personal), "made-by-fd")])

    def test_a_directory_fd_resolves_to_its_path_and_a_closed_one_to_nothing(self):
        directory = Path(tempfile.mkdtemp(prefix="mv2-fd-"))
        self.addCleanup(directory.rmdir)
        fd = os.open(directory, os.O_RDONLY)
        self.assertEqual(testing._fd_path(fd), os.path.realpath(directory))  # F_GETPATH here, /proc on Linux
        os.close(fd)
        self.assertIsNone(testing._fd_path(fd))
        self.assertIsNone(testing._absolute("name", fd))  # cannot tell where: not counted

    def test_a_write_by_a_relative_name_here_is_still_refused(self):
        with self.assertRaises(PermissionError):
            Path("state.json").write_text("{}")  # relative to the working directory, which is in the home
        self.assertEqual([item["event"] for item in self.new_violations()], ["open"])

    def test_the_plugin_under_test_and_bytecode_in_personal_plugins_are_not_the_homes_state(self):
        testing._exempt.add(str(self.personal))
        self.addCleanup(testing._exempt.discard, str(self.personal))
        (self.personal / "notes.txt").write_text("the plugin's own file")  # its developer's code, not mv2's
        other = self.real / ".multiverse" / "plugins" / "other"
        with self.assertRaises(PermissionError):
            (other / "plugin.json").parent.mkdir()  # another plugin: still refused
        testing._exempt.discard(str(self.personal))
        cache = self.personal / "tests" / "__pycache__"
        cache.mkdir()  # what Python writes when it imports a test module here
        (cache / "test_mine.cpython-313.pyc").write_bytes(b"x")
        self.assertEqual([item["event"] for item in self.new_violations()], ["os.mkdir"])

    def test_a_scaffolded_personal_plugins_tests_pass_run_from_its_directory(self):
        from multiverse.tools import plugins as tool
        import subprocess
        with self.unguarded():
            tool.scaffold("starter", self.personal.parent)  # the real template, into the stand-in's plugins/
        directory = self.personal.parent / "starter"
        # What its README says: run the tests from the plugin's directory, here with the stand-in as HOME.
        run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=directory,
                             env={**os.environ, "HOME": str(self.real), "PYTHONPATH": str(SOURCE)},
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])


class SymlinkAliasTest(StandInHome):
    """PR #23 independent review (b): a write through a link elsewhere that points into the home is a write there."""

    def setUp(self):
        super().setUp()
        with self.unguarded():
            (self.real / ".multiverse").mkdir()

    def test_a_write_through_a_link_into_the_home_is_refused(self):
        alias = self.work / "alias"
        alias.symlink_to(self.real / ".multiverse")  # outside the home: making the link itself is fine
        chained = self.work / "chained"
        chained.symlink_to(alias)
        for attempt in (lambda: (alias / "state.json").write_text("{}"),  # a new file whose parent is a link
                        lambda: (alias / "sub").mkdir(),
                        lambda: (chained / "other.json").write_text("{}")):  # a link to a link
            with self.assertRaises(PermissionError):
                attempt()
        real = os.path.realpath(self.real / ".multiverse")
        self.assertEqual([item["path"] for item in self.new_violations()],
                         [os.path.join(real, name) for name in ("state.json", "sub", "other.json")])
        self.assertEqual(os.listdir(self.real / ".multiverse"), [])

    def test_a_link_to_somewhere_else_is_left_alone(self):
        elsewhere = Path(tempfile.mkdtemp(prefix="mv2-elsewhere-"))
        self.addCleanup(__import__("shutil").rmtree, elsewhere)
        alias = self.work / "to-elsewhere"
        alias.symlink_to(elsewhere)
        (alias / "fine.txt").write_text("x")
        self.assertEqual(self.new_violations(), [])

