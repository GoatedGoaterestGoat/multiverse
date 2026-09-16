"""Harness contracts and real tmux launches, without accounts or model calls."""
import os
from pathlib import Path
import shutil
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import agents
from multiverse.cli import parser
from multiverse.dashboard import harnesses
from multiverse.store import read, write
from test_multiverse import BaseTest, TerminalBase, until


class HarnessFilesTest(BaseTest):
    def test_discovery_and_plain_defaults(self):
        self.assertEqual(harnesses(self.root), ["codex", "claude", "pi"])
        self.assertEqual(parser().parse_args(["spawn", "--name", "test", "--task", "test"]).harness, "codex")
        self.assertNotIn("--extension", read(self.root / "harnesses/pi/harness.json")["spawn"])
        bad = self.root / "harnesses" / "broken"
        bad.mkdir()
        (bad / "harness.json").write_text("{unfinished")
        self.assertEqual(harnesses(self.root), ["codex", "claude", "pi"])
        shutil.rmtree(self.root / "harnesses/codex")
        self.assertEqual(harnesses(self.root), ["claude", "pi"])

    def test_session_binding_rejects_invalid_stale_and_changed_ids(self):
        agent_dir = self.root / "state/agents" / str(uuid4())
        write(agent_dir / "properties.json", {"id": agent_dir.name, "name": "test"})
        write(agent_dir / "runtime.json", {"launch_id": "new"})
        session = str(uuid4())
        with self.assertRaises(ValueError):
            agents.bind_session(self.root, "test", "not-a-session", "new")
        with self.assertRaisesRegex(ValueError, "Outdated"):
            agents.bind_session(self.root, "test", session, "old")
        self.assertNotIn("native_session_id", read(agent_dir / "properties.json"))
        agents.bind_session(self.root, "test", session, "new")
        agents.bind_session(self.root, "test", session, "new")
        with self.assertRaisesRegex(ValueError, "different native session"):
            agents.bind_session(self.root, "test", str(uuid4()), "new")


class HarnessLaunchTest(TerminalBase):
    def fake_cli(self, harness, report=True):
        directory = self.root / "harnesses" / harness
        executable = directory / "fake-cli"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys, time, uuid\nfrom pathlib import Path\n"
            "args = sys.argv[1:]\n"
            f"if {harness == 'codex' and report!r} and 'resume' not in args:\n"
            "    subprocess.run([os.environ['MV2_CLI'], 'session-bind', '--session-id', str(uuid.uuid4())], check=True)\n"
            "Path(os.environ['MV2_AGENT_DIR'], 'seen.json').write_text(json.dumps({'argv': args, "
            "'parent_markers': [key for key in ('CODEX_THREAD_ID', 'CLAUDECODE') if key in os.environ]}))\n"
            "print('HARNESS READY', flush=True)\nwhile True: time.sleep(1)\n")
        executable.chmod(0o755)
        spec = read(directory / "harness.json")
        for mode in ("spawn", "resume"):
            spec[mode][0] = str(executable)
        write(directory / "harness.json", spec)

    def stop_agent(self, agent):
        agents.terminal_action(self.root, agent["id"], "stop")
        time.sleep(0.1)

    def test_codex_and_claude_launch_literals_models_and_exact_resume(self):
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                self.fake_cli(harness)
                marker = "CODEX_THREAD_ID" if harness == "codex" else "CLAUDECODE"
                prompt = '--pretend-flag "quoted"\n$(touch MUST_NOT_EXIST) `echo no` {literal}'
                with patch.dict(os.environ, {marker: "parent-session"}):
                    first = agents.start(self.root, harness, prompt, harness, "chosen-model", socket_path=self.socket)
                directory = agents.directory(self.root, first["id"])
                seen = directory / "seen.json"
                until(lambda: seen.exists())
                received = read(seen)
                argv = received["argv"]
                self.assertNotIn(marker, received["parent_markers"])
                self.assertEqual(argv[-1], prompt)
                self.assertLess(argv.index("--model"), argv.index("--"))
                self.assertEqual(argv[argv.index("--model") + 1], "chosen-model")
                self.assertFalse((self.work / "MUST_NOT_EXIST").exists())
                if harness == "codex":
                    import tomllib
                    settings = tomllib.loads(argv[argv.index("-c") + 1])
                    self.assertIn("session-bind", settings["developer_instructions"])
                    self.assertIn("You work in a Multiverse universe", settings["developer_instructions"])
                else:
                    self.assertIn("You work in a Multiverse universe", argv[argv.index("--append-system-prompt") + 1])
                native = read(directory / "properties.json")["native_session_id"]
                if harness == "claude":
                    self.assertEqual(native, first["id"])
                    self.assertEqual(argv[argv.index("--session-id") + 1], native)
                self.stop_agent(first)
                seen.unlink()
                agents.start(self.root, resume=first["id"], socket_path=self.socket)
                until(lambda: seen.exists())
                resumed = read(seen)["argv"]
                self.assertIn(native, resumed)
                self.assertNotIn(prompt, resumed)
                self.assertNotIn("--last", resumed)
                self.stop_agent(first)

    def test_codex_missing_registration_never_guesses_latest_session(self):
        self.fake_cli("codex", report=False)
        first = agents.start(self.root, "unregistered", "test", socket_path=self.socket)
        directory = agents.directory(self.root, first["id"])
        until(lambda: (directory / "seen.json").exists())
        self.stop_agent(first)
        launches = list((directory / "launches").iterdir())
        with self.assertRaisesRegex(ValueError, "No native session ID recorded"):
            agents.start(self.root, resume=first["id"], socket_path=self.socket)
        self.assertEqual(list((directory / "launches").iterdir()), launches)


if __name__ == "__main__":
    unittest.main()
