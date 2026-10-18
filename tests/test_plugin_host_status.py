"""mv2 plugins host and its status file (BUG-DASHBOARD-005, and qa-plugins's damaged-file cases).

The CLI cases are qa-plugins's (18d4e7b, 91a9413) and qa-dashboard's (7bf3224) black-box tests, run on this
checkout's own mv2 with a private tmux server."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import host, loader
from multiverse.plugins.testing import PluginTestCase

SOURCE = Path(__file__).resolve().parents[1] / "src"
HANDLER = 'def register(mv):\n    mv.events.on("agent.reported", lambda event: None)\n'
DAMAGED = {  # status.json as found on disk -> whether it says why
    "true": True, "[1, 2]": True, "null": True, "42": True, '"running"': True,
    "": True, '{"state": "runn': True, "\x00\x01": True,
    json.dumps({"state": 1, "pid": "12", "updated_at": 5, "workers": [1], "stopped": "no", "socket": 3}): False,
    json.dumps({"state": "running", "pid": True, "workers": {"a": 1, "b": {"pid": "x", "state": 2}},
                "stopped": {"a": 2, "b": "why"}}): False,
}


class HostCommandTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(loader.reset)
        self.socket = str(self.work / "t.sock")
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", self.socket, "kill-server"], capture_output=True))
        self.status_file = host.host_directory(self.root) / "status.json"

    def mv2(self, *arguments):
        environment = {**os.environ, "PYTHONPATH": str(SOURCE), "MV2_TMUX_SOCKET": self.socket}
        return subprocess.run([sys.executable, "-m", "multiverse.cli", "-u", str(self.root), *arguments],
                              env=environment, cwd=self.work, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60)

    def assertClean(self, result, code=0):
        self.assertNotIn("Traceback", result.stderr + result.stdout)
        self.assertEqual(result.returncode, code, result.stderr)

    def test_stop_without_a_running_host_returns_cleanly(self):
        self.assertClean(self.mv2("plugins", "host", "stop"))

    def test_stop_stops_the_host_and_says_so(self):
        self.write_plugin("hostprobe", {"module": "plugin", "extends": ["events"], "events": ["agent.reported"]},
                          {"plugin.py": HANDLER})
        self.assertClean(self.mv2("plugins", "enable", "hostprobe"))
        self.assertClean(self.mv2("plugins", "host", "start"))
        self.assertTrue(host.running(self.root))
        stopped = self.mv2("plugins", "host", "stop")
        self.assertClean(stopped)
        self.assertIn("is not running", stopped.stdout)
        self.assertFalse(host.running(self.root))
        self.assertIn("not running", self.mv2("plugins", "host").stdout)

    def test_a_damaged_status_file_is_status_unknown_never_a_traceback(self):
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        for text, says_why in DAMAGED.items():
            with self.subTest(status=text[:30]):
                self.status_file.write_text(text)
                for action in ((), ("status",), ("stop",)):
                    result = self.mv2("plugins", "host", *action)
                    self.assertClean(result)
                    self.assertIn("is not running", result.stdout)
                    if says_why:
                        self.assertIn("status.json cannot be used", result.stdout)
                self.status_file.write_text(text)
                self.assertClean(self.mv2("list", "--json"))

    def test_a_host_starts_over_a_damaged_status_file(self):
        self.write_plugin("hostprobe", {"module": "plugin", "extends": ["events"], "events": ["agent.reported"]},
                          {"plugin.py": HANDLER})
        self.assertClean(self.mv2("plugins", "enable", "hostprobe"))
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.write_text("true")
        self.assertClean(self.mv2("plugins", "host", "start"))
        self.assertTrue(host.running(self.root))
        self.assertIsInstance(json.loads(self.status_file.read_text()), dict)
        self.assertClean(self.mv2("plugins", "host", "stop"))
        self.assertFalse(host.running(self.root))


class StatusReadersTest(PluginTestCase):
    """Every reader of the status file in-process: none raises, whatever the file holds."""

    def test_every_reader_takes_a_damaged_file(self):
        path = host.host_directory(self.root) / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        for text in DAMAGED:
            with self.subTest(status=text[:30]):
                path.write_text(text)
                status = host.status(self.root)
                self.assertFalse(status["running"])
                self.assertIsInstance(status.get("workers", {}), dict)
                self.assertTrue(all(isinstance(info, dict) for info in status.get("workers", {}).values()))
                self.assertTrue(all(isinstance(why, str) for why in status.get("stopped", {}).values()))
                self.assertFalse(host.running(self.root))
                self.assertTrue(all(isinstance(why, str) for why in host.left_stopped(self.root).values()))
                host._write_status(self.root, "now", {}, {}, {})  # the supervisor's own write over it
                self.assertEqual(json.loads(path.read_text())["state"], "running")
