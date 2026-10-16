"""Two processes writing one plugin log at once: rotation happens once, and no line is lost."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"

# A writer that waits for `go`, then logs one line. Path.replace is slowed, so two writers that both decide to
# rotate overlap: without a lock between processes the second rotation replaces the first's NAME.1.
WRITER = textwrap.dedent("""
    import pathlib, sys, time, types
    from multiverse.plugins import loader
    kind, log, go, marker = sys.argv[1:5]
    real = pathlib.Path.replace
    def slow(self, target):
        time.sleep(0.4)
        return real(self, target)
    pathlib.Path.replace = slow
    loader.ERRORS_LOG_BYTES = loader.LOG_BYTES = 100
    while not pathlib.Path(go).exists():
        time.sleep(0.005)
    if kind == "errors":
        root = pathlib.Path(log).parents[3]
        loader.record(types.SimpleNamespace(errors=[], name="p"), marker, ValueError(marker), root)
    else:
        loader.append_log(log, marker)
""")


class LogRaceTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.work, True)

    def race(self, kind, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("OLD " * 100 + "\n")  # past the limit: the first writer rotates it
        go = self.work / "go"
        environment = {**os.environ, "PYTHONPATH": str(SOURCE)}
        writers = [subprocess.Popen([sys.executable, "-c", WRITER, kind, str(log), str(go), f"writer-{index}"],
                                    env=environment) for index in range(2)]
        time.sleep(0.5)  # both import and wait at the gate
        go.touch()
        for writer in writers:
            self.assertEqual(writer.wait(30), 0)
        rotated = log.with_name(log.name + ".1")
        self.assertIn("OLD", rotated.read_text(), "the rotated file was replaced by a second rotation")
        text = log.read_text()
        for index in range(2):
            self.assertIn(f"writer-{index}", text, f"writer-{index}'s line was lost")

    def test_errors_log(self):
        self.race("errors", self.work / "state" / "plugins" / "p" / "errors.log")

    def test_plugin_log(self):
        self.race("plugin", self.work / "state" / "plugins" / "p" / "plugin.log")


if __name__ == "__main__":
    unittest.main()
