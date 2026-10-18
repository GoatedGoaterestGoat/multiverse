"""A dashboard whose mv2 code changed under it says so, instead of showing the import error that follows."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Static
from multiverse.plugins.testing import PluginTestCase
from multiverse.ui.app import MultiverseApp
from multiverse.ui.backend import LocalBackend
from multiverse.ui.stale import CodeWatch, fingerprint

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

    def test_unchanged_code_is_not_stale_and_only_py_files_count(self):
        watch = CodeWatch(self.root)
        self.assertEqual(sorted(watch.baseline), ["pkg/types.py"])
        (self.root / "notes.txt").write_text("edited")
        (self.root / "pkg" / "__pycache__" / "types.cpython-313.pyc").write_bytes(b"y")
        self.assertFalse(watch.check())
        self.assertEqual(watch.changed_files, [])

    def test_a_modified_added_or_removed_module_makes_it_stale_and_it_stays_so(self):
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

    def test_only_version_skew_errors_get_the_sentence_and_only_once_the_code_changed(self):
        watch = CodeWatch(self.root)
        skew = ImportError(RAW)
        self.assertEqual(watch.explain(skew), RAW)  # same code: a real import problem, shown as it is
        self.bump(self.root / "pkg" / "types.py", "A = 2\n")
        for error in (skew, ModuleNotFoundError("No module named 'x'"), AttributeError("no attribute y"),
                      TypeError("run() got an unexpected keyword argument 'z'"), NameError("name 'q' is not defined")):
            with self.subTest(error=type(error).__name__):
                shown = watch.explain(error)
                self.assertTrue(shown.startswith(FRIENDLY), shown)
                self.assertIn(str(error), shown)  # the original stays, for whoever has to diagnose it
        for error in (ValueError("No agent named x here."), OSError("disk"), RuntimeError("boom")):
            self.assertEqual(watch.explain(error), str(error))  # an ordinary failure is not blamed on the update

    def test_the_real_package_can_be_fingerprinted_quickly(self):
        import multiverse
        started = time.monotonic()
        found = fingerprint(Path(multiverse.__file__).parent)
        self.assertGreater(len(found), 100)
        self.assertLess(time.monotonic() - started, 2.0)


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

    def app(self, backend=None):
        return MultiverseApp(self.root, backend or LocalBackend(), refresh_interval=0, code_root=self.code.name)

    def status(self, app):
        return str(app.query_one("#status", Static).render())

    async def test_an_action_that_fails_with_an_import_error_after_the_code_changed_says_to_reopen(self):
        def resume():
            raise ImportError(RAW)

        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.wait(lambda: app.snapshot is not None and not app.scanning, "the first refresh")
            app.submit_operation("Resume agent", resume)
            await self.wait(lambda: not app.operating, "the action to end")
            self.assertIn(f"Resume agent failed: {RAW}", self.status(app))  # the code is unchanged: the raw error
            self.assertNotIn(FRIENDLY, self.status(app))
            self.change_code()
            app.submit_operation("Resume agent", resume)
            await self.wait(lambda: not app.operating, "the action to end")
            shown = self.status(app)
            self.assertIn(f"Resume agent failed: {FRIENDLY}", shown)
            self.assertIn(RAW, shown)

    async def test_an_ordinary_failure_is_not_blamed_on_the_update(self):
        def kill():
            raise ValueError("No agent named worker here.")

        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.wait(lambda: app.snapshot is not None and not app.scanning, "the first refresh")
            self.change_code()
            app.submit_operation("Kill agent", kill)
            await self.wait(lambda: not app.operating, "the action to end")
            self.assertIn("Kill agent failed: No agent named worker here.", self.status(app))
            self.assertNotIn(FRIENDLY, self.status(app))

    async def test_a_refresh_that_fails_after_the_code_changed_says_to_reopen_and_the_header_says_so(self):
        backend = LocalBackend()
        app = self.app(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await self.wait(lambda: app.snapshot is not None and not app.scanning, "the first refresh")
            self.assertNotIn("mv2 changed", str(app.query_one("#totals", Static).render()))
            self.change_code()
            app.refresh_snapshot()
            await self.wait(lambda: "mv2 changed: reopen" in str(app.query_one("#totals", Static).render()),
                            "the header to say mv2 changed")
            with mock.patch.object(LocalBackend, "snapshot", side_effect=ImportError(RAW)):
                app.refresh_snapshot()
                await self.wait(lambda: "Could not refresh" in self.status(app), "the refresh to fail")
            self.assertIn(f"Could not refresh: {FRIENDLY}", self.status(app))
            self.assertIn(RAW, self.status(app))


if __name__ == "__main__":
    unittest.main()
