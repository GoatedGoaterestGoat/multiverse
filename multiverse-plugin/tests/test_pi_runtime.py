from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import (
    ask_agents,
    atomic_files,
    inbox,
    pi_transcript,
    sb_connector,
    sb_notify,
    server,
    sessions,
)


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
    def test_secondbrain_prompt_path_does_not_target_pi(self) -> None:
        target = sessions.DashboardRow(
            name="pi-agent",
            cwd="/tmp/pi-message-workspace",
            status="idle",
            status_ts=1,
            runtime=sessions.RUNTIME_PI,
            session_id="native-pi",
        )
        connector = object.__new__(sb_connector.SBConnector)
        with (
            patch("src.sessions.find_agent_by_name", return_value=target),
            patch(
                "src.sb_actions.send_prompt_via_tmux",
                side_effect=AssertionError("tmux used"),
            ),
        ):
            result = connector._deliver_prompt_blocking(
                target.cwd, target.name, "hello"
            )

        self.assertEqual(result[0], "not_found")

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


class PiCallerIdentityTests(unittest.TestCase):
    """``_resolve_self_identity`` must never hand a non-Claude caller a Claude
    agent's identity.

    The Claude-transcript heuristic exists for one runtime: Claude's shared
    bg-pty-host freezes MULTIVERSE_SESSION_* to whichever session first spawned
    the daemon. Every other runtime owns its own MCP server and correct env, so
    for them the scan can only pick the wrong agent — whichever Claude session
    last wrote a jsonl row. That agent's inbox is then drained by the caller.

    Only the non-Claude direction is asserted here. The Claude direction is
    covered by a separately filed pre-existing defect and is deliberately not
    pinned by this suite.
    """

    def _fixture(self, root: Path) -> dict[str, str]:
        projects = root / "claude" / "projects" / "-slug"
        projects.mkdir(parents=True)
        (projects / "abc.jsonl").write_text(
            json.dumps({"sessionId": "sid-1", "type": "user"}) + "\n"
        )
        sess = root / "mv" / "sessions" / "s1"
        sess.mkdir(parents=True)
        (sess / "claude_uuid").write_text("sid-1\n")
        (sess / "meta.json").write_text(
            json.dumps({"name": "other-claude-agent", "cwd": "/other/workspace"})
        )
        return {
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
            "MULTIVERSE_ROOT": str(root / "mv"),
            "MULTIVERSE_SESSION_NAME": "pi-agent",
            "MULTIVERSE_SESSION_CWD": "/pi/workspace",
        }

    def test_pi_caller_keeps_its_own_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = self._fixture(Path(raw))
            env["MULTIVERSE_RUNTIME"] = sessions.RUNTIME_PI
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(
                    server._resolve_self_identity(), ("pi-agent", "/pi/workspace")
                )

    def test_codex_caller_keeps_its_own_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = self._fixture(Path(raw))
            env["MULTIVERSE_RUNTIME"] = sessions.RUNTIME_CODEX
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(
                    server._resolve_self_identity(), ("pi-agent", "/pi/workspace")
                )


class PiRunnerFallbackTests(unittest.TestCase):
    def test_ended_fallback_runs_from_the_plugin_root(self) -> None:
        """tmux launches the runner with ``-c <workspace>``. ``-m src.mvctl``
        resolves ``src`` off cwd, so the fallback only works from the plugin
        root — and a workspace with its own ``src/`` package must never be the
        thing we import."""
        runner = Path(__file__).resolve().parent.parent / "bin" / "multiverse-pi-session"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin_root = root / "plugin"
            (plugin_root / "pi-extension").mkdir(parents=True)
            (plugin_root / "pi-extension" / "index.ts").write_text("")
            (plugin_root / "src").mkdir()
            venv_bin = plugin_root / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            log = root / "fallback.json"
            shim = venv_bin / "python"
            shim.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "json.dump({'cwd': os.getcwd(), 'argv': sys.argv[1:]},"
                " open(os.environ['PI_FALLBACK_LOG'], 'w'))\n"
            )
            shim.chmod(0o755)

            # A workspace that shadows the plugin's own package.
            workspace = root / "workspace"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "__init__.py").write_text(
                "raise SystemExit('imported the workspace src')\n"
            )

            fake_pi = root / "pi"
            fake_pi.write_text("#!/bin/sh\nexit 0\n")
            fake_pi.chmod(0o755)

            result = subprocess.run(
                [str(runner)],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "MULTIVERSE_PI_BIN": str(fake_pi),
                    "MULTIVERSE_PLUGIN_ROOT": str(plugin_root),
                    "MULTIVERSE_STATUS": str(root / "status.json"),
                    "PI_FALLBACK_LOG": str(log),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(log.is_file(), "the ended fallback never ran")
            recorded = json.loads(log.read_text())
            self.assertEqual(
                Path(recorded["cwd"]).resolve(), plugin_root.resolve(),
                "fallback must run from the plugin root or `-m src.mvctl` cannot import",
            )
            self.assertEqual(recorded["argv"], ["-m", "src.mvctl", "status", "ended"])


class PiStatusOrderingTests(unittest.TestCase):
    def test_ended_is_terminal(self) -> None:
        """The extension spawns one detached ``mvctl status`` per lifecycle
        event, so agent_settled's ``idle`` can outlive pi and land after
        session_shutdown's ``ended``. Atomic replacement orders writes but not
        the processes that make them."""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            sessions.write_status_path(path, "ended")
            for stale in ("idle", "working", "starting", "listening"):
                sessions.write_status_path(path, stale)
                self.assertEqual(json.loads(path.read_text())["status"], "ended")

    def test_teardown_states_still_win_over_ended(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            sessions.write_status_path(path, "ended")
            sessions.write_status_path(path, "ending")
            self.assertEqual(json.loads(path.read_text())["status"], "ending")

    def test_normal_lifecycle_is_unaffected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            for status in ("starting", "idle", "working", "idle", "ended"):
                sessions.write_status_path(path, status)
                self.assertEqual(json.loads(path.read_text())["status"], status)

    def test_a_concurrent_writer_cannot_interleave_with_the_ended_guard(self) -> None:
        """The guard is read-then-write, so sequential tests prove nothing about
        it. Unlocked, the losing interleaving is

            idle: read (not ended) -> ended: write -> idle: write

        and the session renders idle forever after teardown. This stages that
        interleaving deterministically: hold the lock, start a real second
        process writing ``idle``, confirm it is genuinely blocked rather than
        merely slow, write ``ended`` underneath it, then release and let it run.
        Without the lock the child completes immediately and clobbers.
        """
        plugin_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            sessions.write_status_path(path, "working")
            child: subprocess.Popen | None = None
            try:
                # The lock is taken BEFORE the child exists. Spawning first
                # would let the child win the race, finish its write and exit
                # before the parent ever acquired, making the assertion below
                # fail for a scheduling reason rather than a locking one.
                with atomic_files.exclusive_file_lock(
                    path.with_name(path.name + ".lock")
                ):
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            "import sys; from pathlib import Path;"
                            " sys.path.insert(0, sys.argv[1]);"
                            " from src import sessions;"
                            " sessions.write_status_path(Path(sys.argv[2]), 'idle')",
                            str(plugin_root),
                            str(path),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    # Long enough for the child to start, import and reach the
                    # lock. It must still be parked there.
                    time.sleep(2.0)
                    self.assertIsNone(
                        child.poll(),
                        "the idle writer was not excluded; check+write is unlocked",
                    )
                    # The teardown write lands while the child is parked.
                    atomic_files.atomic_write_json(
                        path, {"status": "ended", "ts": time.time()}
                    )
                _, err = child.communicate(timeout=30)
            finally:
                if child is not None and child.poll() is None:
                    child.kill()
                    child.communicate()
            self.assertEqual(child.returncode, 0, err.decode())
            self.assertEqual(
                json.loads(path.read_text())["status"],
                "ended",
                "a stale idle overwrote ended after the lock was released",
            )


class PiCwdCanonicalizationTests(unittest.TestCase):
    def test_session_dir_is_resolve_stable(self) -> None:
        """``list_pi_sessions_in`` resolves before encoding and
        ``pi_session_path`` did not, so the two derived different directories
        for one workspace — the row listed but ask/delete said 'not found'."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            real = root / "real"
            real.mkdir()
            link = root / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            self.assertEqual(
                pi_transcript.encode_pi_cwd(str(link)),
                pi_transcript.encode_pi_cwd(str(real)),
            )
            self.assertEqual(
                pi_transcript.encode_pi_cwd(str(root / "real" / ".." / "real")),
                pi_transcript.encode_pi_cwd(str(real)),
            )

    def test_lookup_and_listing_agree_through_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            real = root / "real"
            real.mkdir()
            link = root / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            agent_dir = root / "agent"
            session_dir = agent_dir / "sessions" / pi_transcript.encode_pi_cwd(str(real))
            session_dir.mkdir(parents=True)
            (session_dir / "2026-01-01_pid-1.jsonl").write_text(
                json.dumps({"type": "session", "id": "pid-1", "cwd": str(real)}) + "\n"
                + json.dumps({"type": "session_info", "name": "pi-agent"}) + "\n"
            )
            with patch.dict(
                os.environ, {"PI_CODING_AGENT_DIR": str(agent_dir)}, clear=False
            ):
                os.environ.pop("PI_CODING_AGENT_SESSION_DIR", None)
                listed = pi_transcript.list_pi_sessions_in(str(link))
                found = pi_transcript.pi_session_path("pid-1", str(link))
            self.assertEqual([item["id"] for item in listed], ["pid-1"])
            self.assertIsNotNone(found, "listed but not resolvable — the split-dir bug")


class PiTranscriptNameTests(unittest.TestCase):
    def test_latest_nameless_session_info_row_clears_the_name(self) -> None:
        """Match pi's SessionManager, where missing/blank names explicitly
        clear the current title instead of retaining a stale reservation."""
        for final_info in ({"type": "session_info"}, {"type": "session_info", "name": ""}):
            with self.subTest(final_info=final_info), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "s.jsonl"
                path.write_text(
                    json.dumps({"type": "session", "id": "pid-1", "cwd": "/w"}) + "\n"
                    + json.dumps({"type": "session_info", "name": "pi-agent"}) + "\n"
                    + json.dumps(final_info) + "\n"
                )
                self.assertIsNone(
                    pi_transcript.read_pi_session_metadata(path)["name"]
                )

    def test_rename_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "s.jsonl"
            path.write_text(
                json.dumps({"type": "session", "id": "pid-1", "cwd": "/w"}) + "\n"
                + json.dumps({"type": "session_info", "name": "first"}) + "\n"
                + json.dumps({"type": "session_info", "name": "second"}) + "\n"
            )
            self.assertEqual(pi_transcript.read_pi_session_metadata(path)["name"], "second")


class PiAskForkIsolationTests(unittest.TestCase):
    def test_fork_drops_session_root_but_keeps_agent_config(self) -> None:
        """The scratch fork must not inherit the real session directory, but
        it still needs PI_CODING_AGENT_DIR for settings and credentials."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            source = root / "source.jsonl"
            source.write_text("{}\n")
            log = root / "env.json"
            fake = root / "pi"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os\n"
                "json.dump({k: v for k, v in os.environ.items()"
                " if k.startswith(('PI_CODING_AGENT_', 'MULTIVERSE_'))},"
                " open(os.environ['PI_ASK_LOG'], 'w'))\n"
                "print('answer')\n"
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
                    {
                        "MULTIVERSE_PI_BIN": str(fake),
                        "PI_ASK_LOG": str(log),
                        "PI_CODING_AGENT_DIR": str(root / "real-agent-dir"),
                        "PI_CODING_AGENT_SESSION_DIR": str(root / "real-sessions"),
                        "MULTIVERSE_SESSION_NAME": "origin",
                    },
                    clear=False,
                ),
                patch.object(ask_agents, "pi_session_path", return_value=source),
            ):
                result = ask_agents.ask_one("What changed?", target)

            self.assertEqual(result.response, "answer")
            leaked = json.loads(log.read_text())
            self.assertEqual(
                leaked,
                {"PI_CODING_AGENT_DIR": str(root / "real-agent-dir")},
                f"fork inherited unsafe session state: {sorted(leaked)}",
            )


class MvctlExitCodeTests(unittest.TestCase):
    def test_error_text_exits_non_zero(self) -> None:
        """The extension derives ``r.ok`` from the exit code, and the server's
        tools report failure in-band as an ``Error…`` string. Emitting that as
        ok=0 notified hard failures at "info" severity."""
        from src import mvctl

        with patch("sys.stdout", new_callable=io.StringIO):
            with patch(
                "src.server.message_agent",
                return_value="Error: cannot message yourself.",
            ):
                rc = mvctl.main(["message", "--to", "self", "--body", "hi"])
        self.assertEqual(rc, 1)

    def test_normal_text_exits_zero(self) -> None:
        from src import mvctl

        with patch("sys.stdout", new_callable=io.StringIO):
            with patch("src.server.message_agent", return_value="Message delivered."):
                rc = mvctl.main(["message", "--to", "peer", "--body", "hi"])
        self.assertEqual(rc, 0)

    def test_legacy_push_flag_is_still_accepted(self) -> None:
        """Already-running pi processes may still use the branch's old argv."""
        from src import mvctl

        with patch("sys.stdout", new_callable=io.StringIO):
            with patch("src.server.message_agent", return_value="Message delivered."):
                rc = mvctl.main(
                    ["message", "--to", "peer", "--body", "hi", "--push"]
                )
        self.assertEqual(rc, 0)

    def test_json_mode_reports_the_same_verdict(self) -> None:
        from src import mvctl

        with patch("src.server.list_agents", return_value="Error listing agents: boom"):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = mvctl.main(["--json", "list-agents"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["ok"], False)


class SBConnectorResumeContractTests(unittest.TestCase):
    def test_dormant_pi_agent_is_rejected_as_unsupported(self) -> None:
        """Pi remains outside the SecondBrain prompt/resume seam."""
        target = SimpleNamespace(
            name="pi-agent",
            runtime=sessions.RUNTIME_PI,
            session_id="native-pi",
            session=None,
            status="dormant",
        )
        with patch("src.sessions.create_session") as create:
            with self.assertRaisesRegex(RuntimeError, "not supported by SecondBrain"):
                sb_connector.SBConnector._auto_resume_with_prompt(
                    None, target, "/tmp/work", "do the thing"
                )
        create.assert_not_called()

    def test_claude_and_codex_dispatch_is_unchanged(self) -> None:
        for runtime, expected in (
            (sessions.RUNTIME_CLAUDE, ["--resume", "native-id"]),
            (sessions.RUNTIME_CODEX, ["resume", "native-id"]),
        ):
            target = SimpleNamespace(
                name="agent",
                runtime=runtime,
                session_id="native-id",
                session=None,
                status="dormant",
            )
            with patch("src.sessions.create_session") as create:
                sb_connector.SBConnector._auto_resume_with_prompt(
                    None, target, "/tmp/work", "p"
                )
            self.assertEqual(create.call_args.kwargs["claude_args"], expected)

class InboxClaimReleaseTests(unittest.TestCase):
    """A failed hand-over must return the messages now, not in a minute.

    ``claim-inbox`` stages messages out of the pending pool by renaming them.
    If ``sendMessage`` then throws, catching the error is not enough — the
    files stay staged and invisible to every deliverer until
    ``_readopt_stale_claims`` adopts them ``_STALE_CLAIM_SECONDS`` later, with
    the sender blocked on a reply throughout.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = str(self.root / "workspace")
        self.name = "pi-agent"
        patcher = patch.object(inbox, "INBOX_DIR", self.root / "state" / "inbox")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _queue(self, body: str = "hello") -> None:
        inbox.write_message_to_inbox(
            sender_name="peer",
            recipient_name=self.name,
            recipient_cwd=self.cwd,
            message=body,
        )

    def test_release_restores_pending_immediately(self) -> None:
        self._queue()
        self.assertEqual(inbox.pending_inbox_count(self.cwd, self.name), 1)

        claimed = inbox.claim_pending_messages(self.cwd, self.name)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(
            inbox.pending_inbox_count(self.cwd, self.name), 0,
            "claiming must hide the message from other deliverers",
        )

        basenames = [Path(m["__claim_path"]).name for m in claimed]
        released = inbox.release_claimed_messages(self.cwd, self.name, basenames)

        self.assertEqual(released, 1)
        self.assertEqual(
            inbox.pending_inbox_count(self.cwd, self.name), 1,
            "released messages must be deliverable at once, not after the 60s "
            "stale-claim sweep",
        )
        again = inbox.claim_pending_messages(self.cwd, self.name)
        self.assertEqual([m["body"] for m in again], ["hello"])

    def test_release_ignores_anything_that_is_not_a_staged_basename(self) -> None:
        self._queue()
        inbox.claim_pending_messages(self.cwd, self.name)
        victim = self.root / "victim.json"
        victim.write_text("{}")
        self.assertEqual(
            inbox.release_claimed_messages(
                self.cwd,
                self.name,
                ["../victim.json", "not-a-claim.json", "", "/etc/passwd"],
            ),
            0,
        )
        self.assertTrue(victim.exists())
        self.assertEqual(inbox.pending_inbox_count(self.cwd, self.name), 0)

    def test_mvctl_release_inbox_round_trips(self) -> None:
        from src import mvctl

        self._queue()
        claimed = inbox.claim_pending_messages(self.cwd, self.name)
        basename = Path(claimed[0]["__claim_path"]).name
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = mvctl.main(
                [
                    "release-inbox",
                    "--name", self.name,
                    "--cwd", self.cwd,
                    "--claim", basename,
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "1")
        self.assertEqual(inbox.pending_inbox_count(self.cwd, self.name), 1)


if __name__ == "__main__":
    unittest.main()
