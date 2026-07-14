from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
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

            with patch.object(sessions, "CODEX_HOME", codex_home):
                found = sessions.list_codex_sessions_in(cwd)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "named-id")
        self.assertEqual(found[0]["name"], "codex-agent")
        self.assertEqual(found[0]["preview"], "First Codex prompt")

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
            self.assertEqual(json.loads(status.read_text())["status"], "ended")


if __name__ == "__main__":
    unittest.main()
