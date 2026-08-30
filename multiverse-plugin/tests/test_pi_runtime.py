from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import ask_agents, pi_transcript, sb_notify, server, sessions


class PiTranscriptTests(unittest.TestCase):
    def test_flat_session_dir_is_scoped_by_header_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wanted = root / "wanted"
            other = root / "other"
            wanted.mkdir()
            other.mkdir()
            session_dir = root / "sessions"
            session_dir.mkdir()
            (session_dir / "2026-01-01_pid-1.jsonl").write_text(
                json.dumps({"type": "session", "id": "pid-1", "cwd": str(wanted)})
                + "\n"
                + json.dumps({"type": "session_info", "name": "pi-agent"})
                + "\n"
                + json.dumps(
                    {
                        "type": "message",
                        "message": {"role": "user", "content": "Implement pi support"},
                    }
                )
                + "\n"
            )
            (session_dir / "2026-01-01_pid-2.jsonl").write_text(
                json.dumps({"type": "session", "id": "pid-2", "cwd": str(other)})
                + "\n"
                + json.dumps({"type": "session_info", "name": "other-agent"})
                + "\n"
            )
            with patch.dict(
                os.environ,
                {"PI_CODING_AGENT_SESSION_DIR": str(session_dir)},
                clear=False,
            ):
                found = pi_transcript.list_pi_sessions_in(str(wanted))
                resolved = pi_transcript.pi_session_path("pid-1", str(wanted))

            self.assertEqual([(item["id"], item["name"]) for item in found], [("pid-1", "pi-agent")])
            self.assertEqual(found[0]["preview"], "Implement pi support")
            self.assertEqual(resolved, session_dir / "2026-01-01_pid-1.jsonl")

    def test_dashboard_deduplicates_wrapped_pi_session(self) -> None:
        cwd = "/tmp/pi-workspace"
        wrapped = sessions.Session(
            id="mv-pi",
            cwd=cwd,
            created_at=1,
            status="idle",
            status_ts=2,
            name="pi-agent",
            runtime=sessions.RUNTIME_PI,
            pi_session_id="pi-native",
        )
        native = {
            "id": "pi-native",
            "name": "pi-agent",
            "cwd": cwd,
            "preview": "hello",
            "mtime": 1,
        }
        with (
            patch.object(sessions, "list_pi_sessions_in", return_value=[native]),
            patch.object(sessions, "list_codex_sessions_in", return_value=[]),
            patch.object(sessions, "CLAUDE_PROJECTS_DIR", Path("/nonexistent")),
            patch.object(sessions.forks, "gc"),
            patch.object(sessions.forks, "active_uuids", return_value=set()),
            patch.object(sessions, "list_locked", return_value=set()),
        ):
            rows = sessions.build_dashboard_rows([wrapped], cwd)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].runtime, sessions.RUNTIME_PI)
        self.assertEqual(rows[0].session_id, "pi-native")
        self.assertEqual(rows[0].preview, "hello")


class PiLaunchTests(unittest.TestCase):
    def test_loads_legacy_harness_metadata_and_native_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "sessions"
            session_dir = state / "legacy-pi"
            session_dir.mkdir(parents=True)
            (session_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "cwd": "/tmp/work",
                        "created_at": 1,
                        "name": "legacy",
                        "harness": "pi",
                    }
                )
            )
            (session_dir / "claude_uuid").write_text("native-pi\n")
            (session_dir / "status.json").write_text(
                json.dumps({"status": "ended", "ts": 2})
            )
            with patch.object(sessions, "STATE_DIR", state):
                loaded = sessions.load_session("legacy-pi")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.runtime, sessions.RUNTIME_PI)
        self.assertEqual(loaded.runtime_session_id, "native-pi")
        self.assertIsNone(loaded.claude_uuid)

    def test_create_session_uses_pi_runner_and_native_id_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / "state" / "sessions"
            plugin = root / "plugin"
            (plugin / "bin").mkdir(parents=True)
            calls: list[list[str]] = []

            def run(cmd, **kwargs):
                calls.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with (
                patch.dict(
                    os.environ,
                    {
                        "MULTIVERSE_PLUGIN_ROOT": str(plugin),
                        "MULTIVERSE_PI_BIN": "custom-pi",
                        "PI_CODING_AGENT_SESSION_DIR": str(root / "pi-sessions"),
                    },
                    clear=False,
                ),
                patch.object(sessions, "STATE_DIR", state),
                patch.object(sessions, "MULTIVERSE_DIR", state.parent),
                patch.object(sessions, "short_id", return_value="piwrap"),
                patch.object(sessions, "current_host", return_value="host"),
                patch.object(sessions.subprocess, "run", side_effect=run),
                patch.object(sb_notify, "record_agent_created") as record_sb,
            ):
                created = sessions.create_session(
                    str(workspace),
                    ["--session", "native-pi"],
                    resumed_from="native-pi",
                    name="pi-agent",
                    runtime=sessions.RUNTIME_PI,
                )

            launch = calls[0]
            command_text = " ".join(launch)
            self.assertEqual(created.runtime, sessions.RUNTIME_PI)
            self.assertIn("multiverse-pi-session", command_text)
            self.assertIn("--session native-pi", command_text)
            self.assertIn("MULTIVERSE_RUNTIME=pi", launch)
            self.assertIn(
                f"MULTIVERSE_RUNTIME_SESSION_ID_PATH={state / 'piwrap' / 'runtime_session_id'}",
                launch,
            )
            self.assertIn("MULTIVERSE_PI_BIN=custom-pi", launch)
            record_sb.assert_not_called()

    def test_auto_resume_uses_pi_session_flag(self) -> None:
        target = SimpleNamespace(
            name="pi-agent",
            runtime=sessions.RUNTIME_PI,
            session_id="native-pi",
            session=None,
            status="dormant",
        )
        with patch.object(server, "create_session") as create:
            server._auto_resume(target, "/tmp/work")
        self.assertEqual(create.call_args.kwargs["claude_args"], ["--session", "native-pi"])
        self.assertEqual(create.call_args.kwargs["runtime"], sessions.RUNTIME_PI)


class PiAskTests(unittest.TestCase):
    def test_pi_side_question_uses_scratch_fork_and_cleans_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            source = root / "source.jsonl"
            source.write_text("{}\n")
            log = root / "args.json"
            fake = root / "pi"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "json.dump(sys.argv[1:], open(os.environ['PI_ASK_LOG'], 'w'))\n"
                "print('Pi side answer')\n"
            )
            fake.chmod(0o755)
            target = ask_agents.AskTarget(
                name="pi-agent",
                cwd=str(workspace),
                runtime=sessions.RUNTIME_PI,
                session_id="native-pi",
            )
            with (
                patch.dict(
                    os.environ,
                    {"MULTIVERSE_PI_BIN": str(fake), "PI_ASK_LOG": str(log)},
                    clear=False,
                ),
                patch.object(ask_agents, "pi_session_path", return_value=source),
            ):
                result = ask_agents.ask_one("What changed?", target)

            argv = json.loads(log.read_text())
            scratch = Path(argv[argv.index("--session-dir") + 1])
            self.assertEqual(result.response, "Pi side answer")
            self.assertIsNone(result.error)
            self.assertIn("--fork", argv)
            self.assertIn("--no-tools", argv)
            self.assertFalse(scratch.exists())


class PiMessageRoutingTests(unittest.TestCase):
    def test_live_pi_delivery_never_types_into_tmux(self) -> None:
        cwd = "/tmp/pi-message-workspace"
        wrapped = sessions.Session(
            id="mvpi",
            cwd=cwd,
            created_at=1,
            status="idle",
            name="receiver",
            runtime=sessions.RUNTIME_PI,
            pi_session_id="native-pi",
        )
        target = sessions.DashboardRow(
            name="receiver",
            cwd=cwd,
            status="idle",
            status_ts=1,
            runtime=sessions.RUNTIME_PI,
            session=wrapped,
            session_id="native-pi",
        )
        with (
            patch.object(server, "_resolve_self_identity", return_value=("sender", cwd)),
            patch.object(server, "find_agent_by_name", return_value=target),
            patch.object(server, "wake_is_outstanding", return_value=False),
            patch.object(server, "write_message_to_inbox", return_value=Path("message.json")),
            patch.object(server, "log_message_sent"),
            patch.object(server, "log_notification_routed"),
            patch.object(server, "_wake_via_tmux", side_effect=AssertionError("tmux used")),
            patch.dict(os.environ, {"MULTIVERSE_RUNTIME": sessions.RUNTIME_PI}, clear=False),
        ):
            text = server.message_agent("receiver", "hello")

        self.assertIn("pi extension", text)
        self.assertIn("within a couple of seconds", text)


if __name__ == "__main__":
    unittest.main()
