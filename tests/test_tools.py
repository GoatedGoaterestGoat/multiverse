"""Exercise standalone tools and compatibility CLI against the same file protocol."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from multiverse import cli, universe
from multiverse.dashboard import LocalBackend
from multiverse.store import read, write
from multiverse.tools import spawn


class ToolsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = universe.create(self.work / "universe", "tools", workdir=self.work)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MV2_")}
        self.env["PYTHONPATH"] = str(PROJECT / "src")
        self.agent = str(uuid4())
        self.directory = self.root / "state/agents" / self.agent
        write(self.directory / "properties.json", {"id": self.agent, "name": "worker",
              "universe_id": read(self.root / "universe.json")["id"]})
        write(self.directory / "runtime.json", {"launch_id": "current", "state": "stopped"})

    def invoke(self, action, *args, standalone=True, env=None, code=0, text=None):
        entry = ["-m", "multiverse.tools." + ("list_agents" if action == "list" else action.replace("-", "_"))]
        if not standalone:
            entry = [str(PROJECT / "bin/mv2")]
        result = subprocess.run([sys.executable, *entry, "-u", str(self.root),
                                 *([] if standalone else [action]), *args],
                                env=env or self.env, input=text, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_every_tool_runs_independently_and_cli_help_stays_available(self):
        for action in cli.COMMANDS:
            with self.subTest(action=action):
                result = self.invoke(action, "--help")
                self.assertIn("--universe", result.stdout)
        # Non-UI commands must remain usable without importing Textual.
        result = subprocess.run([sys.executable, "-c",
            "import sys; from multiverse.cli import parser; parser(); "
            "assert 'textual' not in sys.modules"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_and_standalone_share_list_catalog_command_and_failure_output(self):
        for action, args in (("list", ("--json",)), ("catalog", ()),
                             ("command", ("help", "literal $(false)", "--flag"))):
            self.assertEqual(self.invoke(action, *args).stdout,
                             self.invoke(action, *args, standalone=False).stdout)
        for standalone in (True, False):
            result = self.invoke("command", "missing", standalone=standalone, code=1)
            self.assertIn("Expected one exact match", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            write(self.directory / "runtime.json", {"state": "failed", "error": "launch failed"})
            self.assertFalse(json.loads(self.invoke("doctor", standalone=standalone, code=1).stdout)["ok"])

    def test_message_roundtrip_across_tool_cli_and_tui_keeps_sender_and_cursor(self):
        message = "literal 'quoted'\n$(false) `false`"
        first = json.loads(self.invoke("send", self.agent, "--stdin", "--queue-only", text=message).stdout)
        self.assertEqual(first["state"], "queued")
        batch = json.loads(self.invoke("inbox", "--agent", self.agent, standalone=False).stdout)
        self.assertEqual(batch["messages"][0]["text"], message)
        self.assertNotIn("id", first)
        self.assertNotIn("id", batch["messages"][0])
        self.assertEqual(json.loads(self.invoke("inbox", "--agent", self.agent).stdout)["messages"], [])
        LocalBackend().send(self.root, self.agent, "from dashboard")
        batch = json.loads(self.invoke("inbox", "--agent", self.agent, "--all").stdout)
        self.assertEqual([m["text"] for m in batch["messages"]], [message, "from dashboard"])
        self.assertEqual(batch["messages"][-1]["from"], {"name": "user", "agent_id": None})

    def test_retired_message_commands_and_arguments_are_not_available(self):
        import contextlib
        import io
        self.assertNotIn('ack', cli.COMMANDS)
        self.assertFalse((PROJECT / 'src/multiverse/tools/ack.py').exists())
        for args in (['ack'], ['send', self.agent, '--text', 'hello', '--reply-to', 'old'],
                     ['deliver', self.agent, '--message-id', 'old', '--retry'],
                     ['deliver', self.agent], ['deliver', self.agent, '--check', '--retry']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(args)

    def test_doctor_checks_inbox_without_consuming_unread_messages(self):
        self.invoke('send', self.agent, '--text', 'keep unread', '--queue-only')
        self.invoke('doctor')
        self.assertFalse((self.directory / 'inbox.cursor').exists())
        batch = json.loads(self.invoke('inbox', '--agent', self.agent).stdout)
        self.assertEqual([m['text'] for m in batch['messages']], ['keep unread'])

    def test_old_reply_references_and_read_markers_stay_out_of_normal_inbox(self):
        old = {'id': 'old-message', 'reply_to': 'old-parent', 'text': 'still readable', 'from': {'name': 'reviewer'}}
        (self.directory / 'inbox.jsonl').write_text(json.dumps(old) + '\n')
        write(self.directory / 'inbox-state.json', {'old-message': {'acknowledged_at': 'old-time'}})
        history = json.loads(self.invoke('inbox', '--agent', self.agent, '--all').stdout)
        self.assertEqual(history['messages'][0]['id'], old['id'])
        batch = json.loads(self.invoke('inbox', '--agent', self.agent).stdout)
        self.assertEqual(batch['messages'], [{'from': {'name': 'reviewer'}, 'text': 'still readable'}])
        self.assertNotIn('old-message', json.dumps(batch))
        self.assertNotIn('old-parent', json.dumps(batch))

    def test_standalone_callbacks_reject_stale_launch_without_mutating_state(self):
        before = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()}
        callbacks = (("inbox", ()),
                     ("report", ("--phase", "done")), ("runtime-update", ("--status", "idle")),
                     ("session-bind", ("--session-id", str(uuid4()))))
        for action, args in callbacks:
            with self.subTest(action=action):
                result = self.invoke(action, "--agent", self.agent, *args,
                                     env={**self.env, "MV2_LAUNCH_ID": "old"}, code=1)
                self.assertIn("Outdated launch", result.stderr)
        after = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_unavailable_native_delivery_has_nonzero_exit_but_retains_message(self):
        properties = read(self.directory / 'properties.json')
        write(self.root / 'harnesses/legacy/harness.json', {'schema': 1, 'spawn': ['/bin/true'], 'resume': ['/bin/true']})
        properties.update(harness='legacy', workspace=str(self.work), native_session_id=str(uuid4()))
        write(self.directory / 'properties.json', properties)
        result = json.loads(self.invoke('send', self.agent, '--text', 'saved on failure', code=1).stdout)
        self.assertEqual(result['state'], 'unavailable')
        self.assertIn('cannot wake', result['native_delivery']['reason'])
        self.assertTrue(result['saved'])
        batch = json.loads(self.invoke('inbox', '--agent', self.agent, '--all').stdout)
        self.assertIn('id', batch['messages'][0])
        self.assertNotIn('id', result)
        self.assertNotIn('message_id', result['native_delivery'])
        self.assertEqual(batch['messages'][0]['text'], result['text'])

    def test_init_and_tui_subverse_install_fresh_scoped_definitions(self):
        installed = json.loads(self.invoke("init", str(self.work / "fresh"), "--name", "fresh").stdout)
        self.assertEqual(installed["name"], "fresh")
        child = LocalBackend().subverse(self.root, {"name": "child"})
        self.assertEqual(child, self.root / "subverses/child")
        self.assertNotEqual(read(child / "universe.json")["id"], read(self.root / "universe.json")["id"])
        self.assertEqual(list((child / "state/agents").iterdir()), [])

    def test_cli_and_tui_spawn_use_same_tool_with_original_target_and_literals(self):
        literal = "--flag\n$(false) `false` {literal}"
        task_file = self.work / "task.md"
        task_file.write_text(literal)
        with patch.object(spawn.agents, "start", return_value={"id": "created"}) as start:
            cli.run(cli.parser().parse_args(["-u", str(self.root), "spawn", "--name", "worker",
                                            "--task-file", str(task_file), "--model", "chosen"]))
            first = start.call_args
            LocalBackend().spawn(self.root, {"name": "worker", "task": literal, "model": "chosen"})
            self.assertEqual(first, start.call_args)
            self.assertEqual(first.args, (self.root, "worker", literal, "codex", "chosen"))


if __name__ == "__main__":
    unittest.main()
