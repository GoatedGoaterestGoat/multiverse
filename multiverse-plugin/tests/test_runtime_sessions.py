from __future__ import annotations

import json
import os
import signal
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


def _isolated_process_env() -> dict[str, str]:
    """Keep subprocess tests from inheriting a live Multiverse identity."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MULTIVERSE_")
    }


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

    def test_resumed_codex_rollout_keeps_its_first_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            rollout = Path(raw) / "rollout.jsonl"
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": "current-thread",
                                    "cwd": "/workspace",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": "ancestor-thread",
                                    "cwd": "/workspace",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "user_message",
                                    "message": "Original prompt",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            metadata = sessions._read_codex_session_metadata(rollout)

        self.assertEqual(metadata["id"], "current-thread")
        self.assertEqual(metadata["preview"], "Original prompt")
        self.assertEqual(metadata["ancestor_ids"], ["ancestor-thread"])

    def test_codex_preview_reads_current_response_item_user_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            rollout = Path(raw) / "rollout.jsonl"
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": "current-thread",
                                    "cwd": "/workspace",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": (
                                                "<environment_context>hidden"
                                                "</environment_context>"
                                            ),
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "Do the spawned work.\nStart here.",
                                        }
                                    ],
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            metadata = sessions._read_codex_session_metadata(rollout)

        self.assertEqual(metadata["preview"], "Do the spawned work. Start here.")

    def test_codex_discovery_hides_named_resume_ancestor(self) -> None:
        resumed = {
            "id": "current-thread",
            "name": "codex-agent",
            "preview": "Original prompt",
            "mtime": 2,
            "ancestor_ids": ["ancestor-thread"],
        }
        ancestor = {
            "id": "ancestor-thread",
            "name": "codex-agent",
            "preview": "Original prompt",
            "mtime": 1,
            "ancestor_ids": [],
        }
        with patch.object(
            sessions,
            "_CODEX_DISCOVERY_CACHE",
            {"/workspace": (time.monotonic(), [resumed, ancestor])},
        ):
            visible = sessions.list_codex_sessions_in("/workspace")
            full_lineage = sessions.list_codex_sessions_in(
                "/workspace", include_ancestors=True
            )

        self.assertEqual([item["id"] for item in visible], ["current-thread"])
        self.assertEqual(
            [item["id"] for item in full_lineage],
            ["current-thread", "ancestor-thread"],
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

    def test_dashboard_deduplicates_resumed_codex_ancestor_and_uses_preview(
        self,
    ) -> None:
        wrapped = sessions.Session(
            id="mv-id",
            cwd="/workspace",
            created_at=1,
            status="idle",
            status_ts=2,
            name="codex-agent",
            runtime=sessions.RUNTIME_CODEX,
            codex_session_id="current-thread",
            resumed_from="current-thread",
        )
        native = [
            {
                "id": "current-thread",
                "name": "codex-agent",
                "preview": "(no user message)",
                "mtime": 4,
                "ancestor_ids": ["ancestor-thread"],
            },
            {
                "id": "ancestor-thread",
                "name": "codex-agent",
                "preview": "Original prompt",
                "mtime": 3,
            },
        ]
        with (
            patch.object(sessions.forks, "gc"),
            patch.object(sessions.forks, "active_uuids", return_value=set()),
            patch.object(sessions, "list_codex_sessions_in", return_value=native),
            patch.object(sessions, "list_locked", return_value=set()),
            patch.object(sessions, "CLAUDE_PROJECTS_DIR", Path("/missing")),
        ):
            rows = sessions.build_dashboard_rows([wrapped], "/workspace")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, "current-thread")
        self.assertEqual(rows[0].preview, "Original prompt")

    def test_dashboard_hides_dormant_codex_resume_ancestor(self) -> None:
        native = [
            {
                "id": "current-thread",
                "name": "codex-agent",
                "preview": "Original prompt",
                "mtime": 2,
                "ancestor_ids": ["ancestor-thread"],
            },
            {
                "id": "ancestor-thread",
                "name": "codex-agent",
                "preview": "Original prompt",
                "mtime": 1,
                "ancestor_ids": [],
            },
        ]
        with (
            patch.object(sessions.forks, "gc"),
            patch.object(sessions.forks, "active_uuids", return_value=set()),
            patch.object(sessions, "list_codex_sessions_in", return_value=native),
            patch.object(sessions, "list_locked", return_value=set()),
            patch.object(sessions, "CLAUDE_PROJECTS_DIR", Path("/missing")),
        ):
            rows = sessions.build_dashboard_rows([], "/workspace")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, "current-thread")

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
                patch.object(
                    sessions, "_codex_runtime_pid_matches", return_value=True
                ),
            ):
                found = sessions.list_sessions()

            self.assertEqual(found[0].status, "idle")

    def test_live_codex_pid_prevents_detach_from_marking_ended(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            session_dir = self._write_session(state_dir)
            (session_dir / "runtime_pid").write_text("4242\n")
            with (
                patch.object(sessions, "STATE_DIR", state_dir),
                patch.object(sessions, "current_host", return_value="local-host"),
                patch.object(sessions, "tmux_session_exists", return_value=False),
                patch.object(
                    sessions, "_codex_runtime_pid_matches", return_value=True
                ),
            ):
                found = sessions.list_sessions()

            self.assertEqual(found[0].status, "idle")
            self.assertEqual(
                json.loads((session_dir / "status.json").read_text())["status"],
                "idle",
            )

    def test_codex_pid_identity_requires_embedded_wrapper_id(self) -> None:
        wrapped = sessions.Session(
            id="wrapper-id",
            cwd="/workspace",
            created_at=1,
            runtime=sessions.RUNTIME_CODEX,
            host="local-host",
        )
        with (
            patch.object(sessions, "current_host", return_value="local-host"),
            patch.object(sessions, "_pid_alive", return_value=True),
            patch.object(
                sessions,
                "_proc_cmdline",
                return_value=(
                    "codex -c mcp_servers.multiverse.env."
                    'MULTIVERSE_SESSION_ID="wrapper-id"'
                ),
            ),
        ):
            self.assertTrue(sessions._codex_runtime_pid_matches(wrapped, 4242))

        with (
            patch.object(sessions, "current_host", return_value="local-host"),
            patch.object(sessions, "_pid_alive", return_value=True),
            patch.object(
                sessions,
                "_proc_cmdline",
                return_value="codex resume unrelated-thread",
            ),
        ):
            self.assertFalse(sessions._codex_runtime_pid_matches(wrapped, 4242))

    def test_live_codex_session_reloads_after_monitor_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            session_dir = self._write_session(state_dir)
            (session_dir / "runtime_pid").write_text("4242\n")
            (session_dir / "runtime_session_id").write_text("codex-thread\n")
            with (
                patch.object(sessions, "STATE_DIR", state_dir),
                patch.object(sessions, "current_host", return_value="local-host"),
                patch.object(sessions, "tmux_session_exists", return_value=True),
                patch.object(
                    sessions, "_codex_runtime_pid_matches", return_value=True
                ),
            ):
                before_restart = sessions.list_sessions()
                after_restart = sessions.list_sessions()

        self.assertEqual(len(before_restart), 1)
        self.assertEqual(len(after_restart), 1)
        self.assertIsNot(before_restart[0], after_restart[0])
        self.assertEqual(after_restart[0].name, "agent")
        self.assertEqual(after_restart[0].runtime, sessions.RUNTIME_CODEX)
        self.assertEqual(after_restart[0].runtime_session_id, "codex-thread")
        self.assertEqual(after_restart[0].status, "idle")


class RuntimeLaunchTests(unittest.TestCase):
    def _create_with_runtime(
        self,
        runtime: str,
        runtime_args: list[str],
        initial_prompt: str | None = None,
    ):
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
                initial_prompt=initial_prompt,
            )
        meta = json.loads((state_dir / "session-id" / "meta.json").read_text())
        return created, meta, calls

    def test_codex_launch_uses_codex_runner_and_resume_command(self) -> None:
        created, meta, calls = self._create_with_runtime(
            sessions.RUNTIME_CODEX, ["resume", "native-thread"]
        )
        tmux_cmd = calls[0]

        self.assertEqual(created.runtime, sessions.RUNTIME_CODEX)
        self.assertEqual(meta["runtime"], sessions.RUNTIME_CODEX)
        self.assertEqual(meta["codex_args"], ["resume", "native-thread"])
        self.assertNotIn("--permission-mode", meta["codex_args"])
        self.assertIn("MULTIVERSE_RUNTIME=codex", tmux_cmd)
        self.assertIn("MULTIVERSE_RUNTIME_SESSION_ID_PATH=", " ".join(tmux_cmd))
        self.assertIn(
            "multiverse-codex-session resume native-thread", " ".join(tmux_cmd)
        )
        persistence_args = [
            ";",
            "set-option",
            "-t",
            "multiverse-session-id",
            "destroy-unattached",
            "off",
        ]
        persistence_index = tmux_cmd.index(";")
        self.assertEqual(
            tmux_cmd[
                persistence_index : persistence_index + len(persistence_args)
            ],
            persistence_args,
        )

    def test_codex_launch_keeps_name_and_initial_prompt_off_command_line(
        self,
    ) -> None:
        prompt = "First line with apostrophe's.\nSecond line."
        _, _, calls = self._create_with_runtime(
            sessions.RUNTIME_CODEX,
            [],
            initial_prompt=prompt,
        )
        tmux_cmd = calls[0]

        self.assertIn("MULTIVERSE_SESSION_NAME=named-agent", tmux_cmd)
        prompt_env = next(
            part
            for part in tmux_cmd
            if part.startswith("MULTIVERSE_INITIAL_PROMPT_FILE=")
        )
        prompt_path = Path(prompt_env.split("=", 1)[1])
        self.assertEqual(prompt_path.read_text(), prompt)
        self.assertNotIn(prompt, " ".join(tmux_cmd))
        self.assertIn("multiverse-codex-session", " ".join(tmux_cmd))

    def test_claude_launch_keeps_existing_permission_and_runner_behavior(self) -> None:
        created, meta, calls = self._create_with_runtime(
            sessions.RUNTIME_CLAUDE, []
        )
        tmux_cmd = calls[0]

        self.assertEqual(created.runtime, sessions.RUNTIME_CLAUDE)
        self.assertEqual(
            meta["claude_args"], ["--permission-mode", "auto"]
        )
        self.assertIn(
            "multiverse-session --permission-mode auto", " ".join(tmux_cmd)
        )
        self.assertNotIn("--sandbox", " ".join(tmux_cmd))
        self.assertNotIn("danger-full-access", " ".join(tmux_cmd))

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


class RuntimeKillTests(unittest.TestCase):
    def _session(
        self,
        state_dir: Path,
        session_id: str,
        runtime: str = sessions.RUNTIME_CODEX,
    ) -> sessions.Session:
        session_dir = state_dir / session_id
        session_dir.mkdir()
        (session_dir / "meta.json").write_text(
            json.dumps(
                {
                    "cwd": str(state_dir),
                    "created_at": 1,
                    "name": "owned-agent",
                    "runtime": runtime,
                    "host": sessions.current_host(),
                }
            )
        )
        (session_dir / "status.json").write_text(
            json.dumps({"status": "idle", "ts": 2})
        )
        (session_dir / "runtime_session_id").write_text("native-thread\n")
        return sessions.Session(
            id=session_id,
            cwd=str(state_dir),
            created_at=1,
            status="idle",
            name="owned-agent",
            runtime=runtime,
            codex_session_id="native-thread",
            host=sessions.current_host(),
        )

    def test_live_runtime_is_terminated_before_tmux_for_both_runtimes(
        self,
    ) -> None:
        for runtime in (sessions.RUNTIME_CLAUDE, sessions.RUNTIME_CODEX):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as raw:
                state_dir = Path(raw) / "sessions"
                state_dir.mkdir()
                wrapped = self._session(state_dir, f"{runtime}-wrapper", runtime)
                calls: list[str] = []

                def terminate(*_args):
                    calls.append("runtime")

                def run(cmd, **_kwargs):
                    self.assertEqual(
                        cmd,
                        ["tmux", "kill-session", "-t", wrapped.tmux_name],
                    )
                    calls.append("tmux")
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                with (
                    patch.object(sessions, "STATE_DIR", state_dir),
                    patch.object(
                        sessions, "tmux_session_exists", return_value=True
                    ),
                    patch.object(
                        sessions,
                        "_tmux_pane_runtime_group",
                        return_value=(1234, 1234),
                    ),
                    patch.object(
                        sessions,
                        "_terminate_tmux_pane_runtime",
                        side_effect=terminate,
                    ),
                    patch.object(sessions.subprocess, "run", side_effect=run),
                ):
                    sessions.kill_session(wrapped)

                self.assertEqual(calls, ["runtime", "tmux"])
                self.assertFalse((state_dir / wrapped.id).exists())

    def test_live_pane_group_is_derived_from_tmux_owned_pid(self) -> None:
        wrapped = sessions.Session(
            id="owned-pane",
            cwd="/workspace",
            created_at=1,
            runtime=sessions.RUNTIME_CLAUDE,
        )
        display = subprocess.CompletedProcess([], 0, "4321\n", "")
        with (
            patch.object(sessions.subprocess, "run", return_value=display) as run,
            patch.object(sessions.os, "getpgid", return_value=8765),
            patch.object(sessions.os, "getpgrp", return_value=9999),
        ):
            self.assertEqual(
                sessions._tmux_pane_runtime_group(wrapped), (4321, 8765)
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                wrapped.tmux_name,
                "#{pane_pid}",
            ],
        )

    def test_kill_terminates_owned_process_group_before_releasing_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "sessions"
            state_dir.mkdir()
            wrapped = self._session(state_dir, "owned-wrapper")
            child_pid_path = Path(raw) / "child-pid"
            runtime_script = Path(raw) / "owned-runtime.py"
            runtime_script.write_text(
                "import signal, subprocess, sys, time\n"
                "child = subprocess.Popen(['sleep', '60'])\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\n"
                "def stop(_signal, _frame):\n"
                "    child.wait(timeout=2)\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "while True:\n"
                "    time.sleep(1)\n"
            )
            runtime = subprocess.Popen(
                [
                    sys.executable,
                    str(runtime_script),
                    str(child_pid_path),
                    'MULTIVERSE_SESSION_ID="owned-wrapper"',
                ],
                start_new_session=True,
            )
            try:
                session_dir = state_dir / wrapped.id
                deadline = time.monotonic() + 3
                while not child_pid_path.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(child_pid_path.is_file())
                child_pid = int(child_pid_path.read_text())
                (session_dir / "runtime_pid").write_text(f"{runtime.pid}\n")

                with (
                    patch.object(sessions, "STATE_DIR", state_dir),
                    patch.object(
                        sessions, "tmux_session_exists", return_value=False
                    ),
                ):
                    for kind, value in sessions._session_claim_specs(
                        wrapped.cwd,
                        wrapped.runtime,
                        wrapped.name,
                        wrapped.runtime_session_id,
                    ):
                        sessions._acquire_claim(
                            sessions._claim_path(kind, value), wrapped.id
                        )
                    sessions.kill_session(wrapped)

                runtime.wait(timeout=3)
                deadline = time.monotonic() + 2
                while sessions._pid_alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(sessions._pid_alive(runtime.pid))
                self.assertFalse(sessions._pid_alive(child_pid))
                self.assertFalse(session_dir.exists())
                claims_dir = state_dir.parent / "session_claims"
                self.assertEqual(list(claims_dir.iterdir()), [])
            finally:
                if runtime.poll() is None:
                    os.killpg(os.getpgid(runtime.pid), signal.SIGKILL)
                    runtime.wait()

    def test_kill_does_not_signal_reused_runtime_pid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "sessions"
            state_dir.mkdir()
            wrapped = self._session(state_dir, "stale-wrapper")
            unrelated = subprocess.Popen(["sleep", "60"], start_new_session=True)
            try:
                session_dir = state_dir / wrapped.id
                (session_dir / "runtime_pid").write_text(f"{unrelated.pid}\n")
                with (
                    patch.object(sessions, "STATE_DIR", state_dir),
                    patch.object(
                        sessions, "tmux_session_exists", return_value=False
                    ),
                ):
                    sessions.kill_session(wrapped)

                self.assertIsNone(unrelated.poll())
                self.assertFalse(session_dir.exists())
            finally:
                if unrelated.poll() is None:
                    os.killpg(os.getpgid(unrelated.pid), signal.SIGKILL)
                    unrelated.wait()


class CodexDeletionTests(unittest.TestCase):
    def test_delete_codex_thread_uses_app_server_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            capture = temp_dir / "requests.jsonl"
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "while IFS= read -r line; do\n"
                "  printf '%s\\n' \"$line\" >> \"$CAPTURE_PATH\"\n"
                "  id=$(printf '%s' \"$line\" | jq -r '.id // empty')\n"
                "  if [ \"$id\" = 1 ]; then\n"
                "    printf '%s\\n' '{\"id\":1,\"result\":{}}'\n"
                "  elif [ \"$id\" = 2 ]; then\n"
                "    printf '%s\\n' '{\"id\":2,\"result\":{}}'\n"
                "  fi\n"
                "done\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **_isolated_process_env(),
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
                **_isolated_process_env(),
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

class CodexRunnerTests(unittest.TestCase):
    def test_runner_names_spawned_thread_after_session_start(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runner = (
            repo_root
            / "multiverse-plugin"
            / "bin"
            / "multiverse-codex-session"
        )
        plugin_root = repo_root / "plugins" / "multiverse-codex"
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            status = temp_dir / "status.json"
            native_id = temp_dir / "runtime_session_id"
            codex_args_capture = temp_dir / "codex-args.txt"
            startup_capture = temp_dir / "startup.json"
            status.write_text(json.dumps({"status": "starting", "ts": 1}))
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$@\" > \"$CODEX_ARGS_CAPTURE\"\n"
                "printf '%s\\n' spawned-thread > \"$MULTIVERSE_RUNTIME_SESSION_ID_PATH\"\n"
                "attempt=0\n"
                "while [ ! -s \"$STARTUP_CAPTURE\" ] && [ \"$attempt\" -lt 100 ]; do\n"
                "  attempt=$((attempt + 1))\n"
                "  sleep 0.02\n"
                "done\n"
                "[ -s \"$STARTUP_CAPTURE\" ]\n"
            )
            fake_codex.chmod(0o755)
            fake_startup_python = temp_dir / "startup-python"
            fake_startup_python.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "def value(flag):\n"
                "    return args[args.index(flag) + 1]\n"
                "prompt_path = pathlib.Path(value('--prompt-file'))\n"
                "payload = {\n"
                "    'module': value('-m'),\n"
                "    'name': value('--name'),\n"
                "    'tmux_name': value('--tmux-name'),\n"
                "    'prompt': prompt_path.read_text(),\n"
                "}\n"
                "pathlib.Path(os.environ['STARTUP_CAPTURE']).write_text(json.dumps(payload))\n"
                "prompt_path.unlink()\n"
            )
            fake_startup_python.chmod(0o755)
            prompt_file = temp_dir / "initial-prompt.txt"
            prompt_file.write_text("Do the spawned work.")
            env = {
                **_isolated_process_env(),
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(plugin_root),
                "MULTIVERSE_PLUGIN_ROOT": str(repo_root / "multiverse-plugin"),
                "MULTIVERSE_STATUS": str(status),
                "MULTIVERSE_SESSION_ID": "wrapper-id",
                "MULTIVERSE_SESSION_NAME": "named-child",
                "MULTIVERSE_RUNTIME_SESSION_ID_PATH": str(native_id),
                "MULTIVERSE_RUNTIME_PID_PATH": str(temp_dir / "runtime-pid"),
                "MULTIVERSE_INITIAL_PROMPT_FILE": str(prompt_file),
                "MULTIVERSE_CODEX_STARTUP_PYTHON": str(fake_startup_python),
                "CODEX_HOME": str(temp_dir / "codex-home"),
                "CODEX_ARGS_CAPTURE": str(codex_args_capture),
                "STARTUP_CAPTURE": str(startup_capture),
            }

            proc = subprocess.run(
                [str(runner)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            startup = json.loads(startup_capture.read_text())
            self.assertEqual(startup["module"], "src.codex_startup_input")
            self.assertEqual(startup["name"], "named-child")
            self.assertEqual(startup["tmux_name"], "multiverse-wrapper-id")
            self.assertEqual(startup["prompt"], "Do the spawned work.")
            self.assertNotIn("Do the spawned work.", codex_args_capture.read_text())

    def test_runner_injects_plugin_config_before_resume_command(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runner = repo_root / "multiverse-plugin" / "bin" / "multiverse-codex-session"
        plugin_root = repo_root / "plugins" / "multiverse-codex"
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            status = temp_dir / "status.json"
            capture = temp_dir / "args.txt"
            stdin_capture = temp_dir / "stdin.txt"
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$CAPTURE_PATH\"\n"
                "IFS= read -r line\n"
                "printf '%s\\n' \"$line\" > \"$STDIN_CAPTURE_PATH\"\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **_isolated_process_env(),
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
                "STDIN_CAPTURE_PATH": str(stdin_capture),
            }

            proc = subprocess.run(
                [str(runner), "resume", "codex-thread"],
                input="typed through wrapper\n",
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            args = capture.read_text().splitlines()
            self.assertIn("--dangerously-bypass-hook-trust", args)
            self.assertIn("--approve-for-me", args)
            self.assertNotIn("--sandbox", args)
            self.assertNotIn("danger-full-access", args)
            resume_index = args.index("resume")
            self.assertGreater(resume_index, args.index("-c"))
            self.assertLess(args.index("--approve-for-me"), resume_index)
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
            self.assertEqual(stdin_capture.read_text().strip(), "typed through wrapper")
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
                **_isolated_process_env(),
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
                **_isolated_process_env(),
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
                **_isolated_process_env(),
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
                **_isolated_process_env(),
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

    def test_runner_forwards_hup_and_reaps_codex_child(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runner = repo_root / "multiverse-plugin" / "bin" / "multiverse-codex-session"
        plugin_root = repo_root / "plugins" / "multiverse-codex"
        with tempfile.TemporaryDirectory() as raw:
            temp_dir = Path(raw)
            status = temp_dir / "status.json"
            runtime_pid = temp_dir / "runtime_pid"
            ready = temp_dir / "ready"
            received_hup = temp_dir / "received-hup"
            status.write_text(json.dumps({"status": "starting", "ts": 1}))
            fake_codex = temp_dir / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import os, signal, time\n"
                "def stop(_signal, _frame):\n"
                "    open(os.environ['RECEIVED_HUP'], 'w').close()\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGHUP, stop)\n"
                "open(os.environ['READY_PATH'], 'w').close()\n"
                "while True:\n"
                "    time.sleep(1)\n"
            )
            fake_codex.chmod(0o755)
            env = {
                **_isolated_process_env(),
                "MULTIVERSE_CODEX_BIN": str(fake_codex),
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(plugin_root),
                "MULTIVERSE_PLUGIN_ROOT": str(repo_root / "multiverse-plugin"),
                "MULTIVERSE_STATUS": str(status),
                "MULTIVERSE_RUNTIME_PID_PATH": str(runtime_pid),
                "READY_PATH": str(ready),
                "RECEIVED_HUP": str(received_hup),
            }
            proc = subprocess.Popen(
                [str(runner)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            child_pid = 0
            try:
                deadline = time.monotonic() + 3
                while (
                    (not ready.is_file() or not runtime_pid.is_file())
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertTrue(ready.is_file())
                child_pid = int(runtime_pid.read_text())
                proc.send_signal(signal.SIGHUP)
                _, stderr = proc.communicate(timeout=4)
            finally:
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait()

            self.assertEqual(proc.returncode, 129, stderr)
            self.assertTrue(received_hup.is_file())
            self.assertFalse(sessions._pid_alive(child_pid))
            self.assertEqual(json.loads(status.read_text())["status"], "ended")


if __name__ == "__main__":
    unittest.main()
