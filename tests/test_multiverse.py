import concurrent.futures
import json
import os
from pathlib import Path
import pty
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multiverse import agents, universe
from multiverse.tools import send
from multiverse.store import ack, append_message, inbox, read, write
from multiverse.tmux import Tmux, observe
from multiverse.dashboard import LocalBackend, columns, hierarchy


def append_many(args):
    path, index = args
    for n in range(12):
        append_message(Path(path), f"{index}/{n} — hello\nworld", {"name": str(index)})


def until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for condition")


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mv2-test-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.root = universe.create(self.work / ".multiverse", "test", workdir=self.work)


class FilesTest(BaseTest):
    def test_clean_recursive_install(self):
        child = universe.create(self.root / "subverses" / "review", "review", workdir=self.work)
        universe.create(child / "subverses" / "verify", "verify", workdir=self.work)
        (child / "state" / "secret").write_text("never clone live state")
        clone = universe.create(self.work / "copy", "copy", str(self.root), self.work)
        old = list(universe.tree(self.root))
        new = list(universe.tree(clone))
        self.assertEqual(len(new), 3)
        self.assertFalse((new[1] / "state" / "secret").exists())
        for a, b in zip(old, new):
            self.assertNotEqual(read(a / "universe.json")["id"], read(b / "universe.json")["id"])
            self.assertEqual(universe.workspace(b), self.work)
        self.assertEqual(universe.resolve(child), child)
        self.assertEqual(universe.parent(child), self.root)
        with self.assertRaises(ValueError):
            universe.create(self.root, "overwrite")

    def test_catalog_loads_only_exact_unique_entry(self):
        self.assertIn("Review the requested change", universe.command(self.root, "review"))
        duplicate = self.root / "commands" / "review"
        duplicate.mkdir()
        write(duplicate / "entry.json", {"description": "ambiguous"})
        (duplicate / "prompt.md").write_text("DO NOT EXECUTE")
        with self.assertRaises(ValueError):
            universe.command(self.root, "review")
        with self.assertRaises(ValueError):
            universe.command(self.root, "rev")
        self.assertEqual(universe.command(self.root, "commands/review"), "DO NOT EXECUTE")

    def test_bootstrap_does_not_load_skill_bodies(self):
        skill = self.root / "skills" / "coordination" / "SKILL.md"
        skill.write_text("SENTINEL_LONG_CAPABILITY\n" * 1000)
        prompt = universe.bootstrap(self.root, {"id": "worker-id", "name": "worker", "universe_id": "u"})
        self.assertNotIn("SENTINEL_LONG_CAPABILITY", prompt)
        self.assertLess(len(prompt.split()), 260)

    def test_concurrent_inbox_and_explicit_ack(self):
        agent = self.root / "state" / "agents" / "test"
        agent.mkdir()
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            list(pool.map(append_many, [(str(agent), n) for n in range(4)]))
        first = inbox(agent)
        self.assertEqual(len(first["messages"]), 48)
        self.assertEqual(len({m["id"] for m in first["messages"]}), 48)
        self.assertEqual(inbox(agent), first)  # Peek/crash does not discard anything.
        ack(agent, first["cursor"])
        self.assertEqual(inbox(agent)["messages"], [])
        append_message(agent, "next", {"name": "user"})
        self.assertEqual(inbox(agent)["messages"][0]["text"], "next")
        with self.assertRaises(ValueError):
            ack(agent, first["cursor"] + 1)
        with self.assertRaises(ValueError):
            ack(agent, 0)

    def test_incomplete_record_never_acknowledged_or_overwritten(self):
        agent = self.work / "agent"
        agent.mkdir()
        append_message(agent, "complete", {})
        with (agent / "inbox.jsonl").open("ab") as out:
            out.write(b'{"incomplete":')
        batch = inbox(agent)
        self.assertEqual(len(batch["messages"]), 1)
        ack(agent, batch["cursor"])
        self.assertEqual(inbox(agent)["messages"], [])
        with self.assertRaises(ValueError):
            append_message(agent, "must not hide corruption", {})

    def test_missing_local_server_is_stopped_despite_old_hostname(self):
        with patch.object(Tmux, "snapshot", return_value=({}, None)):
            result = observe({}, {"host": "some-other-host", "tmux": {"pane": "%1", "socket": "/tmp/nope"}})
        self.assertEqual(result["live"], "stopped")

    def test_subverse_view_is_local_and_parent_shows_summary(self):
        child = universe.create(self.root / "subverses" / "review", "review", workdir=self.work)
        write(child / "views" / "default.json", {"columns": ["name", "phase", "harness"]})
        self.assertEqual(columns(child), ["name", "phase", "harness"])
        self.assertNotEqual(columns(child), columns(self.root))
        nested = universe.create(child / "subverses" / "verify", "verify", workdir=self.work)
        data = LocalBackend().snapshot(self.root)
        self.assertEqual([row["path"] for row in hierarchy(data)], [str(self.root), str(child), str(nested)])
        self.assertEqual(data["groups"][-1]["parent"], str(child))
        self.assertNotEqual(data["groups"][0]["accent"], data["groups"][1]["accent"])
        write(child / "views" / "default.json", {"title": "Reviews", "accent": "#ff00aa"})
        styled = LocalBackend().snapshot(self.root)["groups"][1]
        self.assertEqual((styled["name"], styled["accent"]), ("Reviews", "#ff00aa"))


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for terminal integration")
class TerminalBase(BaseTest):
    def setUp(self):
        super().setUp()
        self.socket = str(self.work / "tmux.sock")
        self.backend = Tmux(self.socket)
        self.addCleanup(self.kill_server)
        harness = self.root / "harnesses" / "fake"
        harness.mkdir()
        (harness / "worker.py").write_text(
            "import json, os, sys, time\nfrom pathlib import Path\n"
            "Path(os.environ['MV2_AGENT_DIR'], 'seen.json').write_text(json.dumps(sys.argv[1:]))\n"
            "print('FAKE READY', flush=True)\nwhile True: time.sleep(1)\n")
        self.literal = "a 'quote'\n$(touch SHOULD_NOT_EXIST) `touch ALSO_NOT`"
        argv = [sys.executable, "{harness_dir}/worker.py", self.literal, "{task_file}"]
        write(harness / "harness.json", {"schema": 1, "spawn": argv, "resume": argv})

    def kill_server(self):
        try:
            self.backend.run("kill-server")
        except (OSError, RuntimeError):
            pass

    def spawn(self):
        result = agents.start(self.root, "tester", "a task", "fake", socket_path=self.socket)
        agent_dir = agents.directory(self.root, result["id"])
        until(lambda: (agent_dir / "seen.json").exists())
        return result, agent_dir


class TerminalTest(TerminalBase):
    def test_real_tmux_identity_resume_and_literal_arguments(self):
        result, agent_dir = self.spawn()
        self.assertEqual(read(agent_dir / "seen.json")[0], self.literal)
        self.assertFalse((self.work / "SHOULD_NOT_EXIST").exists())
        self.assertFalse((self.work / "ALSO_NOT").exists())
        row = agents.listing(self.root)[0]
        self.assertEqual(row["live"], "alive")
        self.assertIn("FAKE READY", agents.terminal_action(self.root, result["id"], "inspect"))
        pane = row["runtime"]["tmux"]["pane"]
        self.backend.run("rename-window", "-t", pane, "unrelated-name")
        self.assertEqual(agents.listing(self.root)[0]["live"], "alive")
        with self.assertRaises(ValueError):
            agents.start(self.root, resume=result["id"], socket_path=self.socket)
        agents.terminal_action(self.root, result["id"], "stop")
        self.assertEqual(agents.listing(self.root)[0]["live"], "stopped")
        until(lambda: not self.backend.snapshot()[0])
        time.sleep(0.1)
        resumed = agents.start(self.root, resume=result["id"], socket_path=self.socket)
        self.assertEqual(resumed["id"], result["id"])
        self.assertNotEqual(resumed["runtime"]["launch_id"], row["runtime"]["launch_id"])
        with self.assertRaises(ValueError):
            agents.update_runtime(agent_dir, row["runtime"]["launch_id"], status="idle")

    def test_wrong_launch_tag_refuses_control(self):
        result, _ = self.spawn()
        pane = result["runtime"]["tmux"]["pane"]
        self.backend.run("set-option", "-p", "-t", pane, "@mv2_launch_id", "someone-else")
        self.assertEqual(agents.listing(self.root)[0]["live"], "stopped")
        with self.assertRaises(ValueError):
            agents.terminal_action(self.root, result["id"], "stop")
        self.assertIn(pane, self.backend.snapshot()[0])

    def test_hostname_change_preserves_owned_pane_and_duplicate_launch_guard(self):
        result, agent_dir = self.spawn()
        agents.update_runtime(agent_dir, result['runtime']['launch_id'], host='old-hostname')
        self.assertEqual(agents.listing(self.root)[0]['live'], 'alive')
        self.assertIn('FAKE READY', agents.terminal_action(self.root, result['id'], 'inspect'))
        with self.assertRaisesRegex(ValueError, 'refusing a duplicate launch'):
            agents.start(self.root, resume=result['id'], socket_path=self.socket)
        pane = result['runtime']['tmux']['pane']
        self.backend.run('set-option', '-p', '-t', pane, '@mv2_launch_id', 'someone-else')
        self.assertEqual(agents.listing(self.root)[0]['live'], 'stopped')
        with self.assertRaises(ValueError):
            agents.terminal_action(self.root, result['id'], 'stop')
        self.assertIn(pane, self.backend.snapshot()[0])

    def test_missing_pane_after_hostname_change_can_resume_same_session(self):
        result, agent_dir = self.spawn()
        native_id = '00000000-0000-4000-8000-000000000001'
        agents.bind_session(self.root, result['id'], native_id)
        self.backend.run('kill-pane', '-t', result['runtime']['tmux']['pane'])
        until(lambda: not self.backend.snapshot()[0])
        agents.update_runtime(agent_dir, result['runtime']['launch_id'], host='old-hostname', state='running')
        self.assertEqual(agents.listing(self.root)[0]['live'], 'stopped')
        time.sleep(0.1)
        resumed = agents.start(self.root, resume=result['id'], socket_path=self.socket)
        self.assertEqual(resumed['native_session_id'], native_id)
        self.assertEqual(resumed['id'], result['id'])
        self.assertNotEqual(resumed['runtime']['launch_id'], result['runtime']['launch_id'])
        self.assertEqual(agents.listing(self.root)[0]['live'], 'alive')

    def test_dashboard_can_close_without_stopping_agent(self):
        self.spawn()
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, str(ROOT / "bin" / "mv2"), "-u", str(self.root), "tui"],
                                   stdin=slave, stdout=slave, stderr=slave, env={**os.environ, "TERM": "xterm-256color"})
        os.close(slave)
        try:
            def read_until(marker):
                output = b""
                deadline = time.monotonic() + 5
                while marker not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.2)[0]:
                        output += os.read(master, 65536)
                self.assertTrue(marker in output, f"Missing {marker!r}; terminal tail: {output[-1800:]!r}")
                return output

            read_until(b"tester")
            # Exercise the actual attach command and return to the dashboard.
            self.backend.run("set-option", "-g", "prefix", "C-b")
            self.backend.run("bind-key", "d", "detach-client")
            os.write(master, b"\r")
            read_until(b"FAKE READY")
            until(lambda: self.backend.run("list-clients", "-F", "#{client_session}"))
            os.write(master, b"\x02d")
            output = read_until("◈ test".encode())
            os.write(master, b"q")
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(agents.listing(self.root)[0]["live"], "alive")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


@unittest.skipUnless(shutil.which("pi") and shutil.which("tmux"), "Pi and tmux required")
class PiTest(TerminalBase):
    def test_real_pi_native_inbox_and_resume(self):
        harness_dir = self.root / "harnesses" / "pi"
        spec = read(harness_dir / "harness.json")
        extra = ["--offline", "--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates",
                 "--no-approve", "--extension", str(Path(__file__).with_name("mock-provider.ts")),
                 "--provider", "mv2-test", "--model", "mv2-test"]
        spec["settings"].update(args=extra, inbox_bridge=True)
        write(harness_dir / "harness.json", spec)
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.work / "pi-home"), "PI_OFFLINE": "1"}):
            result = agents.start(self.root, "pi-test", "First test turn", "pi", socket_path=self.socket)
        agent_dir = agents.directory(self.root, result["id"])
        log = agent_dir / "mock-requests.jsonl"
        try:
            until(lambda: log.exists(), seconds=25)
            first = send.run(self.root, agent=result["id"], text="INBOX_SENTINEL", sender={"name": "test"})
            self.assertEqual(first["state"], "native_queued", first)
            until(lambda: "INBOX_SENTINEL" in log.read_text(), seconds=20)
            until(lambda: not inbox(agent_dir)["messages"])
            self.assertIn(first["id"], (agent_dir / "session.jsonl").read_text())
            request = json.loads(log.read_text().splitlines()[0])
            self.assertIn("You work in a Multiverse universe", request["systemPrompt"])
            self.assertEqual(sorted(t["name"] for t in request["tools"]), ["bash", "edit", "read", "write"])
            until(lambda: agents.runtime(agent_dir).get("status") == "idle")
            agents.terminal_action(self.root, result["id"], "stop")
            until(lambda: not self.backend.snapshot()[0])
            time.sleep(0.1)
            before = len(log.read_text().splitlines())
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.work / "pi-home"), "PI_OFFLINE": "1"}):
                agents.start(self.root, resume=result["id"], socket_path=self.socket)
            until(lambda: agents.runtime(agent_dir).get("native_bridge") == "pi-socket-v1", seconds=20)
            sent = send.run(self.root, agent=result["id"], text="AFTER_RESUME", sender={"name": "test"})
            self.assertEqual(sent["state"], "native_queued", sent)
            until(lambda: "AFTER_RESUME" in log.read_text(), seconds=25)
            self.assertEqual(len(log.read_text().splitlines()), before + 1)
            self.assertIn("First test turn", log.read_text().splitlines()[-1])
        except Exception:
            print("Pi runtime:", agents.runtime(agent_dir), file=sys.stderr)
            try:
                print(agents.terminal_action(self.root, result["id"], "inspect"), file=sys.stderr)
            except Exception:
                pass
            raise


if __name__ == "__main__":
    unittest.main()
