"""Plugin logs rotate at their limit, and two processes writing one log at once rotate it once and lose no line."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, events, host, loader
from multiverse.plugins.testing import PluginTestCase

SOURCE = Path(__file__).resolve().parents[1] / "src"

# A writer that says it is ready, waits for `go`, then logs one line. Path.replace is slowed, so two writers that
# both decide to rotate overlap: without a lock between processes the second rotation replaces the first's NAME.1.
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
    pathlib.Path(go).with_name(f"{kind}-{marker}.ready").touch()
    while not pathlib.Path(go).exists():
        time.sleep(0.005)
    if kind == "errors":
        root = pathlib.Path(log).parents[3]
        loader.record(types.SimpleNamespace(errors=[], name="p"), marker, ValueError(marker), root)
    else:
        loader.append_log(log, marker)
""")


def hold_lock(path, seconds):
    """A separate process holding mv2's file lock at `path` for `seconds`; returns it once the lock is held."""
    process = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, sys.argv[3]); from multiverse.store import lock\n"
        "with lock(sys.argv[1]):\n    print('held', flush=True); time.sleep(float(sys.argv[2]))"),
        str(path), str(seconds), str(SOURCE)], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "held"
    return process


class LogRaceTest(PluginTestCase):
    def test_two_processes_rotating_one_log_lose_no_line(self):
        """Plugin, errors, events and host logs rotate at their limit, errors.log keeping one traceback per call site
        a minute, a write waits for another process's rotation, and two processes rotating one log at once rotate it
        once and lose no line; a break goes unnoticed because the only symptom is diagnostic lines lost (or a full
        disk) while the dashboard, host and CLI write at once."""
        self.write_plugin("chatty", {"module": "plugin"}, {"plugin.py": "def register(mv): pass\n"})
        self.enable("chatty")
        loaded = loader.load(self.entry("chatty"))
        with self.subTest("every log rotates"):
            mv = api.Plugin(loaded)
            with mock.patch.object(loader, "LOG_BYTES", 500):
                for index in range(40):
                    mv.log("x" * 40, self.root)
                    events._log(self.root, "y" * 40)
                    host._log(self.root, "z" * 40)
            for log in (self.root / "state" / "plugins" / "chatty" / "plugin.log",
                        self.root / "state" / "plugins" / "events.log", host.host_directory(self.root) / "host.log"):
                self.assertTrue(log.with_name(log.name + ".1").exists(), log)
                self.assertLess(log.stat().st_size, 1000)
        with self.subTest("errors.log is throttled and rotates"):
            log = self.root / "state" / "plugins" / "chatty" / "errors.log"
            for index in range(50):  # a column failing differently on every row, every refresh
                loader.guard(loaded, "column x", lambda index=index: 1 / 0 if index % 2 else int(f"x{index}"),
                             root=self.root)
            self.assertEqual(log.read_text().count("Traceback"), 1)  # one traceback per call site and minute
            with mock.patch.object(loader, "ERRORS_LOG_BYTES", 2000):
                for index in range(200):
                    loader.record(loaded, f"call {index}", ValueError("x" * 50), self.root)
            self.assertTrue(log.with_name("errors.log.1").exists())
            self.assertLess(log.stat().st_size, 2000 + 4000)
        with self.subTest("a write waits for another process rotating the log"):
            path = self.work / "state" / "plugin.log"
            path.parent.mkdir(parents=True)
            holder = hold_lock(loader.log_lock_path(path), 1.0)
            self.addCleanup(holder.wait, 10)
            start = time.monotonic()
            loader.append_log(path, "line")
            self.assertGreater(time.monotonic() - start, 0.6)
            self.assertEqual(path.read_text(), "line\n")
        with self.subTest("two processes rotating one log at once"):
            logs = {"errors": self.work / "state" / "plugins" / "p" / "errors.log",
                    "plugin": self.work / "state" / "plugins" / "p" / "plugin.log"}
            go = self.work / "go"
            writers = []
            for kind, log in logs.items():
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("OLD " * 100 + "\n")  # past the limit: the first writer rotates it
                writers += [subprocess.Popen([sys.executable, "-c", WRITER, kind, str(log), str(go),
                                              f"writer-{index}"], env={**os.environ, "PYTHONPATH": str(SOURCE)})
                            for index in range(2)]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and len(list(self.work.glob("*.ready"))) < len(writers):
                time.sleep(0.01)  # every writer imported and waits at the gate
            go.touch()
            for writer in writers:
                self.assertEqual(writer.wait(30), 0)
            for kind, log in logs.items():
                rotated = log.with_name(log.name + ".1")
                self.assertIn("OLD", rotated.read_text(), f"{kind}: the rotated file was replaced by a second rotation")
                text = log.read_text()
                for index in range(2):
                    self.assertIn(f"writer-{index}", text, f"{kind}: writer-{index}'s line was lost")
