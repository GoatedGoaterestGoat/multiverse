"""The tests of this checkout's plugins (plugins/NAME, optional Git submodules), when they are initialized.

An initialized plugin's own tests/ run as part of mv2's suite, with that
directory as their top level, as the examples' do: a plugin and the mv2 commit
that pins it are tested together. An uninitialized submodule is an empty
directory and adds nothing, so a plain clone's suite is unchanged.

A plugin test module that has the name of one of mv2's own (tests/test_x.py in
both) cannot be imported next to it in one run; that plugin's tests are not
loaded, and one failure names both files.
"""
from pathlib import Path
import unittest

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
        suite.addTests(test_loader.discover(str(directory), top_level_dir=str(directory)))
    return suite


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
