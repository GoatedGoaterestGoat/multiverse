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

    def plugin(self, plugins, name, tests=None):
        directory = plugins / name
        directory.mkdir(parents=True)
        if tests is None:  # an uninitialized submodule: an empty directory
            return directory
        (directory / "plugin.json").write_text("{}")
        (directory / "tests").mkdir()
        for module, body in tests.items():
            (directory / "tests" / module).write_text(body)
        return directory

    def run_suite(self, suite):
        result = unittest.TestResult()
        suite.run(result)
        return result

    def test_initialized_plugin_suites_load_once_each(self):
        """Initialized submodule plugins' own tests load into mv2's run once each, empty submodules and a missing
        plugins directory add nothing, and a module-name clash is one clear failure; a break goes unnoticed because a
        plugin suite that stops loading still looks green."""
        with self.subTest("an initialized plugin's tests run once and an empty submodule adds nothing"):
            plugins = self.work / "initialized"
            self.plugin(plugins, "empty")
            self.plugin(plugins, "ticks", {"test_ticks_one.py": "import unittest\nclass T(unittest.TestCase):\n"
                                                                 "    def test_a(self): pass\n"
                                                                 "    def test_b(self): pass\n"})
            result = self.run_suite(checkout_suites(unittest.TestLoader(), plugins, self.own))
            self.assertEqual((result.testsRun, result.failures, result.errors), (2, [], []))
        with self.subTest("a shared module name is one clear failure"):
            plugins = self.work / "clashing"
            self.plugin(plugins, "clash", {"test_core.py": "import unittest\nclass T(unittest.TestCase):\n"
                                                           "    def test_a(self): pass\n"})
            result = self.run_suite(checkout_suites(unittest.TestLoader(), plugins, self.own))
            self.assertEqual(result.testsRun, 1)
            self.assertIn("clash: its tests test_core.py have the names of mv2's own tests", result.failures[0][1])
        with self.subTest("no plugins directory"):
            suite = checkout_suites(unittest.TestLoader(), self.work / "missing", self.own)
            self.assertEqual(suite.countTestCases(), 0)
