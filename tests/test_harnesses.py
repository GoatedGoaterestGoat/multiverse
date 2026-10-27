"""Harness contracts and real tmux launches, without accounts or model calls."""
import errno
import os
import shlex
from pathlib import Path
import sys
import time
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from native_cli import forbidden_python, mv2_launcher
from test_multiverse import TerminalBase, until


class HarnessLaunchTest(TerminalBase):
    def setUp(self):
        super().setUp()
        # A stand-in callback must not rediscover Python from PATH or bin/mv2.
        bad_python = forbidden_python(self.work)
        self.enterContext(patch.dict(os.environ, {'PATH': str(bad_python) + os.pathsep + os.environ['PATH']}))
        self.enterContext(patch.object(lifecycle, '_cli_path', return_value=mv2_launcher(self.work)))
        self.enterContext(patch('multiverse.runtime.native.start_shared_service',
            side_effect=AssertionError('Fake CLI contracts must never prepare a native daemon')))

    def fake_cli(self, harness, report=True):
        directory = self.root / "harnesses" / harness
        executable = directory / "fake-cli"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys, time, uuid\nfrom pathlib import Path\n"
            "args = sys.argv[1:]\n"
            f"if {harness == 'codex' and report!r} and 'resume' not in args:\n"
            "    native = str(uuid.uuid4())\n"
            "    for _ in range(2):\n"
            "        subprocess.run([os.environ['MV2_CLI'], 'session-bind', '--session-id', native], check=True)\n"
            # Existence is the reader's ready signal; publish only after the JSON file is complete.
            "seen = Path(os.environ['MV2_AGENT_DIR'], 'seen.json')\n"
            "pending = seen.with_suffix('.tmp')\n"
            "pending.write_text(json.dumps({'argv': args, "
            "'parent_markers': [key for key in ('CODEX_THREAD_ID', 'CLAUDECODE') if key in os.environ]}))\n"
            "pending.replace(seen)\n"
            "print('HARNESS READY', flush=True)\nwhile True: time.sleep(1)\n")
        executable.chmod(0o755)
        spec = read(directory / "harness.json")
        spec["settings"]["executable"] = str(executable)
        # The fake CLI implements plain argv and binding, not a native service.
        spec["settings"]["inbox_bridge"] = False
        write(directory / "harness.json", spec)

    def stop_agent(self, agent):
        lifecycle.terminal_action(self.root, agent["id"], "stop")
        time.sleep(0.1)

    def deny_callback_namespace(self, denied_errno):
        launcher = Path(mv2_launcher(self.work))
        source = str(Path(__file__).resolve().parents[1] / 'src')
        code = (
            f"import sys; sys.path.insert(0, {source!r})\n"
            "from multiverse.universe import sessions\n"
            "def denied(root):\n"
            f"    raise OSError({denied_errno}, 'sandbox denies universe write', str(sessions.top(root) / 'state/.sessions.lock'))\n"
            "sessions.namespace = denied\n"
            "from multiverse.cli import main\nmain()\n")
        launcher.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-c', code]) + ' "$@"\n')

    def test_codex_session_bind_from_inside_a_real_pane_and_exact_resume(self):
        """A plain Codex stand-in calls the real mv2 session-bind from inside its tmux pane, with a forbidden python
        first on PATH, and resume then uses exactly that id; a break goes unnoticed because every other binding test
        mocks the runner, and a platform-specific denial errno can silently leave a working sandboxed agent unresumable."""
        for denied in (None, errno.EPERM, errno.EROFS):
            with self.subTest(namespace_denied=denied):
                if denied:
                    self.deny_callback_namespace(denied)
                self.fake_cli("codex")
                prompt = '--pretend-flag "quoted"\n$(touch MUST_NOT_EXIST) `echo no` {literal}'
                with patch.dict(os.environ, {"CODEX_THREAD_ID": "parent-session"}):
                    first = lifecycle.start(self.root, f"codex-denied-{denied}" if denied else "codex", prompt, "codex", "chosen-model", socket_path=self.socket)
                directory = agents.directory(self.root, first["id"])
                seen = directory / "seen.json"
                until(lambda: seen.exists() or agents.runtime(directory).get('state') == 'failed')
                self.assertTrue(seen.exists(), agents.runtime(directory))
                received = read(seen)
                argv = received["argv"]
                self.assertNotIn("CODEX_THREAD_ID", received["parent_markers"])
                self.assertEqual(argv[-1], prompt)
                self.assertLess(argv.index("--model"), argv.index("--"))
                self.assertEqual(argv[argv.index("--model") + 1], "chosen-model")
                self.assertFalse((self.work / "MUST_NOT_EXIST").exists())
                settings = tomllib.loads(argv[argv.index("-c") + 1])
                self.assertIn("session-bind", settings["developer_instructions"])
                self.assertIn("You work in a Multiverse universe", settings["developer_instructions"])
                native = read(directory / "properties.json")["native_session_id"]
                self.stop_agent(first)
                seen.unlink()
                lifecycle.start(self.root, resume=first["id"], socket_path=self.socket)
                until(lambda: seen.exists())
                resumed = read(seen)["argv"]
                self.assertIn(native, resumed)
                self.assertNotIn(prompt, resumed)
                self.assertNotIn("--last", resumed)
                self.stop_agent(first)
        with self.subTest("a session that never bound"):
            self.fake_cli("codex", report=False)
            first = lifecycle.start(self.root, "unregistered", "test", socket_path=self.socket)
            directory = agents.directory(self.root, first["id"])
            until(lambda: (directory / "seen.json").exists())
            self.stop_agent(first)
            launches = list((directory / "launches").iterdir())
            with self.assertRaisesRegex(ValueError, "No native session ID recorded"):
                lifecycle.start(self.root, resume=first["id"], socket_path=self.socket)
            self.assertEqual(list((directory / "launches").iterdir()), launches)


if __name__ == "__main__":
    unittest.main()
