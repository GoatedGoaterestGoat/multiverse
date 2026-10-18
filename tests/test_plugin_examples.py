"""`mv2 plugins check` on each example plugin and on a new plugin's scaffold, and the examples' own tests, so what
authors copy keeps working."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
SOURCE = Path(__file__).resolve().parents[1] / "src"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "plugins"


class ExamplesCheckTest(unittest.TestCase):
    def test_every_example_is_a_usable_plugin(self):
        """The shipped examples and the mv2 plugins new scaffold, which authors copy, pass check against the current
        API, and the scaffold's own tests pass run from its directory in a personal plugins folder; a break goes
        unnoticed because nothing else imports them."""
        from multiverse.plugins import loader
        from multiverse.tools import plugins
        examples = sorted(path for path in EXAMPLES.iterdir() if (path / "plugin.json").is_file())
        self.assertTrue(examples)
        for example in examples:
            with self.subTest(example.name):
                self.addCleanup(loader.reset)
                result = plugins.check(example)
                self.assertTrue(result["ok"], result["problems"])
        with self.subTest("a new plugin's scaffold"):
            home = Path(tempfile.mkdtemp(prefix="mv2-scaffold-home-"))
            self.addCleanup(shutil.rmtree, home, True)
            directory = Path(plugins.scaffold("starter", home / ".multiverse" / "plugins")["directory"])
            self.addCleanup(loader.reset)
            result = plugins.check(directory)
            self.assertTrue(result["ok"], result["problems"])
            # What its README says: run the tests from the plugin's directory, here with a home of its own.
            run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=directory,
                                 env={**os.environ, "HOME": str(home), "PYTHONPATH": str(SOURCE)},
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr[-3000:])


def load_tests(test_loader, tests, pattern):
    # The example plugins' own tests (notifier's handlers, cost-item's status item, hello-column), fast and
    # in-process: the only check that the shipped samples, which authors copy, still work against the API. Nothing
    # else runs them, so without these a break goes unnoticed.
    top = test_loader._top_level_dir
    try:
        for directory in sorted(EXAMPLES.glob("*/tests")):
            tests.addTests(test_loader.discover(str(directory), top_level_dir=str(directory)))
    finally:
        test_loader._top_level_dir = top  # Python 3.11's discover() leaves it at the last example's
    return tests
