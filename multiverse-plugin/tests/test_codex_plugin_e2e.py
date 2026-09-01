from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from src import inbox, sessions


_FAKE_CODEX = r"""
#!__PYTHON__
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def run_app_server() -> None:
    fork_id = "ephemeral-fork-id"
    answer = {
        "id": "answer-item",
        "type": "agentMessage",
        "phase": "final_answer",
        "text": "Wrapped Codex answer",
    }
    for raw in sys.stdin:
        request = json.loads(raw)
        method = request.get("method")
        if method == "initialize":
            print(
                json.dumps(
                    {"id": request["id"], "result": {"userAgent": "fake"}}
                ),
                flush=True,
            )
        elif method == "thread/fork":
            print(
                json.dumps(
                    {
                        "id": request["id"],
                        "result": {
                            "thread": {"id": fork_id, "ephemeral": True}
                        },
                    }
                ),
                flush=True,
            )
        elif method == "turn/start":
            print(
                json.dumps(
                    {
                        "id": request["id"],
                        "result": {"turn": {"id": "turn-id"}},
                    }
                ),
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": fork_id,
                            "turnId": "turn-id",
                            "item": answer,
                        },
                    }
                ),
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": fork_id,
                            "turn": {
                                "id": "turn-id",
                                "status": "completed",
                                "items": [answer],
                            },
                        },
                    }
                ),
                flush=True,
            )


def parse_mcp_config() -> tuple[str, dict[str, str]]:
    command = ""
    child_env = os.environ.copy()
    command_prefix = "mcp_servers.multiverse.command="
    env_prefix = "mcp_servers.multiverse.env."
    for arg in sys.argv[1:]:
        if arg.startswith(command_prefix):
            command = json.loads(arg[len(command_prefix) :])
        elif arg.startswith(env_prefix):
            name, encoded = arg[len(env_prefix) :].split("=", 1)
            child_env[name] = json.loads(encoded)
    if not command:
        raise RuntimeError("wrapped Codex process received no Multiverse MCP command")
    return command, child_env


def result_text(result) -> str:
    if result.isError:
        raise RuntimeError(str(result.content))
    return "\n".join(
        block.text for block in result.content if hasattr(block, "text")
    )


async def run_wrapped_client() -> None:
    command, child_env = parse_mcp_config()
    params = StdioServerParameters(
        command=command,
        env=child_env,
        cwd=os.environ["E2E_WORKSPACE"],
    )
    results: dict[str, object] = {}
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            results["tools"] = sorted(tool.name for tool in listed.tools)
            results["list_agents"] = result_text(
                await session.call_tool("list_agents", {})
            )
            results["monitor_inbox"] = result_text(
                await session.call_tool("monitor_inbox", {})
            )
            results["check_inbox"] = result_text(
                await session.call_tool("check_inbox", {})
            )
            results["message_agent"] = result_text(
                await session.call_tool(
                    "message_agent",
                    {
                        "target_name": "codex-target",
                        "message": "Wrapped MCP delivery",
                    },
                )
            )
            results["ask_agent"] = result_text(
                await session.call_tool(
                    "ask_agent",
                    {
                        "target_name": "codex-target",
                        "question": "What did you finish?",
                    },
                )
            )
            results["spawn_agent"] = result_text(
                await session.call_tool(
                    "spawn_agent",
                    {
                        "instruction": "Run the isolated wrapped spawn task.",
                        "name": "e2e-child",
                    },
                )
            )
    Path(os.environ["E2E_RESULT_PATH"]).write_text(json.dumps(results))


if "app-server" in sys.argv:
    run_app_server()
else:
    asyncio.run(run_wrapped_client())
"""


class WrappedCodexPluginEndToEndTests(unittest.TestCase):
    def test_wrapped_codex_loads_plugin_and_calls_all_six_tools(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        wrapper_root = repo_root / "multiverse-plugin"
        codex_plugin_root = repo_root / "plugins" / "multiverse-codex"
        runner = wrapper_root / "bin" / "multiverse-codex-session"

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_root = root / "state"
            state_root.mkdir()
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            codex_home = root / "codex-home"
            rollout_dir = codex_home / "sessions" / "2026" / "07" / "18"
            rollout_dir.mkdir(parents=True)
            (codex_home / "session_index.jsonl").write_text(
                json.dumps(
                    {"id": "target-thread", "thread_name": "codex-target"}
                )
                + "\n"
            )
            (rollout_dir / "rollout-target-thread.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "target-thread",
                            "cwd": str(workspace),
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "Target context",
                        },
                    }
                )
                + "\n"
            )

            caller_dir = state_root / "sessions" / "caller-wrapper"
            caller_dir.mkdir(parents=True)
            (caller_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "cwd": str(workspace),
                        "created_at": 1,
                        "runtime": sessions.RUNTIME_CODEX,
                        "host": sessions.current_host(),
                        "name": "codex-caller",
                        "codex_args": [],
                    }
                )
            )
            (caller_dir / "status.json").write_text(
                json.dumps({"status": "idle", "ts": time.time()})
            )
            (caller_dir / "runtime_session_id").write_text("caller-thread\n")

            caller_inbox = (
                state_root
                / "inbox"
                / inbox._encode_cwd(str(workspace))
                / "codex-caller"
            )
            caller_inbox.mkdir(parents=True)
            (caller_inbox / "incoming.json").write_text(
                json.dumps(
                    {
                        "message_id": "incoming",
                        "from": "codex-target",
                        "body": "Wrapped inbox delivery",
                        "ts": 1,
                    }
                )
            )

            fake_codex = root / "codex"
            fake_codex.write_text(
                textwrap.dedent(_FAKE_CODEX)
                .lstrip()
                .replace("__PYTHON__", sys.executable)
            )
            fake_codex.chmod(0o755)
            tmux_log = root / "tmux.log"
            fake_tmux = root / "tmux"
            fake_tmux.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$TMUX_E2E_LOG\"\n"
                "exit 0\n"
            )
            fake_tmux.chmod(0o755)
            result_path = root / "results.json"

            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ.get('PATH', '')}",
                "CODEX_HOME": str(codex_home),
                "MULTIVERSE_ROOT": str(state_root),
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(codex_plugin_root),
                "MULTIVERSE_PLUGIN_ROOT": str(wrapper_root),
                "MULTIVERSE_RUNTIME": sessions.RUNTIME_CODEX,
                "MULTIVERSE_SESSION_ID": "caller-wrapper",
                "MULTIVERSE_SESSION_CWD": str(workspace),
                "MULTIVERSE_SESSION_NAME": "codex-caller",
                "MULTIVERSE_STATUS": str(caller_dir / "status.json"),
                "MULTIVERSE_RUNTIME_PID_PATH": str(
                    caller_dir / "runtime_pid"
                ),
                "MULTIVERSE_RUNTIME_SESSION_ID_PATH": str(
                    caller_dir / "runtime_session_id"
                ),
                "E2E_WORKSPACE": str(workspace),
                "E2E_RESULT_PATH": str(result_path),
                "TMUX_E2E_LOG": str(tmux_log),
                "UV_CACHE_DIR": "/tmp/multiverse-uv-cache",
            }

            approval: list[dict] = []
            approval_errors: list[str] = []

            def approve_spawn() -> None:
                request_dir = state_root / "spawn_requests"
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if request_dir.is_dir():
                        for path in request_dir.glob("*.json"):
                            if path.name.endswith(".resolved.json"):
                                continue
                            try:
                                request = json.loads(path.read_text())
                            except (OSError, json.JSONDecodeError):
                                continue
                            approval.append(request)
                            resolution = request_dir / (
                                f"{request['id']}.resolved.json"
                            )
                            resolution.write_text(
                                json.dumps(
                                    {
                                        "status": "accepted",
                                        "name": "e2e-child",
                                        "runtime": sessions.RUNTIME_CODEX,
                                    }
                                )
                            )
                            return
                    time.sleep(0.02)
                approval_errors.append("spawn request did not appear")

            approver = threading.Thread(target=approve_spawn, daemon=True)
            approver.start()
            completed = subprocess.run(
                [str(runner)],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            approver.join(timeout=2)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(approval_errors, [])
            self.assertEqual(len(approval), 1)
            self.assertTrue(result_path.is_file(), completed.stderr)
            results = json.loads(result_path.read_text())
            self.assertEqual(
                results["tools"],
                [
                    "ask_agent",
                    "check_inbox",
                    "list_agents",
                    "message_agent",
                    "monitor_inbox",
                    "spawn_agent",
                ],
            )
            self.assertIn("codex-caller", results["list_agents"])
            self.assertIn("codex-target", results["list_agents"])
            self.assertIn("already have pending", results["monitor_inbox"])
            self.assertIn("Wrapped inbox delivery", results["check_inbox"])
            # Delivery is always active now: the dormant target is woken.
            self.assertIn(
                "prompted Codex to call check_inbox on startup",
                results["message_agent"],
            )
            self.assertEqual(results["ask_agent"], "Wrapped Codex answer")
            self.assertEqual(
                results["spawn_agent"],
                "Spawned agent with name 'e2e-child'.",
            )
            self.assertEqual(list(caller_inbox.glob("*.json")), [])

            target_inbox = (
                state_root
                / "inbox"
                / inbox._encode_cwd(str(workspace))
                / "codex-target"
            )
            messages = list(target_inbox.glob("*.json"))
            self.assertEqual(len(messages), 1)
            self.assertEqual(
                json.loads(messages[0].read_text())["body"],
                "Wrapped MCP delivery",
            )

            child_dirs = []
            for session_dir in (state_root / "sessions").iterdir():
                meta_path = session_dir / "meta.json"
                if not meta_path.is_file():
                    continue
                meta = json.loads(meta_path.read_text())
                if meta.get("name") == "e2e-child":
                    child_dirs.append((session_dir, meta))
            self.assertEqual(len(child_dirs), 1)
            child_dir, child_meta = child_dirs[0]
            self.assertEqual(child_meta["runtime"], sessions.RUNTIME_CODEX)
            self.assertEqual(
                (child_dir / "initial_prompt.txt").read_text(),
                "Run the isolated wrapped spawn task.",
            )
            self.assertEqual(
                list((state_root / "spawn_requests").glob("*.json")), []
            )
            self.assertIn("new-session", tmux_log.read_text())


if __name__ == "__main__":
    unittest.main()
