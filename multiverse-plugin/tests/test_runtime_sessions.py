from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from src import sessions


class RuntimeMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        sessions._CODEX_METADATA_CACHE.clear()
        sessions._CODEX_DISCOVERY_CACHE.clear()

    def test_legacy_session_metadata_defaults_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            session_dir = state_dir / "legacy"
            session_dir.mkdir()
            (session_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "cwd": "/workspace",
                        "created_at": 1,
                        "name": "legacy-agent",
                        "resumed_from": "claude-thread",
                    }
                )
            )
            (session_dir / "status.json").write_text(
                json.dumps({"status": "ended", "ts": 2})
            )

            with patch.object(sessions, "STATE_DIR", state_dir):
                loaded = sessions.load_session("legacy")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.runtime, sessions.RUNTIME_CLAUDE)
        self.assertEqual(loaded.runtime_session_id, "claude-thread")

    def test_discovers_only_named_codex_sessions_in_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            codex_home = Path(raw)
            cwd = str((codex_home / "workspace").resolve())
            other_cwd = str((codex_home / "other").resolve())
            rollouts = codex_home / "sessions" / "2026" / "07" / "13"
            rollouts.mkdir(parents=True)
            (codex_home / "session_index.jsonl").write_text(
                json.dumps({"id": "named-id", "thread_name": "codex-agent"})
                + "\n"
            )
            (rollouts / "rollout-a-named-id.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "named-id", "cwd": cwd},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "First Codex prompt",
                        },
                    }
                )
                + "\n"
            )
            (rollouts / "rollout-b-other-id.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "other-id", "cwd": other_cwd},
                    }
                )
                + "\n"
            )
            (rollouts / "rollout-c-unnamed-id.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "unnamed-id", "cwd": cwd},
                    }
                )
                + "\n"
            )

            with patch.object(sessions, "CODEX_HOME", codex_home):
                found = sessions.list_codex_sessions_in(cwd)
                all_found = sessions.list_codex_sessions_in(
                    cwd, include_unnamed=True
                )

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "named-id")
        self.assertEqual(found[0]["name"], "codex-agent")
        self.assertEqual(found[0]["preview"], "First Codex prompt")
        self.assertEqual(
            {item["id"] for item in all_found}, {"named-id", "unnamed-id"}
        )

    def test_dashboard_deduplicates_wrapped_codex_thread(self) -> None:
        wrapped = sessions.Session(
            id="mv-id",
            cwd="/workspace",
            created_at=1,
            status="ended",
            status_ts=2,
            name="codex-agent",
            runtime=sessions.RUNTIME_CODEX,
            codex_session_id="codex-thread",
        )
        dormant = [
            {
                "id": "codex-thread",
                "name": "codex-agent",
                "preview": "hello",
                "mtime": 3,
            }
        ]
        with (
            patch.object(sessions.forks, "gc"),
            patch.object(sessions.forks, "active_uuids", return_value=set()),
            patch.object(sessions, "list_codex_sessions_in", return_value=dormant),
            patch.object(sessions, "list_locked", return_value=set()),
            patch.object(sessions, "CLAUDE_PROJECTS_DIR", Path("/missing")),
        ):
            rows = sessions.build_dashboard_rows([wrapped], "/workspace")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].runtime, sessions.RUNTIME_CODEX)
        self.assertEqual(rows[0].session_id, "codex-thread")
        self.assertEqual(rows[0].preview, "hello")

    def test_all_session_names_share_one_runtime_namespace(self) -> None:
        with (
            patch.object(
                sessions, "existing_names_in_cwd", return_value={"wrapped-agent"}
            ),
            patch.object(
                sessions,
                "list_claude_sessions_in",
                return_value=[{"name": "claude-agent"}],
            ),
            patch.object(
                sessions,
                "list_codex_sessions_in",
                return_value=[{"name": "codex-agent"}],
            ),
        ):
            names = sessions.all_session_names_in_cwd("/workspace")

        self.assertEqual(
            names, {"wrapped-agent", "claude-agent", "codex-agent"}
        )


class RuntimeLivenessTests(unittest.TestCase):
    def _write_session(
        self,
        state_dir: Path,
        *,
        runtime: str = sessions.RUNTIME_CODEX,
        status: str = "idle",
        host: str = "local-host",
    ) -> Path:
        session_dir = state_dir / "wrapped"
        session_dir.mkdir()
        (session_dir / "meta.json").write_text(
            json.dumps(
                {
                    "cwd": "/workspace",
                    "created_at": 1,
                    "name": "agent",
                    "runtime": runtime,
                    "host": host,
                }
            )
        )
        (session_dir / "status.json").write_text(
            json.dumps({"status": status, "ts": 2})
        )
        return session_dir

    def test_missing_local_tmux_is_persisted_as_ended(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            session_dir = self._write_session(state_dir)
            with (
                patch.object(sessions, "STATE_DIR", state_dir),
                patch.object(sessions, "current_host", return_value="local-host"),
                patch.object(sessions, "tmux_session_exists", return_value=False),
            ):
                found = sessions.list_sessions()

            self.assertEqual(found[0].status, "ended")
            persisted = json.loads((session_dir / "status.json").read_text())
            self.assertEqual(persisted["status"], "ended")

    def test_foreign_host_status_is_not_corrected_from_local_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            session_dir = self._write_session(state_dir, host="other-host")
            with (
                patch.object(sessions, "STATE_DIR", state_dir),
                patch.object(sessions, "current_host", return_value="local-host"),
                patch.object(sessions, "tmux_session_exists") as tmux_exists,
            ):
                found = sessions.list_sessions()

            self.assertEqual(found[0].status, "idle")
            self.assertEqual(
                json.loads((session_dir / "status.json").read_text())["status"],
                "idle",
            )
            tmux_exists.assert_not_called()

    def test_dead_codex_pid_is_corrected_even_when_tmux_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            session_dir = self._write_session(state_dir)
            (session_dir / "runtime_pid").write_text("4242\n")
            with (
                patch.object(sessions, "STATE_DIR", state_dir),
                patch.object(sessions, "current_host", return_value="local-host"),
                patch.object(sessions, "tmux_session_exists", return_value=True),
                patch.object(sessions, "_pid_alive", return_value=False),
            ):
                found = sessions.list_sessions()

            self.assertEqual(found[0].status, "ended")
            self.assertEqual(
                json.loads((session_dir / "status.json").read_text())["status"],
                "ended",
            )

    def test_live_codex_pid_keeps_current_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            self._write_session(state_dir)
            (state_dir / "wrapped" / "runtime_pid").write_text("4242\n")
            with (
                patch.object(sessions, "STATE_DIR", state_dir),
                patch.object(sessions, "current_host", return_value="local-host"),
                patch.object(sessions, "tmux_session_exists", return_value=True),
                patch.object(sessions, "_pid_alive", return_value=True),
            ):
                found = sessions.list_sessions()

            self.assertEqual(found[0].status, "idle")


class RuntimeLaunchTests(unittest.TestCase):
    def _create_with_runtime(self, runtime: str, runtime_args: list[str]):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        state_dir = root / "state"
        plugin_root = root / "multiverse-plugin"
        (plugin_root / "bin").mkdir(parents=True)
        codex_plugin_root = root / "plugins" / "multiverse-codex"
        codex_plugin_root.mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append([str(part) for part in cmd])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        env = {
            "MULTIVERSE_PLUGIN_ROOT": str(plugin_root),
            "MULTIVERSE_CODEX_PLUGIN_ROOT": str(codex_plugin_root),
            "PATH": os.environ.get("PATH", ""),
        }
        with (
            patch.object(sessions, "STATE_DIR", state_dir),
            patch.object(sessions, "MULTIVERSE_DIR", root),
            patch.object(sessions, "short_id", return_value="session-id"),
            patch.object(sessions.subprocess, "run", side_effect=fake_run),
            patch.dict(os.environ, env, clear=True),
        ):
            created = sessions.create_session(
                cwd=str(root),
                claude_args=runtime_args,
                resumed_from="native-thread" if runtime_args else None,
                name="named-agent",
                runtime=runtime,
            )
        meta = json.loads((state_dir / "session-id" / "meta.json").read_text())
        return created, meta, calls[0]

    def test_codex_launch_uses_codex_runner_and_resume_command(self) -> None:
        created, meta, tmux_cmd = self._create_with_runtime(
            sessions.RUNTIME_CODEX, ["resume", "native-thread"]
        )

        self.assertEqual(created.runtime, sessions.RUNTIME_CODEX)
        self.assertEqual(meta["runtime"], sessions.RUNTIME_CODEX)
        self.assertEqual(meta["codex_args"], ["resume", "native-thread"])
        self.assertNotIn("--permission-mode", meta["codex_args"])
        self.assertIn("MULTIVERSE_RUNTIME=codex", tmux_cmd)
        self.assertIn("MULTIVERSE_RUNTIME_SESSION_ID_PATH=", " ".join(tmux_cmd))
        self.assertIn("multiverse-codex-session resume native-thread", tmux_cmd[-1])

    def test_claude_launch_keeps_existing_permission_and_runner_behavior(self) -> None:
        created, meta, tmux_cmd = self._create_with_runtime(
            sessions.RUNTIME_CLAUDE, []
        )

        self.assertEqual(created.runtime, sessions.RUNTIME_CLAUDE)
        self.assertEqual(
            meta["claude_args"], ["--permission-mode", "auto"]
        )
        self.assertIn("multiverse-session --permission-mode auto", tmux_cmd[-1])

    def test_duplicate_codex_name_and_native_thread_claims_are_rejected(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        state_dir = root / "state"
        plugin_root = root / "multiverse-plugin"
        (plugin_root / "bin").mkdir(parents=True)
        env = {
            "MULTIVERSE_PLUGIN_ROOT": str(plugin_root),
            "PATH": os.environ.get("PATH", ""),
        }
        ids = iter(("first", "same-name", "same-native", "reclaimed"))
        with (
            patch.object(sessions, "STATE_DIR", state_dir),
            patch.object(sessions, "MULTIVERSE_DIR", root),
            patch.object(sessions, "short_id", side_effect=lambda: next(ids)),
            patch.object(
                sessions.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            patch.dict(os.environ, env, clear=True),
        ):
            first = sessions.create_session(
                str(root),
                ["resume", "thread-one"],
                resumed_from="thread-one",
                name="agent-one",
                runtime=sessions.RUNTIME_CODEX,
            )
            with self.assertRaisesRegex(RuntimeError, "already claimed"):
                sessions.create_session(
                    str(root),
                    ["resume", "thread-two"],
                    resumed_from="thread-two",
                    name="agent-one",
                    runtime=sessions.RUNTIME_CODEX,
                )
            with self.assertRaisesRegex(RuntimeError, "already claimed"):
                sessions.create_session(
                    str(root),
                    ["resume", "thread-one"],
                    resumed_from="thread-one",
                    name="agent-two",
                    runtime=sessions.RUNTIME_CODEX,
                )
            sessions.kill_session(first)
            sessions.create_session(
                str(root),
                ["resume", "thread-one"],
                resumed_from="thread-one",
                name="agent-one",
                runtime=sessions.RUNTIME_CODEX,
            )

        self.assertEqual(
            sorted(path.name for path in state_dir.iterdir()), ["reclaimed"]
        )

    def test_concurrent_claim_has_exactly_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "sessions"
            (state_dir / "one").mkdir(parents=True)
            (state_dir / "two").mkdir()
            barrier = threading.Barrier(2)

            def acquire(owner: str) -> str:
                barrier.wait()
                try:
                    sessions._acquire_claim(claim_path, owner)
                except RuntimeError:
                    return "rejected"
                return "acquired"

            with patch.object(sessions, "STATE_DIR", state_dir):
                claim_path = sessions._claim_path("name", "/workspace\0agent")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(acquire, ("one", "two")))

            self.assertEqual(sorted(results), ["acquired", "rejected"])
            self.assertIn(os.readlink(claim_path), {"one", "two"})

    def test_failed_tmux_launch_releases_claims_and_state(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        state_dir = root / "state"
        plugin_root = root / "multiverse-plugin"
        (plugin_root / "bin").mkdir(parents=True)

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["tmux", "new-session"]:
                raise subprocess.CalledProcessError(1, cmd, stderr="launch failed")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        env = {
            "MULTIVERSE_PLUGIN_ROOT": str(plugin_root),
            "PATH": os.environ.get("PATH", ""),
        }
        with (
            patch.object(sessions, "STATE_DIR", state_dir),
            patch.object(sessions, "MULTIVERSE_DIR", root),
            patch.object(sessions, "short_id", return_value="failed"),
            patch.object(sessions.subprocess, "run", side_effect=fake_run),
            patch.dict(os.environ, env, clear=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                sessions.create_session(
                    str(root),
                    ["resume", "thread-one"],
                    resumed_from="thread-one",
                    name="agent-one",
                    runtime=sessions.RUNTIME_CODEX,
                )

        self.assertFalse((state_dir / "failed").exists())
        claims_dir = state_dir.parent / "session_claims"
        self.assertEqual(list(claims_dir.iterdir()), [])


class CodexDeletionTests(unittest.TestCase):
    def test_delete_codex_thread_uses_app_server_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            capture = temp_dir / "requests.jsonl"
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "cat > \"$CAPTURE_PATH\"\n"
                "printf '%s\\n' '{\"id\":2,\"result\":{}}'\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **os.environ,
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "CAPTURE_PATH": str(capture),
            }
            with patch.dict(os.environ, env, clear=True):
                sessions.delete_codex_thread("codex-thread")

            requests = [json.loads(line) for line in capture.read_text().splitlines()]
            delete = next(item for item in requests if item.get("id") == 2)
            self.assertEqual(delete["method"], "thread/delete")
            self.assertEqual(delete["params"], {"threadId": "codex-thread"})


class CodexHookTests(unittest.TestCase):
    def test_session_start_records_native_id_and_idle_status(self) -> None:
        hook = (
            Path(__file__).resolve().parents[2]
            / "plugins"
            / "multiverse-codex"
            / "scripts"
            / "session-hook.py"
        )
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            status = state / "status.json"
            native_id = state / "runtime_session_id"
            env = {
                **os.environ,
                "MULTIVERSE_STATUS": str(status),
                "MULTIVERSE_RUNTIME_SESSION_ID_PATH": str(native_id),
            }
            proc = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "codex-thread",
                        "cwd": str(state),
                    }
                ),
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(native_id.read_text().strip(), "codex-thread")
            self.assertEqual(json.loads(status.read_text())["status"], "idle")

    def test_native_name_uses_codex_thread_name_method(self) -> None:
        hook = (
            Path(__file__).resolve().parents[2]
            / "plugins"
            / "multiverse-codex"
            / "scripts"
            / "session-hook.py"
        )
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            capture = temp_dir / "requests.jsonl"
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "cat > \"$CAPTURE_PATH\"\n"
                "printf '%s\\n' '{\"id\":2,\"result\":{}}'\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **os.environ,
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "CAPTURE_PATH": str(capture),
            }

            proc = subprocess.run(
                [
                    sys.executable,
                    str(hook),
                    "--set-name",
                    "codex-thread",
                    "named-agent",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            requests = [json.loads(line) for line in capture.read_text().splitlines()]
            rename = next(item for item in requests if item.get("id") == 2)
            self.assertEqual(rename["method"], "thread/name/set")
            self.assertEqual(
                rename["params"],
                {"threadId": "codex-thread", "name": "named-agent"},
            )


class CodexRunnerTests(unittest.TestCase):
    def test_runner_injects_plugin_config_before_resume_command(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runner = repo_root / "multiverse-plugin" / "bin" / "multiverse-codex-session"
        plugin_root = repo_root / "plugins" / "multiverse-codex"
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            status = temp_dir / "status.json"
            capture = temp_dir / "args.txt"
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$CAPTURE_PATH\"\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **os.environ,
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(plugin_root),
                "MULTIVERSE_PLUGIN_ROOT": str(repo_root / "multiverse-plugin"),
                "MULTIVERSE_STATUS": str(status),
                "MULTIVERSE_ROOT": str(temp_dir / "multiverse-state"),
                "MULTIVERSE_SESSION_ID": "wrapper-id",
                "MULTIVERSE_SESSION_CWD": str(temp_dir),
                "MULTIVERSE_SESSION_NAME": "codex-agent",
                "MULTIVERSE_RUNTIME": sessions.RUNTIME_CODEX,
                "MULTIVERSE_RUNTIME_PID_PATH": str(temp_dir / "runtime_pid"),
                "MULTIVERSE_RUNTIME_SESSION_ID_PATH": str(
                    temp_dir / "runtime_session_id"
                ),
                "CAPTURE_PATH": str(capture),
            }

            proc = subprocess.run(
                [str(runner), "resume", "codex-thread"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            args = capture.read_text().splitlines()
            self.assertIn("--dangerously-bypass-hook-trust", args)
            resume_index = args.index("resume")
            self.assertGreater(resume_index, args.index("-c"))
            self.assertEqual(
                args[resume_index : resume_index + 2],
                ["resume", "codex-thread"],
            )
            self.assertIn(
                "mcp_servers.multiverse.env.MULTIVERSE_SESSION_CWD="
                + json.dumps(str(temp_dir)),
                args,
            )
            self.assertIn(
                "mcp_servers.multiverse.env.MULTIVERSE_SESSION_NAME="
                + json.dumps("codex-agent"),
                args,
            )
            self.assertIn(
                "mcp_servers.multiverse.env.MULTIVERSE_RUNTIME_PID_PATH="
                + json.dumps(str(temp_dir / "runtime_pid")),
                args,
            )
            self.assertEqual(json.loads(status.read_text())["status"], "ended")

    def test_ready_watcher_advances_unchanged_starting_status(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runner = repo_root / "multiverse-plugin" / "bin" / "multiverse-codex-session"
        plugin_root = repo_root / "plugins" / "multiverse-codex"
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            status = temp_dir / "status.json"
            native_id = temp_dir / "runtime_session_id"
            observed = temp_dir / "observed-status.txt"
            status.write_text(json.dumps({"status": "starting", "ts": 1}))
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' codex-thread > \"$MULTIVERSE_RUNTIME_SESSION_ID_PATH\"\n"
                "attempt=0\n"
                "while [ \"$attempt\" -lt 100 ]; do\n"
                "  current=$(jq -r '.status' \"$MULTIVERSE_STATUS\")\n"
                "  if [ \"$current\" = idle ]; then\n"
                "    printf '%s\\n' \"$current\" > \"$OBSERVED_STATUS\"\n"
                "    exit 0\n"
                "  fi\n"
                "  attempt=$((attempt + 1))\n"
                "  sleep 0.02\n"
                "done\n"
                "exit 1\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **os.environ,
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(plugin_root),
                "MULTIVERSE_PLUGIN_ROOT": str(repo_root / "multiverse-plugin"),
                "MULTIVERSE_STATUS": str(status),
                "MULTIVERSE_RUNTIME_SESSION_ID_PATH": str(native_id),
                "OBSERVED_STATUS": str(observed),
            }

            proc = subprocess.run(
                [str(runner)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(observed.read_text().strip(), "idle")

    def test_ready_watcher_detects_idle_resume_before_first_hook(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runner = repo_root / "multiverse-plugin" / "bin" / "multiverse-codex-session"
        plugin_root = repo_root / "plugins" / "multiverse-codex"
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            status = temp_dir / "status.json"
            observed = temp_dir / "observed-status.txt"
            status.write_text(json.dumps({"status": "starting", "ts": 1}))
            fake_tmux = temp_dir / "tmux"
            fake_tmux.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' 'gpt-5 · Multiverse · main · Ready · Context 90%'\n"
            )
            fake_tmux.chmod(0o755)
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "attempt=0\n"
                "while [ \"$attempt\" -lt 100 ]; do\n"
                "  current=$(jq -r '.status' \"$MULTIVERSE_STATUS\")\n"
                "  if [ \"$current\" = idle ]; then\n"
                "    printf '%s\\n' \"$current\" > \"$OBSERVED_STATUS\"\n"
                "    exit 0\n"
                "  fi\n"
                "  attempt=$((attempt + 1))\n"
                "  sleep 0.02\n"
                "done\n"
                "exit 1\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{temp_dir}:{os.environ.get('PATH', '')}",
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(plugin_root),
                "MULTIVERSE_PLUGIN_ROOT": str(repo_root / "multiverse-plugin"),
                "MULTIVERSE_SESSION_ID": "wrapper-id",
                "MULTIVERSE_STATUS": str(status),
                "MULTIVERSE_RUNTIME_SESSION_ID_PATH": str(
                    temp_dir / "missing-runtime-session-id"
                ),
                "OBSERVED_STATUS": str(observed),
            }

            proc = subprocess.run(
                [str(runner), "resume", "codex-thread"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(observed.read_text().strip(), "idle")

    def test_ready_watcher_does_not_overwrite_working_status(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runner = repo_root / "multiverse-plugin" / "bin" / "multiverse-codex-session"
        plugin_root = repo_root / "plugins" / "multiverse-codex"
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            status = temp_dir / "status.json"
            native_id = temp_dir / "runtime_session_id"
            observed = temp_dir / "observed-status.txt"
            status.write_text(json.dumps({"status": "working", "ts": 1}))
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' codex-thread > \"$MULTIVERSE_RUNTIME_SESSION_ID_PATH\"\n"
                "sleep 0.15\n"
                "jq -r '.status' \"$MULTIVERSE_STATUS\" > \"$OBSERVED_STATUS\"\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **os.environ,
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(plugin_root),
                "MULTIVERSE_PLUGIN_ROOT": str(repo_root / "multiverse-plugin"),
                "MULTIVERSE_STATUS": str(status),
                "MULTIVERSE_RUNTIME_SESSION_ID_PATH": str(native_id),
                "OBSERVED_STATUS": str(observed),
            }

            proc = subprocess.run(
                [str(runner)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(observed.read_text().strip(), "working")

    def test_runner_records_foreground_codex_pid_until_exit(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runner = repo_root / "multiverse-plugin" / "bin" / "multiverse-codex-session"
        plugin_root = repo_root / "plugins" / "multiverse-codex"
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            status = temp_dir / "status.json"
            runtime_pid = temp_dir / "runtime_pid"
            release = temp_dir / "release"
            status.write_text(json.dumps({"status": "starting", "ts": 1}))
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "while [ ! -f \"$RELEASE_PATH\" ]; do sleep 0.02; done\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **os.environ,
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(plugin_root),
                "MULTIVERSE_PLUGIN_ROOT": str(repo_root / "multiverse-plugin"),
                "MULTIVERSE_STATUS": str(status),
                "MULTIVERSE_RUNTIME_PID_PATH": str(runtime_pid),
                "RELEASE_PATH": str(release),
            }
            proc = subprocess.Popen(
                [str(runner)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                deadline = time.monotonic() + 3
                while not runtime_pid.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(runtime_pid.is_file())
                self.assertTrue(sessions._pid_alive(int(runtime_pid.read_text())))
                release.touch()
                _, stderr = proc.communicate(timeout=3)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

            self.assertEqual(proc.returncode, 0, stderr)
            self.assertEqual(json.loads(status.read_text())["status"], "ended")


if __name__ == "__main__":
    unittest.main()
