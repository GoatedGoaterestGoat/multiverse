"""End-to-end test of the pi extension against a mock model.

Covers the seams that only exist at runtime and cannot be reached by importing
Python: that pi accepts the extension's typebox tool schemas, that a tool call
reaches ``mvctl`` and comes back as a tool result, that pi's lifecycle events
write ``status.json`` and the native session id, and that a message queued on
disk is claimed, delivered into the conversation, and committed out of the
inbox.

Needs ``pi`` on PATH and the plugin's virtualenv (``multiverse setup``); skips
itself otherwise. No network and no model credentials — ``mock_openai_server``
serves every request.

    python tests/test_pi_extension.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

EXTENSION = PLUGIN_ROOT / "pi-extension" / "index.ts"
MOCK_PROVIDER = PLUGIN_ROOT / "tests" / "mock-provider.ts"
MOCK_SERVER = PLUGIN_ROOT / "tests" / "mock_openai_server.py"
VENV_PYTHON = PLUGIN_ROOT / ".venv" / "bin" / "python"

#: The six tools the extension must expose to the model.
EXPECTED_TOOLS = (
    "list_agents",
    "message_agent",
    "ask_agent",
    "check_inbox",
    "monitor_inbox",
    "spawn_agent",
)

_PI_TIMEOUT_SECONDS = 90
_SERVER_START_TIMEOUT_SECONDS = 10


def _inbox_dir(root: Path, cwd: Path, name: str) -> Path:
    """The agent's inbox under a specific state root.

    Deliberately not ``src.inbox.agent_inbox_dir``: that module resolves
    ``MULTIVERSE_ROOT`` once at import time, from the *test process*
    environment, so it would point at the real ``~/.multiverse`` while the
    subprocesses under test use a temp root. Mirroring the one-line encoding
    here keeps the assertions pointed at the directory the code under test
    actually wrote to.
    """
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    return root / "inbox" / encoded / name


def _pi_prompt_dir(root: Path, cwd: Path, name: str) -> Path:
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))
    return root / "secondbrain" / "pi_prompts" / encoded / name


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _skip_reason() -> str | None:
    if shutil.which("pi") is None:
        return "pi is not on PATH"
    if not EXTENSION.is_file():
        return f"missing {EXTENSION}"
    if not VENV_PYTHON.is_file():
        return "plugin virtualenv missing — run `multiverse setup`"
    return None


@unittest.skipIf(_skip_reason() is not None, _skip_reason() or "")
class PiExtensionEndToEndTests(unittest.TestCase):
    """Each test drives one real ``pi`` run scripted by the mock provider."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mv-pi-test-"))
        self.addCleanup(shutil.rmtree, self.work, True)

        self.port = _free_port()
        self.state = self.work / "state"
        self.ws = self.work / "ws"
        self.sessions = self.work / "sessions"
        self.session_dir = self.state / "sessions" / "mv1"
        for d in (self.session_dir, self.ws, self.sessions):
            d.mkdir(parents=True, exist_ok=True)

        self.status_path = self.session_dir / "status.json"
        self.name = "tester"
        self.env = {
            **os.environ,
            "MULTIVERSE_ROOT": str(self.state),
            "MULTIVERSE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "MULTIVERSE_SESSION_NAME": self.name,
            "MULTIVERSE_SESSION_CWD": str(self.ws),
            "MULTIVERSE_STATUS": str(self.status_path),
            "PI_CODING_AGENT_SESSION_DIR": str(self.sessions),
            "MOCK_OPENAI_URL": f"http://127.0.0.1:{self.port}/v1",
        }

    # -- harness ----------------------------------------------------------

    def _run_pi(self, script: list[dict], prompt: str) -> Path:
        """Serve ``script`` from the mock provider and run one pi turn.

        Returns the path of the request log, so a test can assert on what pi
        actually sent — which is the only place the tool schemas are visible.
        """
        script_path = self.work / "script.json"
        script_path.write_text(json.dumps(script))
        requests_path = script_path.with_suffix(".requests.jsonl")

        server = subprocess.Popen(
            [sys.executable, str(MOCK_SERVER), "--port", str(self.port),
             "--script", str(script_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(server.kill)
        self._await_server()

        try:
            self.last_run = subprocess.run(
                ["pi",
                 "-e", str(MOCK_PROVIDER),
                 "-e", str(EXTENSION),
                 "--provider", "mock", "--model", "mock-model",
                 "--no-extensions", "--no-skills", "--no-context-files",
                 "-p", prompt],
                cwd=str(self.ws), env=self.env, capture_output=True, text=True,
                timeout=_PI_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic path
            self.fail(f"pi did not exit within {_PI_TIMEOUT_SECONDS}s")
        finally:
            server.terminate()
            server.wait(timeout=10)
        return requests_path

    def _diagnostics(self) -> str:
        """pi's own output, for a failure that would otherwise say only that a
        marker is missing from a request log."""
        run = getattr(self, "last_run", None)
        if run is None:
            return ""
        return f"\n--- pi stdout ---\n{run.stdout}\n--- pi stderr ---\n{run.stderr}"

    def _await_server(self) -> None:
        deadline = time.monotonic() + _SERVER_START_TIMEOUT_SECONDS
        url = f"http://127.0.0.1:{self.port}/v1/models"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1):
                    return
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        self.fail("mock provider did not start")

    def _requests(self, path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    # -- tests ------------------------------------------------------------

    def test_lifecycle_writes_status_and_session_id(self) -> None:
        self._run_pi([{"text": "Nothing to do."}], "hello")

        status = json.loads(self.status_path.read_text())
        # Not asserting "ended": the status write is fire-and-forget (it must
        # never block a turn), so a one-shot run can exit before the final
        # write lands. What matters here is that the lifecycle handlers ran at
        # all and wrote a status the dashboard understands.
        self.assertIn(
            status["status"], {"idle", "working", "listening", "ended"},
            self._diagnostics(),
        )

        recorded = self.session_dir / "runtime_session_id"
        self.assertTrue(recorded.is_file(), "native session id was not recorded")
        self.assertTrue(recorded.read_text().strip(), "recorded session id is empty")

    def test_all_six_tools_are_offered_to_the_model(self) -> None:
        # The schemas pi sends the provider are the contract the model sees; a
        # malformed typebox schema shows up here and nowhere else.
        log = self._requests(self._run_pi([{"text": "ok"}], "hello"))
        for tool in EXPECTED_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(f'"{tool}"', log)

    def test_compaction_is_left_to_pi(self) -> None:
        # The extension must not carry a compaction implementation: pi compacts
        # natively, as Codex does. Asserted on the source so the requirement
        # cannot quietly regress the next time the file is edited.
        source = EXTENSION.read_text(encoding="utf-8")
        self.assertNotIn("session_before_compact", _code_of(source))
        self.assertNotIn("compact-plan", source)
        self.assertNotIn("compact-apply", source)

    def test_exposed_schema_has_no_push_notification(self) -> None:
        # Delivery is always active on main; the parameter no longer exists on
        # message_agent and must not be advertised to the model.
        log = self._requests(self._run_pi([{"text": "ok"}], "hello"))
        self.assertIn('"message_agent"', log)
        self.assertNotIn("push_notification", log)

    def test_tool_call_round_trips_through_mvctl(self) -> None:
        log = self._requests(
            self._run_pi(
                [{"tool": {"name": "list_agents", "arguments": {"max_agents": 5}}},
                 {"text": "Done."}],
                "who else is here?",
            )
        )
        # mvctl's answer for an empty workspace comes back verbatim as the tool
        # result, which the follow-up request carries.
        self.assertIn("no named agents in this workspace", log)

    def _queue(self, body: str) -> None:
        subprocess.run(
            [str(VENV_PYTHON), "-c",
             "import sys;sys.path.insert(0,'.');"
             "from src.inbox import write_message_to_inbox as w;"
             f"w('alice', {self.name!r}, {str(self.ws)!r}, {body!r})"],
            cwd=str(PLUGIN_ROOT), env=self.env, check=True,
            capture_output=True, text=True, timeout=60,
        )

    def _pending(self) -> int:
        d = _inbox_dir(self.state, self.ws, self.name)
        return len(list(d.glob("*.json"))) if d.is_dir() else 0

    def _queue_secondbrain_prompt(self, body: str) -> None:
        subprocess.run(
            [str(VENV_PYTHON), "-c",
             "import sys;sys.path.insert(0,'.');"
             "from src.pi_prompt_queue import write_prompt as w;"
             f"w({str(self.ws)!r}, {self.name!r}, {body!r})"],
            cwd=str(PLUGIN_ROOT), env=self.env, check=True,
            capture_output=True, text=True, timeout=60,
        )

    def _enable_secondbrain(self) -> None:
        directory = self.state / "secondbrain"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "device.key.json").write_text(
            json.dumps(
                {"device_id": "device-id", "granted_workspaces": [str(self.ws)]}
            )
        )
        (directory / "state.json").write_text(
            json.dumps(
                {
                    "seeded": [f"device-id:{self.ws}"],
                    "exposed": [str(self.ws)],
                }
            )
        )

    def test_secondbrain_lifecycle_records_pi_prompt_and_response_signals(self) -> None:
        self._enable_secondbrain()
        self._run_pi([{"text": "Pi response."}], "Pi user prompt")

        prompt_files = list(
            (self.state / "secondbrain" / "user_prompt_signals").glob("*.json")
        )
        response_files = list(
            (self.state / "secondbrain" / "response_signals").glob("*.json")
        )
        self.assertEqual(len(prompt_files), 1, self._diagnostics())
        self.assertEqual(len(response_files), 1, self._diagnostics())
        self.assertEqual(
            json.loads(prompt_files[0].read_text())["text"], "Pi user prompt"
        )

    def test_idle_rpc_delivers_secondbrain_prompt_as_user_message(self) -> None:
        self._queue_secondbrain_prompt("MARKER-SECONDBRAIN-USER-PROMPT")
        script_path = self.work / "sb-rpc-script.json"
        script_path.write_text(json.dumps([{"text": "Prompt handled."}]))
        requests_path = script_path.with_suffix(".requests.jsonl")

        server = subprocess.Popen(
            [sys.executable, str(MOCK_SERVER), "--port", str(self.port),
             "--script", str(script_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(server.kill)
        self._await_server()

        rpc_log_path = self.work / "sb-rpc.log"
        with rpc_log_path.open("w") as rpc_log:
            rpc = subprocess.Popen(
                ["pi",
                 "-e", str(MOCK_PROVIDER),
                 "-e", str(EXTENSION),
                 "--provider", "mock", "--model", "mock-model",
                 "--no-extensions", "--no-skills", "--no-context-files",
                 "--mode", "rpc"],
                cwd=str(self.ws), env=self.env, stdin=subprocess.PIPE,
                stdout=rpc_log, stderr=subprocess.STDOUT, text=True,
            )
            self.addCleanup(rpc.kill)
            delivered = False
            capability_ready = False
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if rpc.poll() is not None:
                    break
                if (
                    requests_path.is_file()
                    and "MARKER-SECONDBRAIN-USER-PROMPT"
                    in requests_path.read_text(encoding="utf-8")
                ):
                    delivered = True
                    capability_path = self.session_dir / "pi_capabilities.json"
                    if capability_path.is_file():
                        capability_ready = (
                            json.loads(capability_path.read_text()).get(
                                "secondbrain_prompt_consumer"
                            )
                            == 1
                        )
                    break
                time.sleep(0.1)
            rpc.terminate()
            rpc.wait(timeout=10)
            if rpc.stdin is not None:
                rpc.stdin.close()

        server.terminate()
        server.wait(timeout=10)
        diagnostics = (
            rpc_log_path.read_text(encoding="utf-8")
            if rpc_log_path.is_file()
            else ""
        )
        self.assertTrue(
            delivered,
            "pi did not receive the SecondBrain prompt as a user turn\n" + diagnostics,
        )
        self.assertTrue(
            capability_ready,
            "the live pi extension did not advertise prompt-consumer v1\n" + diagnostics,
        )
        self.assertFalse(
            (self.session_dir / "pi_capabilities.json").exists(),
            "pi shutdown left a stale live prompt-consumer capability",
        )
        prompt_dir = _pi_prompt_dir(self.state, self.ws, self.name)
        self.assertEqual(
            list(prompt_dir.iterdir()) if prompt_dir.is_dir() else [],
            [],
            "SecondBrain prompt claim was not committed",
        )

    def test_idle_rpc_delivery_commit_and_internal_session_switch(self) -> None:
        """Exercise the live watcher and `/new` lifecycle through RPC mode."""
        self._queue("MARKER-LIVE-INBOX-DELIVERY")
        script_path = self.work / "rpc-script.json"
        script_path.write_text(json.dumps([{"text": "Inbox acknowledged."}]))
        requests_path = script_path.with_suffix(".requests.jsonl")

        server = subprocess.Popen(
            [sys.executable, str(MOCK_SERVER), "--port", str(self.port),
             "--script", str(script_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(server.kill)
        self._await_server()

        rpc_log_path = self.work / "rpc.log"
        with rpc_log_path.open("w") as rpc_log:
            rpc = subprocess.Popen(
                ["pi",
                 "-e", str(MOCK_PROVIDER),
                 "-e", str(EXTENSION),
                 "--provider", "mock", "--model", "mock-model",
                 "--no-extensions", "--no-skills", "--no-context-files",
                 "--mode", "rpc"],
                cwd=str(self.ws), env=self.env, stdin=subprocess.PIPE,
                stdout=rpc_log, stderr=subprocess.STDOUT, text=True,
            )
            self.addCleanup(rpc.kill)
            deadline = time.monotonic() + 20
            delivered = False
            while time.monotonic() < deadline:
                if rpc.poll() is not None:
                    break
                if (
                    requests_path.is_file()
                    and "MARKER-LIVE-INBOX-DELIVERY"
                    in requests_path.read_text(encoding="utf-8")
                ):
                    delivered = True
                    break
                time.sleep(0.1)
            # A pi `/new` emits session_shutdown for the old native session,
            # but the managed process remains live. It must not mark the
            # Multiverse wrapper terminally ended before the replacement's
            # session_start/idle event.
            native_id_path = self.session_dir / "runtime_session_id"
            old_native_id = native_id_path.read_text().strip()
            assert rpc.stdin is not None
            rpc.stdin.write('{"id":"switch","type":"new_session"}\n')
            rpc.stdin.flush()
            switched_live = False
            switch_deadline = time.monotonic() + 15
            while time.monotonic() < switch_deadline:
                if rpc.poll() is not None:
                    break
                if native_id_path.is_file():
                    new_native_id = native_id_path.read_text().strip()
                    try:
                        current_status = json.loads(
                            self.status_path.read_text()
                        )["status"]
                    except (OSError, ValueError, KeyError):
                        current_status = ""
                    if new_native_id and new_native_id != old_native_id:
                        if current_status == "idle":
                            switched_live = True
                            break
                time.sleep(0.1)

            rpc.terminate()
            rpc.wait(timeout=10)
            if rpc.stdin is not None:
                rpc.stdin.close()

        server.terminate()
        server.wait(timeout=10)
        diagnostics = (
            rpc_log_path.read_text(encoding="utf-8")
            if rpc_log_path.is_file()
            else ""
        )
        self.assertTrue(
            delivered,
            "the live pi watcher did not inject the inbox message\n" + diagnostics,
        )
        self.assertTrue(
            switched_live,
            "an internal pi session switch left the wrapper ended\n" + diagnostics,
        )
        inbox = _inbox_dir(self.state, self.ws, self.name)
        self.assertEqual(
            list(inbox.iterdir()) if inbox.is_dir() else [],
            [],
            "delivery left a pending or staged message behind",
        )

    def test_one_shot_runs_do_not_consume_the_inbox(self) -> None:
        # `pi -p` exits after one turn. Delivering there would consume a
        # message into a process that is about to disappear, and waking it with
        # triggerTurn starts a turn the run never waits for. The extension
        # gates delivery on a long-lived session, so a print-mode run must
        # leave the inbox exactly as it found it.
        self._queue("MARKER-SHOULD-NOT-BE-CONSUMED")
        self._run_pi([{"text": "ok"}], "hello")
        self.assertEqual(
            self._pending(), 1,
            "a one-shot pi run consumed a message meant for the interactive session"
            + self._diagnostics(),
        )


@unittest.skipIf(not VENV_PYTHON.is_file(), "plugin virtualenv missing")
class InboxBridgeTests(unittest.TestCase):
    """The claim/commit contract the extension's delivery depends on.

    This is the half that can silently lose a message, and it is reachable
    without a model: ``pi.sendMessage`` is fire-and-forget, so the extension
    stages with ``claim-inbox``, sends, and only then removes the staged files
    with ``commit-inbox``. Anything that breaks that ordering shows up here.
    """

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mv-pi-bridge-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.ws = self.work / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.name = "tester"
        self.env = {
            **os.environ,
            "MULTIVERSE_ROOT": str(self.work / "state"),
            "MULTIVERSE_SESSION_NAME": self.name,
            "MULTIVERSE_SESSION_CWD": str(self.ws),
        }

    def _mvctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(VENV_PYTHON), "-m", "src.mvctl", *args],
            cwd=str(PLUGIN_ROOT), env=self.env, capture_output=True, text=True,
            timeout=60,
        )

    def _queue(self, body: str) -> None:
        subprocess.run(
            [str(VENV_PYTHON), "-c",
             "import sys;sys.path.insert(0,'.');"
             "from src.inbox import write_message_to_inbox as w;"
             f"w('alice', {self.name!r}, {str(self.ws)!r}, {body!r})"],
            cwd=str(PLUGIN_ROOT), env=self.env, check=True,
            capture_output=True, text=True, timeout=60,
        )

    def _pending(self) -> int:
        r = self._mvctl("inbox-count")
        return int(r.stdout.strip() or 0)

    def _dir(self) -> Path:
        return _inbox_dir(self.work / "state", self.ws, self.name)

    def test_claim_returns_payload_and_leaves_nothing_pending(self) -> None:
        self._queue("MARKER-CLAIM")
        r = self._mvctl("claim-inbox")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(len(payload["claims"]), 1)
        self.assertIn("MARKER-CLAIM", payload["text"])
        # Staged, so no longer pending — but not yet gone.
        self.assertEqual(self._pending(), 0)

    def test_uncommitted_claim_survives_on_disk(self) -> None:
        # This is the property that makes the ordering safe: if pi dies or the
        # send is rejected between claim and commit, the message must still
        # exist for stale-claim recovery to return to the pending pool.
        self._queue("MARKER-ORPHAN")
        payload = json.loads(self._mvctl("claim-inbox").stdout)
        staged = [
            p for p in self._dir().iterdir() if ".json.claim-" in p.name
        ]
        self.assertEqual(len(staged), 1, "claim did not leave a staged file behind")
        self.assertIn("MARKER-ORPHAN", staged[0].read_text())
        self.assertEqual([p.name for p in staged], payload["claims"])

    def test_commit_removes_only_the_named_claims(self) -> None:
        self._queue("MARKER-A")
        claims = json.loads(self._mvctl("claim-inbox").stdout)["claims"]
        r = self._mvctl("commit-inbox", *sum((["--claim", c] for c in claims), []))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")
        self.assertEqual(
            list(self._dir().iterdir()), [], "inbox still has files after commit"
        )

    def test_commit_refuses_paths_outside_the_inbox(self) -> None:
        # The extension hands these names straight through from a JSON payload,
        # so the bridge has to treat them as untrusted input rather than as
        # something it produced itself.
        for bad in ("../../etc/passwd", "/etc/passwd", "not-a-claim.json"):
            with self.subTest(claim=bad):
                r = self._mvctl("commit-inbox", "--claim", bad)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), "0")


def _code_of(source: str) -> str:
    """Strip block comments so a doc line explaining an absence does not read
    as the presence of the thing it describes."""
    out, i = [], 0
    while i < len(source):
        start = source.find("/*", i)
        if start == -1:
            out.append(source[i:])
            break
        out.append(source[i:start])
        end = source.find("*/", start + 2)
        if end == -1:
            break
        i = end + 2
    return "".join(out)


if __name__ == "__main__":
    unittest.main()
