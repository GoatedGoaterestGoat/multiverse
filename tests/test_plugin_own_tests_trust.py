"""Running a trusted universe plugin's own tests leaves it trusted."""
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from multiverse.plugins import trust
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import plugins as tool

SOURCE = Path(__file__).resolve().parents[1] / "src"


class OwnTestsTest(PluginTestCase):
    def test_a_test_run_in_the_plugin_folder_keeps_its_trust(self):
        parent = self.root / "plugins"
        parent.mkdir(exist_ok=True)
        tool.scaffold("mine", parent)
        directory = parent / "mine"
        trust.trust(directory)
        self.assertEqual(trust.state(directory), "trusted")
        # What a developer runs in the plugin's folder (its README says so), with mv2 importable.
        run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=directory,
                             env={**os.environ, "PYTHONPATH": str(SOURCE)}, capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        written = sorted(str(path.relative_to(directory)) for path in directory.rglob("*")
                         if path.is_file() and path.stat().st_mtime > (directory / "plugin.json").stat().st_mtime)
        self.assertEqual(trust.state(directory), "trusted", f"the test run wrote {written}")
