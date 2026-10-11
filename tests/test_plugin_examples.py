"""The example plugins' own tests, and `mv2 plugins check` on each, so the examples keep working."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "plugins"


class ExamplesCheckTest(unittest.TestCase):
    def test_every_example_is_a_usable_plugin(self):
        from multiverse.plugins import loader
        from multiverse.tools import plugins
        examples = sorted(path for path in EXAMPLES.iterdir() if (path / "plugin.json").is_file())
        self.assertTrue(examples)
        for example in examples:
            with self.subTest(example.name):
                self.addCleanup(loader.reset)
                result = plugins.check(example)
                self.assertTrue(result["ok"], result["problems"])


def load_tests(test_loader, tests, pattern):
    for directory in sorted(EXAMPLES.glob("*/tests")):
        tests.addTests(test_loader.discover(str(directory), top_level_dir=str(directory)))
    return tests
