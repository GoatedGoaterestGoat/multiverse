"""A dashboard whose mv2 code changed under it says so, instead of showing the import error that follows."""
import asyncio
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Static
from multiverse.plugins.dashboard_testing import quiet
from multiverse.plugins.testing import PluginTestCase
from multiverse.ui.app import MultiverseApp
from multiverse.ui.backend import LocalBackend
from multiverse.ui.stale import CodeWatch

RAW = "cannot import name 'LocalProtocolError' from 'multiverse.harnesses.types'"
FRIENDLY = "mv2 changed since this dashboard started; quit and reopen it"


class CodeWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "types.py").write_text("A = 1\n")
        (self.root / "pkg" / "__pycache__").mkdir()
        (self.root / "pkg" / "__pycache__" / "types.cpython-313.pyc").write_bytes(b"x")
        (self.root / "notes.txt").write_text("not code")

    def bump(self, path, text):
        path.write_text(text)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))  # a clearly later time, whatever the clock

    def test_a_changed_package_is_stale_and_stays_so(self):
        """A modified, added or removed .py module under the package makes the dashboard stale for good, and unchanged
        code or other files do not; fragile because the fingerprint is taken at start, and a miss shows a raw import
        error instead of the reopen advice (ffbe412)."""
        watch = CodeWatch(self.root)
        self.assertEqual(sorted(watch.baseline), ["pkg/types.py"])
        (self.root / "notes.txt").write_text("edited")
        (self.root / "pkg" / "__pycache__" / "types.cpython-313.pyc").write_bytes(b"y")
        self.assertFalse(watch.check())
        self.assertEqual(watch.changed_files, [])
        for change in ("modify", "add", "remove"):
            with self.subTest(change=change):
                watch = CodeWatch(self.root)
                target = self.root / "pkg" / ("new.py" if change == "add" else "types.py")
                if change == "remove":
                    saved = target.read_text()
                    target.unlink()
                else:
                    self.bump(target, "A = 2\n")
                self.assertTrue(watch.check())
                self.assertEqual(watch.changed_files, ["pkg/new.py" if change == "add" else "pkg/types.py"])
                # Putting the file back does not make the running process fresh again.
                if change == "remove":
                    target.write_text(saved)
                self.assertTrue(watch.check())
                if change == "add":
                    target.unlink()
                if change == "modify":
                    self.bump(target, "A = 1\n")


class StaleDashboardTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.agent("worker")
        self.code = tempfile.TemporaryDirectory()
        self.addCleanup(self.code.cleanup)
        self.module = Path(self.code.name) / "types.py"
        self.module.write_text("A = 1\n")

    def change_code(self):
        self.module.write_text("A = 2\n")
        stat = self.module.stat()
        os.utime(self.module, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    async def wait(self, check, what):
        for _ in range(300):
            if check():
                return
            await asyncio.sleep(0.02)
        self.fail(f"timed out waiting for {what}")

    def app(self):
        return MultiverseApp(self.root, LocalBackend(), refresh_interval=0, code_root=self.code.name)

    @contextlib.asynccontextmanager
    async def running(self, app):
        """The app, with nothing left running when the test ends: an action ends by starting a scan, and that scan's
        thread, still running once the cleanups put HOME back, writes under the real ~/.multiverse, which the
        runner's home guard fails the unit for."""
        async with app.run_test(size=(120, 30)):
            await self.wait(lambda: app.snapshot is not None and not app.scanning, "the first refresh")
            yield
            await quiet(app)

    def status(self, app):
        return str(app.query_one("#status", Static).render())

    async def test_only_version_skew_errors_say_to_reopen(self):
        """Only an import-style error after the code changed gets the reopen sentence, and an ordinary failure, in the
        dashboard too, is never blamed on the update; a break goes unnoticed because a real bug would hide behind quit
        and reopen (ffbe412)."""
        def kill():
            raise ValueError("No agent named worker here.")

        watch = CodeWatch(self.code.name)
        skew = ImportError(RAW)
        self.assertEqual(watch.explain(skew), RAW)  # same code: a real import problem, shown as it is
        app = self.app()
        async with self.running(app):
            def resume():
                raise skew

            with self.subTest("before the change the dashboard shows the plain import error"):
                app.submit_operation("Resume agent", resume)
                await self.wait(lambda: not app.operating, "the action to end")
                self.assertIn(RAW, self.status(app))
                self.assertNotIn("reopen", self.status(app).lower())
            self.change_code()
            with self.subTest("after the change the dashboard's own action shows the reopen advice"):
                app.submit_operation("Resume agent", resume)
                await self.wait(lambda: not app.operating, "the action to end")
                self.assertIn(RAW, self.status(app))  # the original stays, for whoever has to diagnose it
                self.assertIn("reopen", self.status(app).lower())
            for error in (skew, ModuleNotFoundError("No module named 'x'"), AttributeError("no attribute y"),
                          TypeError("run() got an unexpected keyword argument 'z'"),
                          NameError("name 'q' is not defined")):
                with self.subTest(error=type(error).__name__):
                    shown = watch.explain(error)
                    self.assertTrue(shown.startswith(FRIENDLY), shown)
                    self.assertIn(str(error), shown)  # the original stays, for whoever has to diagnose it
            for error in (ValueError("No agent named x here."), OSError("disk"), RuntimeError("boom")):
                with self.subTest(error=type(error).__name__):
                    self.assertEqual(watch.explain(error), str(error))
            with self.subTest("an ordinary failure on the dashboard"):
                app.submit_operation("Kill agent", kill)
                await self.wait(lambda: not app.operating, "the action to end")
                self.assertIn("Kill agent failed: No agent named worker here.", self.status(app))
                self.assertNotIn(FRIENDLY, self.status(app))


if __name__ == "__main__":
    unittest.main()
