"""The tests of this checkout's plugins (plugins/NAME, optional Git submodules), when they are initialized.

An initialized plugin's own tests/ run as part of mv2's suite, with that
directory as their top level, as the examples' do: a plugin and the mv2 commit
that pins it are tested together. An uninitialized submodule is an empty
directory and adds nothing, so a plain clone's suite is unchanged.

A plugin test module that has the name of one of mv2's own (tests/test_x.py in
both) cannot be imported next to it in one run; that plugin's tests are not
loaded, and one failure names both files.

A plugin's test modules are imported with HOME in a directory of their own:
some load their plugin at import (plugin_module), before any test has a home,
and plugin discovery's cache must not be written into the real ~/.multiverse.
"""
import atexit
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import home_safety  # before anything moves HOME: the guard keeps the real ~/.multiverse
home_safety.install()

TESTS = Path(__file__).resolve().parent
PLUGINS = TESTS.parent / "plugins"


def checkout_suites(test_loader, plugins=PLUGINS, own=TESTS):
    """A suite of every initialized checkout plugin's tests, or failures that say why one cannot run."""
    suite = unittest.TestSuite()
    mine = {path.stem for path in own.glob("*.py")}
    for manifest in sorted(Path(plugins).glob("*/plugin.json")):
        directory = manifest.parent / "tests"
        if not directory.is_dir():
            continue
        clashes = sorted(path.name for path in directory.glob("*.py") if path.stem in mine)
        if clashes:
            suite.addTest(_failure(f"{manifest.parent.name}: its tests {', '.join(clashes)} have the names of "
                                   f"mv2's own tests in {own}; rename one of each so both can run"))
            continue
        with _home_of_its_own():
            suite.addTests(test_loader.discover(str(directory), top_level_dir=str(directory)))
    return suite


@contextmanager
def _home_of_its_own():
    """HOME in a new directory while a plugin's test modules are imported; removed when the process ends, since a
    load started at import may still be writing there."""
    home = tempfile.mkdtemp(prefix="mv2-checkout-import-home-")
    atexit.register(shutil.rmtree, home, True)
    with mock.patch.dict(os.environ, {"HOME": home}):
        yield


def _failure(reason):
    class CheckoutPluginTests(unittest.TestCase):
        def test_plugin_tests_can_run(self):
            self.fail(reason)
    return CheckoutPluginTests("test_plugin_tests_can_run")


def load_tests(test_loader, tests, pattern):
    tests.addTests(checkout_suites(test_loader))
    return tests


class CheckoutSuitesTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        temp = tempfile.TemporaryDirectory(prefix="mv2-checkout-plugins-")
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.addCleanup(self.forget_modules)
        self.own = self.work / "tests"
        self.own.mkdir()
        (self.own / "test_core.py").write_text("")

    def forget_modules(self):
        """discover() leaves its top level on sys.path and its modules imported: undo that for this test's files."""
        import sys
        sys.path[:] = [entry for entry in sys.path if not entry.startswith(str(self.work))]
        for name, module in list(sys.modules.items()):
            if str(getattr(module, "__file__", "") or "").startswith(str(self.work)):
                del sys.modules[name]

    def plugin(self, name, tests=None):
        directory = self.work / "plugins" / name
        directory.mkdir(parents=True)
        if tests is None:  # an uninitialized submodule: an empty directory
            return directory
        (directory / "plugin.json").write_text("{}")
        (directory / "tests").mkdir()
        for module, body in tests.items():
            (directory / "tests" / module).write_text(body)
        return directory

    def test_a_plugins_tests_are_imported_with_a_home_of_their_own(self):
        from multiverse.plugins import testing as guard
        real = self.work / "real-home"
        protected = home_safety.install(home=real / ".multiverse")
        self.addCleanup(guard._protected.difference_update, {str(protected), str(real / ".multiverse")})
        before = len(home_safety.violations())
        # A test module that writes the home at import, as plugin discovery's cache does through plugin_module().
        plugins = self.work / "plugins"
        self.plugin("writer", {"test_writes.py": "import os\nos.makedirs(os.path.expanduser('~/.multiverse'), "
                                                 "exist_ok=True)\n"})
        with mock.patch.dict(os.environ, {"HOME": str(real)}):
            checkout_suites(unittest.TestLoader(), plugins=plugins, own=self.own)
        found = home_safety.violations()[before:]
        for item in guard._violations[before:]:
            item["unreported"] = False
        self.assertEqual(found, [], "a plugin test module wrote the home it found at import")
        self.assertFalse((real / ".multiverse").exists())

    def run_suite(self, suite):
        result = unittest.TestResult()
        suite.run(result)
        return result

    def test_initialized_plugins_tests_run_and_empty_submodules_add_nothing(self):
        self.plugin("empty")
        self.plugin("ticks", {"test_ticks_one.py": "import unittest\nclass T(unittest.TestCase):\n"
                                                    "    def test_a(self): pass\n    def test_b(self): pass\n"})
        result = self.run_suite(checkout_suites(unittest.TestLoader(), self.work / "plugins", self.own))
        self.assertEqual((result.testsRun, result.failures, result.errors), (2, [], []))

    def test_a_shared_module_name_is_one_clear_failure(self):
        self.plugin("clash", {"test_core.py": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n"})
        result = self.run_suite(checkout_suites(unittest.TestLoader(), self.work / "plugins", self.own))
        self.assertEqual(result.testsRun, 1)
        self.assertIn("clash: its tests test_core.py have the names of mv2's own tests", result.failures[0][1])

    def test_no_plugins_directory(self):
        suite = checkout_suites(unittest.TestLoader(), self.work / "missing", self.own)
        self.assertEqual(suite.countTestCases(), 0)
