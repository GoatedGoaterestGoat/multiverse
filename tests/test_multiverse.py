import concurrent.futures
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from native_safety import install as install_native_safety
install_native_safety()
from isolation import isolate, track

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.runtime.process import process_snapshot
from multiverse.universe import templates
from multiverse.tools import send
from multiverse.universe.mailbox import append_message, inbox
from multiverse.store import read, write
from multiverse.terminal.tmux import Tmux


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
        isolate(self)
        self.temp = tempfile.TemporaryDirectory(prefix="mv2-test-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.root = templates.create(self.work / ".multiverse", "test", workdir=self.work)


class FilesTest(BaseTest):
    def test_concurrent_inbox_writers_and_automatic_read_position(self):
        """Four processes appending 12 multi-line non-ASCII messages each leave 48 whole messages with unique ids, and
        the read cursor advances exactly; fragile because it rests on cross-process file locks that a threaded test
        can miss, and a break loses or interleaves messages rarely and silently."""
        agent = self.root / "state" / "agents" / "test"
        agent.mkdir()
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            list(pool.map(append_many, [(str(agent), n) for n in range(4)]))
        first = inbox(agent)
        self.assertEqual(len(first["messages"]), 48)
        self.assertEqual(len({m["id"] for m in first["messages"]}), 48)
        self.assertEqual(inbox(agent, all_messages=True)["messages"],
                         [{**m, "tracking": {"submission_state": "manual"}} for m in first["messages"]])
        self.assertEqual(inbox(agent)["messages"], [])
        append_message(agent, "next", {"name": "user"})
        self.assertEqual(inbox(agent)["messages"][0]["text"], "next")
        with self.assertRaises(ValueError):
            inbox(agent, after=first["cursor"] + 1)
        self.assertEqual(inbox(agent, after=0)["messages"], [])


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for terminal integration")
class TerminalBase(BaseTest):
    def setUp(self):
        super().setUp()
        self.socket = str(self.work / "tmux.sock")
        self.backend = Tmux(self.socket)
        track(tmux_socket=self.socket)  # its server runs in its own session: tests/run.py ends it if this test can't
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
        # Runners finish receipt/queue cleanup after tmux tears down a pane.
        # Detached wrapper cleanup can remove its pane before its last runtime
        # write. Track all core processes in this fixture, not just live panes.
        def core_processes():
            output = subprocess.run(['ps', '-axo', 'pid=,args='], text=True,
                                    capture_output=True, check=True, timeout=5).stdout
            runner = str(ROOT / 'src/multiverse/runtime/runner.py') + ' '
            workspace = ' ' + str(self.work) + '/'
            return {int(fields[0]) for line in output.splitlines()
                    if len(fields := line.strip().split(None, 1)) == 2
                    and runner in fields[1] and workspace in fields[1]}

        panes, _ = self.backend.snapshot()
        pids = core_processes() | {int(pane['pane_pid']) for pane in panes.values() if pane.get('pane_pid')}
        try:
            self.backend.run("kill-server")
        except (OSError, RuntimeError):
            pass
        def exited(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            # An orphan can have exited before its reaper runs. It cannot write
            # fixture state; unknown inspection of a live PID still blocks.
            observed = process_snapshot(pid)
            return observed is not None and observed[1].startswith('Z')
        try:
            until(lambda: all(exited(pid) for pid in pids), seconds=15)
        except AssertionError:
            remaining = sorted(pid for pid in pids if not exited(pid))
            try:
                detail = subprocess.run(['ps', '-o', 'pid,ppid,stat,etime,comm', '-p', ','.join(map(str, remaining))],
                                        text=True, capture_output=True, check=False, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError) as error:
                detail = f'Process inspection unavailable: {error}'
            raise AssertionError(f'Terminal processes did not exit within 15 s: {remaining}\n{detail}') from None
        # A runner can spawn the detached helper while it is exiting above.
        until(lambda: not core_processes(), seconds=15)

    def spawn(self):
        result = lifecycle.start(self.root, "tester", "a task", "fake", socket_path=self.socket)
        agent_dir = agents.directory(self.root, result["id"])
        until(lambda: (agent_dir / "seen.json").exists())
        return result, agent_dir


class TerminalCleanupTest(BaseTest):
    def test_cleanup_waits_for_running_processes_but_not_unreaped_exits(self):
        """An unreaped exit cannot write fixture state, but a live or uninspectable process must still block cleanup."""
        original_until = until
        for state in ('running', 'uninspectable', 'exited but unreaped'):
            with self.subTest(state=state):
                child = subprocess.Popen([sys.executable, '-c', 'pass' if state == 'exited but unreaped'
                                           else 'import time; time.sleep(60)'],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    if state == 'exited but unreaped':
                        original_until(lambda: (info := process_snapshot(child.pid)) and info[1].startswith('Z'))
                    self.backend = Mock()
                    self.backend.snapshot.return_value = ({'%1': {'pane_pid': str(child.pid)}}, None)
                    observe = patch(__name__ + '.process_snapshot', return_value=None) if state == 'uninspectable' else nullcontext()
                    with observe, patch(__name__ + '.until', side_effect=lambda predicate, seconds=15:
                                        original_until(predicate, seconds=.25)):
                        if state == 'exited but unreaped':
                            TerminalBase.kill_server(self)
                            self.assertIsNone(child.returncode, 'The helper observes an exit; the parent still owns reaping it')
                        else:
                            with self.assertRaises(AssertionError) as caught:
                                TerminalBase.kill_server(self)
                            self.assertIn(str(child.pid), str(caught.exception))
                finally:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=5)


class TerminalTest(TerminalBase):
    def test_real_tmux_literal_arguments_and_duplicate_launch_guard(self):
        """On real tmux, argv with shell metacharacters reaches the process literally, and resume refuses a duplicate
        launch while the pane lives, even after a hostname change; fragile because argv crosses tmux's command string,
        where quoting is easily lost, and a break runs injected shell or a second copy of an agent."""
        result, agent_dir = self.spawn()
        self.assertEqual(read(agent_dir / "seen.json")[0], self.literal)
        self.assertFalse((self.work / "SHOULD_NOT_EXIST").exists())
        self.assertFalse((self.work / "ALSO_NOT").exists())
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row["live"], "alive")
        self.assertIn("FAKE READY", lifecycle.terminal_action(self.root, result["id"], "inspect"))
        pane = row["runtime"]["tmux"]["pane"]
        self.backend.run("rename-window", "-t", pane, "unrelated-name")
        self.assertEqual(lifecycle.listing(self.root)[0]["live"], "alive")
        with self.assertRaisesRegex(ValueError, "refusing a duplicate launch"):
            lifecycle.start(self.root, resume=result["id"], socket_path=self.socket)
        agents.update_runtime(agent_dir, row["runtime"]["launch_id"], host="old-hostname")
        self.assertEqual(lifecycle.listing(self.root)[0]["live"], "alive")
        self.assertIn("FAKE READY", lifecycle.terminal_action(self.root, result["id"], "inspect"))
        with self.assertRaisesRegex(ValueError, "refusing a duplicate launch"):
            lifecycle.start(self.root, resume=result["id"], socket_path=self.socket)
        lifecycle.terminal_action(self.root, result["id"], "stop")
        self.assertEqual(lifecycle.listing(self.root)[0]["live"], "dormant")
        until(lambda: not self.backend.snapshot()[0])
        time.sleep(0.1)
        resumed = lifecycle.start(self.root, resume=result["id"], socket_path=self.socket)
        self.assertEqual(resumed["id"], result["id"])
        self.assertNotEqual(resumed["runtime"]["launch_id"], row["runtime"]["launch_id"])
        with self.assertRaises(ValueError):
            agents.update_runtime(agent_dir, row["runtime"]["launch_id"], status="idle")


@unittest.skipUnless(shutil.which("pi") and shutil.which("tmux"), "Pi and tmux required")
class PiTest(TerminalBase):
    def test_real_pi_native_inbox_and_resume(self):
        """With a real Pi a native send is queued, its message id lands in session.jsonl, and the extension reconnects
        after resume; fragile because it depends on Pi's extension API and session format, and a break leaves messages
        only in the inbox while send reports nothing wrong."""
        harness_dir = self.root / "harnesses" / "pi"
        spec = read(harness_dir / "harness.json")
        extra = ["--offline", "--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates",
                 "--no-approve", "--extension", str(Path(__file__).with_name("mock-provider.ts")),
                 "--provider", "mv2-test", "--model", "mv2-test"]
        spec["settings"].update(args=extra, inbox_bridge=True)
        write(harness_dir / "harness.json", spec)
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.work / "pi-home"), "PI_OFFLINE": "1"}):
            result = lifecycle.start(self.root, "pi-test", "First test turn", "pi", socket_path=self.socket,
                                  wait=True, wait_timeout=25)
        agent_dir = agents.directory(self.root, result["id"])
        log = agent_dir / "mock-requests.jsonl"
        try:
            until(lambda: log.exists(), seconds=25)
            first = send.run(self.root, agent=result["id"], text="INBOX_SENTINEL", sender={"name": "test"})
            self.assertEqual(first["state"], "native_queued", first)
            until(lambda: "INBOX_SENTINEL" in log.read_text(), seconds=20)
            until(lambda: not inbox(agent_dir)["messages"])
            internal_id = inbox(agent_dir, all_messages=True)['messages'][-1]['id']
            self.assertIn(internal_id, (agent_dir / "session.jsonl").read_text())
            request = json.loads(log.read_text().splitlines()[0])
            self.assertIn("You work in a Multiverse universe", request["systemPrompt"])
            self.assertEqual(sorted(t["name"] for t in request["tools"]), ["bash", "edit", "read", "write"])
            until(lambda: agents.runtime(agent_dir).get("status") == "idle")
            lifecycle.terminal_action(self.root, result["id"], "stop")
            until(lambda: not self.backend.snapshot()[0])
            time.sleep(0.1)
            before = len(log.read_text().splitlines())
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.work / "pi-home"), "PI_OFFLINE": "1"}):
                resumed = lifecycle.start(self.root, resume=result["id"], socket_path=self.socket, wait=True, wait_timeout=25)
                self.assertTrue(resumed['ready'], resumed)
            until(lambda: agents.runtime(agent_dir).get("native_bridge") == "pi-socket-v1", seconds=20)
            sent = send.run(self.root, agent=result["id"], text="AFTER_RESUME", sender={"name": "test"})
            self.assertEqual(sent["state"], "native_queued", sent)
            until(lambda: "AFTER_RESUME" in log.read_text(), seconds=25)
            self.assertEqual(len(log.read_text().splitlines()), before + 1)
            self.assertIn("First test turn", log.read_text().splitlines()[-1])
        except Exception:
            print("Pi runtime:", agents.runtime(agent_dir), file=sys.stderr)
            try:
                print(lifecycle.terminal_action(self.root, result["id"], "inspect"), file=sys.stderr)
            except Exception:
                pass
            raise


if __name__ == "__main__":
    unittest.main()
