"""mv2 plugins host and its status file (BUG-DASHBOARD-005, and qa-plugins's and qa-dashboard's damaged-file cases,
18d4e7b, 91a9413, 7bf3224), with the CLI run in this process."""
import contextlib
import io
import json
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import cli
from multiverse.plugins import host
from multiverse.plugins.testing import PluginTestCase

DAMAGED = {  # status.json as found on disk -> whether it says why
    "true": True, "[1, 2]": True, "null": True, "42": True, '"running"': True,
    "": True, '{"state": "runn': True, "\x00\x01": True,
    json.dumps({"state": 1, "pid": "12", "updated_at": 5, "workers": [1], "stopped": "no", "socket": 3}): False,
    json.dumps({"state": "running", "pid": True, "workers": {"a": 1, "b": {"pid": "x", "state": 2}},
                "stopped": {"a": 2, "b": "why"}}): False,
}


class StatusReadersTest(PluginTestCase):
    """Every reader of the status file: none raises, whatever the file holds."""

    def mv2(self, *arguments):
        """(exit code, stdout) of `mv2 -u ROOT ARGUMENTS`; an error mv2 does not turn into a message raises here."""
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), *arguments]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                cli.main()
            except SystemExit as exit:
                return exit.code or 0, out.getvalue()
        return 0, out.getvalue()

    def test_every_reader_takes_a_damaged_file(self):
        """Every reader of the host's status.json (host.status, running, left_stopped, mv2 plugins host, its stop and
        mv2 list) takes all ten damaged forms as status unknown, the supervisor's write replaces it, and host stop
        with no host exits 0; a break goes unnoticed because the file is damaged only after a crash, and then mv2
        plugins, doctor and the dashboard show a traceback (BUG-DASHBOARD-005)."""
        code, out = self.mv2("plugins", "host", "stop")  # no host has ever run here
        self.assertEqual(code, 0)
        self.assertIn("is not running", out)
        path = host.host_directory(self.root) / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        for text, says_why in DAMAGED.items():
            with self.subTest(status=text[:30]):
                path.write_text(text)
                status = host.status(self.root)
                self.assertFalse(status["running"])
                self.assertIsInstance(status.get("workers", {}), dict)
                self.assertTrue(all(isinstance(info, dict) for info in status.get("workers", {}).values()))
                self.assertTrue(all(isinstance(why, str) for why in status.get("stopped", {}).values()))
                self.assertFalse(host.running(self.root))
                self.assertTrue(all(isinstance(why, str) for why in host.left_stopped(self.root).values()))
                for action in ((), ("status",), ("stop",)):
                    code, out = self.mv2("plugins", "host", *action)
                    self.assertEqual(code, 0)
                    self.assertIn("is not running", out)
                    if says_why:
                        self.assertIn("status.json cannot be used", out)
                self.assertEqual(self.mv2("list", "--json")[0], 0)
                host._write_status(self.root, "now", {}, {}, {})  # the supervisor's own write over it
                self.assertEqual(json.loads(path.read_text())["state"], "running")
