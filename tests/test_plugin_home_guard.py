"""No test writes to the real ~/.multiverse: the guard that refuses it, and the late writes it caught."""
import contextlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
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
        """The refused writes since the last call: this test's own, made on purpose, so not reported again."""
        for item in testing._violations[self.before:]:
            item["unreported"] = False
        found = testing.violations()[self.before:]
        self.before += len(found)
        return found

    def elsewhere(self):
        """A new directory outside the home, removed when the test ends."""
        directory = Path(tempfile.mkdtemp(prefix="mv2-elsewhere-"))
        self.addCleanup(shutil.rmtree, directory, True)
        return directory


class GuardTest(StandInHome):
    def test_a_write_under_the_protected_home_is_refused_and_recorded(self):
        """The home guard refuses and records writes into the protected ~/.multiverse, and into the .multiverse of
        the mv2 checkout the tests run from, by direct path, HOME, a relative name, a dir_fd, a symlink or a chained
        symlink, and allows fd-relative removal elsewhere and the plugin under test; fragile because audit events
        carry no dir_fd and do not resolve links, so a miss silently writes the live user's home or the live
        checkout's universe (PR #23 review #11 and (b), reviewer item 4, Shao's re-review)."""
        home = self.real / ".multiverse"
        with self.subTest("by its path and through HOME"):
            with self.assertRaises(PermissionError):
                home.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(self.real)}):
                with self.assertRaises(PermissionError):
                    (home / "x.json").write_text("{}")
            self.assertFalse(home.exists())
            found = self.new_violations()
            self.assertEqual({item["event"] for item in found}, {"os.mkdir", "open"})
            self.assertTrue(all(item["test"] == self.id() for item in found))
            (self.work / "elsewhere.txt").write_text("fine")  # anywhere else is left alone
        with self.subTest("the .multiverse of the checkout the tests run from"):
            testing.install()  # as every test process does, with no stand-in
            checkout = Path(testing.__file__).resolve().parents[3] / ".multiverse"
            # Under a directory that is never there: refused by the guard, or else FileNotFoundError, never a write
            # into this checkout's universe, which is the one agents run in when the tests run from the live checkout.
            probe = checkout / f"mv2-guard-probe-{os.getpid()}-{time.monotonic_ns()}" / "probe"
            with self.assertRaises(PermissionError):
                os.mkdir(probe)
            self.assertEqual([item["path"] for item in self.new_violations()], [str(probe)])
        with self.subTest("check raises once for what is not reported"):
            testing._record("open", str(self.protected / "late.json"))
            with self.assertRaisesRegex(AssertionError, "late.json"):
                testing.check(self.id())
            testing.check(self.id())  # reported now: nothing more to say
            self.new_violations()
        with self.subTest("a directory fd resolves to its path and a closed one to nothing"):
            directory = self.elsewhere()
            fd = os.open(directory, os.O_RDONLY)
            self.assertEqual(testing._fd_path(fd), os.path.realpath(directory))  # F_GETPATH here, /proc on Linux
            os.close(fd)
            self.assertIsNone(testing._fd_path(fd))
            self.assertIsNone(testing._absolute("name", fd))  # cannot tell where: not counted
        with self.unguarded():
            home.mkdir()
        with self.subTest("through a link elsewhere that points into the home"):
            alias = self.work / "alias"
            alias.symlink_to(home)  # outside the home: making the link itself is fine
            chained = self.work / "chained"
            chained.symlink_to(alias)
            for attempt in (lambda: (alias / "state.json").write_text("{}"),  # a new file whose parent is a link
                            lambda: (alias / "sub").mkdir(),
                            lambda: (chained / "other.json").write_text("{}")):  # a link to a link
                with self.assertRaises(PermissionError):
                    attempt()
            real = os.path.realpath(home)
            self.assertEqual([item["path"] for item in self.new_violations()],
                             [os.path.join(real, name) for name in ("state.json", "sub", "other.json")])
            self.assertEqual(os.listdir(home), [])
        with self.subTest("a link to somewhere else is left alone"):
            alias = self.work / "to-elsewhere"
            alias.symlink_to(self.elsewhere())
            (alias / "fine.txt").write_text("x")
            self.assertEqual(self.new_violations(), [])
        # Reviewer item 4: the open audit event carries no dir_fd, so os.open(name, O_CREAT, dir_fd=fd) was judged by
        # the working directory, missing a write inside the home and refusing one outside it.
        with self.subTest("relative to a directory fd in the home, from outside it"):
            inside = os.open(home, os.O_RDONLY)
            self.addCleanup(os.close, inside)
            self.assertFalse(os.getcwd().startswith(str(self.real)))  # the working directory is elsewhere
            with self.assertRaises(PermissionError):
                os.open("made.json", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=inside)
            self.assertEqual([item["path"] for item in self.new_violations()],
                             [os.path.join(os.path.realpath(home), "made.json")])
            self.assertEqual(os.listdir(home), [])
        with self.subTest("relative to a directory fd elsewhere, from inside the home"):
            elsewhere = self.elsewhere()
            outside = os.open(elsewhere, os.O_RDONLY)
            self.addCleanup(os.close, outside)
            with contextlib.chdir(home):
                os.close(os.open("made.json", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=outside))
                self.assertTrue((elsewhere / "made.json").is_file())
                self.assertEqual(self.new_violations(), [])
                with self.assertRaises(PermissionError):  # and a plain relative name here is still this directory
                    os.open("here.json", os.O_WRONLY | os.O_CREAT, 0o600)
            self.assertEqual(len(self.new_violations()), 1)
        # PR #23 review, #11: run from a personal plugin's directory (~/.multiverse/plugins/NAME, as the scaffold
        # says), the guard took rmtree's fd-relative removals in /tmp for writes there, and refused them.
        personal = home / "plugins" / "mine"
        with self.unguarded():
            (personal / "tests").mkdir(parents=True)
        with contextlib.chdir(personal):
            with self.subTest("a relative name in a personal plugin's directory"):
                with self.assertRaises(PermissionError):
                    Path("state.json").write_text("{}")  # relative to the working directory, which is in the home
                self.assertEqual([item["event"] for item in self.new_violations()], ["open"])
            with self.subTest("relative to a directory fd inside the home"):
                inside = os.open(personal, os.O_RDONLY)
                self.addCleanup(os.close, inside)
                with self.assertRaises(PermissionError):
                    os.mkdir("made-by-fd", dir_fd=inside)
                self.assertEqual([item["path"] for item in self.new_violations()],
                                 [os.path.join(os.path.realpath(personal), "made-by-fd")])
            with self.subTest("removing a directory elsewhere by fd-relative names"):
                elsewhere = self.elsewhere()
                (elsewhere / "a" / "b").mkdir(parents=True)
                (elsewhere / "a" / "b" / "file.txt").write_text("x")
                shutil.rmtree(elsewhere)  # unlinks "file.txt", "b", "a" relative to directory fds
                self.assertFalse(elsewhere.exists())
                self.assertEqual(self.new_violations(), [])
            with self.subTest("the plugin under test and bytecode in personal plugins"):
                testing._exempt.add(str(personal))
                self.addCleanup(testing._exempt.discard, str(personal))
                (personal / "notes.txt").write_text("the plugin's own file")  # its developer's code, not mv2's
                with self.assertRaises(PermissionError):
                    (home / "plugins" / "other").mkdir()  # another plugin: still refused
                testing._exempt.discard(str(personal))
                cache = personal / "tests" / "__pycache__"
                cache.mkdir()  # what Python writes when it imports a test module here
                (cache / "test_mine.cpython-313.pyc").write_bytes(b"x")
                self.assertEqual([item["event"] for item in self.new_violations()], ["os.mkdir"])

    def test_late_plugin_work_writes_only_under_the_home_it_started_under(self):
        """Background plugin work writes only under the home it started under: a load that times out after its test,
        and plugin_module() or a checkout plugin's tests imported before any test has a home, never write the home
        they find then; fragile because these writes happen after HOME changed, so they land in another test's home
        or the real one (57f62aa)."""
        with self.subTest("a load that times out after its test"):
            release = self.work / "release"
            os.environ["HANG_RELEASE"] = str(release)
            self.addCleanup(release.touch)
            self.write_plugin("slow", {"module": "plugin"}, {"plugin.py": HANGS})
            self.enable("slow")
            with mock.patch.object(loader, "LOAD_SECONDS", 0.4), mock.patch.dict(os.environ):
                self.assertEqual(loader.active(self.root, wait=0), [])  # its load goes on in the background
                # What a test's cleanup does before the load times out: HOME is the real one again.
                os.environ["HOME"] = str(self.real)
                time.sleep(0.8)
            self.assertEqual(self.new_violations(), [], "the late timeout wrote into the real home")
            self.assertFalse((self.real / ".multiverse").exists())
            self.assertTrue((self.home / ".multiverse" / loader.LOAD_TIMEOUTS).is_file())  # where it started
            release.touch()
        # plugin_module() at a test module's import, before any test has a home, loads exactly the plugin it is
        # given: no plugin discovery, so no installed-plugins scan and no cache write to the home it finds
        # (compaction's measurement: test_tui_compaction's import wrote ~/.multiverse/installed-plugins.json).
        with self.subTest("plugin_module() at a test module's import"):
            directory = self.write_plugin("imported", {"module": "plugin"},
                                          {"plugin.py": "def register(mv):\n    pass\n", "engine.py": "VALUE = 7\n"},
                                          where=self.work / "elsewhere")
            with mock.patch.dict(os.environ, {"HOME": str(self.real)}), mock.patch.object(loader, "_installed", None), \
                    mock.patch.object(loader, "_environment_signature", return_value=[["/a/new/path", 1]]):
                module = testing.plugin_module(directory, "engine")
            self.assertEqual(module.VALUE, 7)
            self.assertEqual(self.new_violations(), [])
            self.assertFalse((self.real / ".multiverse").exists())
        with self.subTest("a checkout plugin's tests are imported with a home of their own"):
            from test_plugin_checkouts import checkout_suites
            plugins = self.work / "checkout" / "plugins"
            tests = plugins / "writer" / "tests"
            tests.mkdir(parents=True)
            (plugins / "writer" / "plugin.json").write_text("{}")
            # A test module that writes the home at import, as plugin discovery's cache does through plugin_module().
            (tests / "test_writes.py").write_text("import os\nos.makedirs(os.path.expanduser('~/.multiverse'), "
                                                  "exist_ok=True)\n")
            own = self.work / "checkout" / "tests"
            own.mkdir()
            self.addCleanup(self.forget, tests)
            with mock.patch.dict(os.environ, {"HOME": str(self.real)}):
                checkout_suites(unittest.TestLoader(), plugins=plugins, own=own)
            self.assertEqual(self.new_violations(), [], "a plugin test module wrote the home it found at import")
            self.assertFalse((self.real / ".multiverse").exists())

    def forget(self, directory):
        """discover() leaves its top level on sys.path and its modules imported: undo that for `directory`."""
        sys.path[:] = [entry for entry in sys.path if not entry.startswith(str(directory))]
        for name, module in list(sys.modules.items()):
            if str(getattr(module, "__file__", "") or "").startswith(str(directory)):
                del sys.modules[name]
