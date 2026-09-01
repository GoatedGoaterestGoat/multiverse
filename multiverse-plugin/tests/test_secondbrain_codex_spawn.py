from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import sb_connector, sb_events, sb_notify, server, sessions, spawn_requests
from secondbrain import protocol as sb_protocol


class SecondBrainSpawnDecisionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.cwd = str((self.root / "workspace").resolve())
        Path(self.cwd).mkdir()
        self.request_dir = self.root / "spawn_requests"
        self.connector = object.__new__(sb_connector.SBConnector)

    def _write_request(self, *, runtime: str = sessions.RUNTIME_CODEX) -> dict:
        with (
            patch.object(spawn_requests, "SPAWN_REQUESTS_DIR", self.request_dir),
            patch.object(spawn_requests.sb_notify, "record_spawn_request"),
        ):
            path = spawn_requests.write_spawn_request(
                req_id="request-id",
                from_name="codex-caller",
                cwd=self.cwd,
                instruction="Investigate the failing tests.",
                default_name="test-agent",
                caller_tmux_session="multiverse-caller",
                caller_runtime=runtime,
            )
            return json.loads(path.read_text())

    def _resolve(
        self, decision: str, name: str = ""
    ) -> tuple[tuple[str, str, str, str], object]:
        decision_recorder = unittest.mock.Mock()
        with (
            patch.object(spawn_requests, "SPAWN_REQUESTS_DIR", self.request_dir),
            patch.object(spawn_requests, "_caller_alive", return_value=True),
            patch.object(
                spawn_requests.sb_notify,
                "record_spawn_decision",
                decision_recorder,
            ),
        ):
            result = self.connector._deliver_spawn_decision_blocking(
                self.cwd, "request-id", decision, name
            )
        return result, decision_recorder

    def test_codex_approval_inherits_runtime_and_preserves_renaming(self) -> None:
        request = self._write_request()

        result, recorder = self._resolve("approve", "renamed-agent")

        self.assertEqual(
            result,
            (
                "ok",
                "Approved spawn of 'renamed-agent' (renamed from 'test-agent').",
                "",
                "",
            ),
        )
        resolution = json.loads(
            (self.request_dir / "request-id.resolved.json").read_text()
        )
        self.assertEqual(
            resolution,
            {
                "status": "accepted",
                "name": "renamed-agent",
                "reason": None,
                "runtime": sessions.RUNTIME_CODEX,
                "ts": resolution["ts"],
            },
        )
        recorder.assert_called_once_with(request, "accepted", "renamed-agent")

    def test_legacy_request_without_runtime_defaults_to_claude(self) -> None:
        request = self._write_request(runtime=sessions.RUNTIME_CLAUDE)
        request.pop("caller_runtime")
        (self.request_dir / "request-id.json").write_text(json.dumps(request))

        result, _ = self._resolve("approve")

        self.assertEqual(result[0], "ok")
        resolution = json.loads(
            (self.request_dir / "request-id.resolved.json").read_text()
        )
        self.assertEqual(resolution["runtime"], sessions.RUNTIME_CLAUDE)

    def test_decline_keeps_existing_resolution_and_event_shape(self) -> None:
        request = self._write_request()

        result, recorder = self._resolve("decline")

        self.assertEqual(
            result,
            ("ok", "Declined spawn request for 'test-agent'.", "", ""),
        )
        resolution = json.loads(
            (self.request_dir / "request-id.resolved.json").read_text()
        )
        self.assertEqual(resolution["status"], "rejected")
        self.assertIsNone(resolution["runtime"])
        recorder.assert_called_once_with(request, "rejected", None)

    def test_duplicate_decision_does_not_overwrite_winner(self) -> None:
        self._write_request()
        first, _ = self._resolve("approve", "first-agent")
        second, recorder = self._resolve("approve", "second-agent")

        self.assertEqual(first[0], "ok")
        self.assertEqual(second[0], "err")
        self.assertEqual(second[2], "already_decided")
        resolution = json.loads(
            (self.request_dir / "request-id.resolved.json").read_text()
        )
        self.assertEqual(resolution["name"], "first-agent")
        self.assertEqual(resolution["runtime"], sessions.RUNTIME_CODEX)
        recorder.assert_not_called()

    def test_workspace_mismatch_and_dead_caller_write_no_resolution(self) -> None:
        self._write_request()
        other_cwd = str((self.root / "other").resolve())
        Path(other_cwd).mkdir()

        with patch.object(spawn_requests, "SPAWN_REQUESTS_DIR", self.request_dir):
            mismatch = self.connector._deliver_spawn_decision_blocking(
                other_cwd, "request-id", "approve", ""
            )
        with (
            patch.object(spawn_requests, "SPAWN_REQUESTS_DIR", self.request_dir),
            patch.object(spawn_requests, "_caller_alive", return_value=False),
        ):
            dead = self.connector._deliver_spawn_decision_blocking(
                self.cwd, "request-id", "approve", ""
            )

        self.assertEqual(mismatch[2], "forbidden")
        self.assertEqual(dead[2], "not_found")
        self.assertFalse(
            (self.request_dir / "request-id.resolved.json").exists()
        )

    def test_invalid_caller_runtime_fails_closed(self) -> None:
        self._write_request(runtime="unsupported")

        result, recorder = self._resolve("approve")

        self.assertEqual(result[0], "err")
        self.assertEqual(result[2], "deliver_failed")
        self.assertIn("invalid caller runtime", result[3])
        self.assertFalse(
            (self.request_dir / "request-id.resolved.json").exists()
        )
        recorder.assert_not_called()

    def test_pi_spawn_request_can_be_approved_through_secondbrain(self) -> None:
        request = self._write_request(runtime=sessions.RUNTIME_PI)

        result, recorder = self._resolve("approve")

        self.assertEqual(result[0], "ok")
        resolution = json.loads(
            (self.request_dir / "request-id.resolved.json").read_text()
        )
        self.assertEqual(resolution["runtime"], sessions.RUNTIME_PI)
        recorder.assert_called_once_with(request, "accepted", "test-agent")

    def test_codex_spawn_events_do_not_add_runtime_to_wire_payloads(self) -> None:
        request = self._write_request()

        requesting_agent, request_payload = sb_events.spawn_request_event_fields(
            request
        )
        deciding_agent, decision_payload = sb_events.spawn_decision_event_fields(
            request, "accepted", "renamed-agent"
        ) or (None, None)

        self.assertEqual(requesting_agent, "codex-caller")
        self.assertEqual(deciding_agent, "codex-caller")
        self.assertEqual(
            set(request_payload),
            {"new_agent_name", "summary", "full", "request_id"},
        )
        self.assertEqual(
            set(decision_payload),
            {
                "request_id",
                "decision",
                "requesting_agent",
                "requested_name",
                "final_name",
                "renamed",
                "summary",
            },
        )
        self.assertNotIn("runtime", request_payload)
        self.assertNotIn("runtime", decision_payload)


class SecondBrainDirectSpawnRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cwd = str((Path(self.temp_dir.name) / "workspace").resolve())
        Path(self.cwd).mkdir()
        self.connector = object.__new__(sb_connector.SBConnector)

    def _deliver(self, *, runtime: str | None, instruction: str = "Do the work."):
        env = {}
        if runtime is not None:
            env["MULTIVERSE_SECONDBRAIN_SPAWN_RUNTIME"] = runtime
        create = unittest.mock.Mock()
        with (
            patch.object(sessions, "find_agent_by_name", return_value=None),
            patch.object(sessions, "create_session", create),
            patch.dict(os.environ, env, clear=True),
        ):
            result = self.connector._deliver_spawn_blocking(
                self.cwd, "new-agent", instruction
            )
        return result, create

    def test_codex_policy_starts_named_prompted_codex_agent(self) -> None:
        result, create = self._deliver(runtime=sessions.RUNTIME_CODEX)

        self.assertEqual(
            result,
            (
                "ok",
                "Spawned 'new-agent' in 'workspace' with an initial task.",
                "new-agent",
                "",
                "",
            ),
        )
        create.assert_called_once_with(
            cwd=self.cwd,
            claude_args=[],
            name="new-agent",
            runtime=sessions.RUNTIME_CODEX,
            initial_prompt="Do the work.",
        )

    def test_direct_spawn_defaults_to_existing_claude_behavior(self) -> None:
        result, create = self._deliver(runtime=None, instruction="")

        self.assertEqual(result[0], "ok")
        self.assertIn("idle", result[1])
        create.assert_called_once_with(
            cwd=self.cwd,
            claude_args=[],
            name="new-agent",
            runtime=sessions.RUNTIME_CLAUDE,
            initial_prompt=None,
        )

    def test_pi_policy_starts_named_prompted_pi_agent(self) -> None:
        result, create = self._deliver(runtime=sessions.RUNTIME_PI)

        self.assertEqual(result[0], "ok")
        create.assert_called_once_with(
            cwd=self.cwd,
            claude_args=[],
            name="new-agent",
            runtime=sessions.RUNTIME_PI,
            initial_prompt="Do the work.",
        )

    def test_duplicate_name_and_invalid_policy_do_not_create(self) -> None:
        with (
            patch.object(
                sessions,
                "find_agent_by_name",
                return_value=SimpleNamespace(name="new-agent"),
            ),
            patch.object(sessions, "create_session") as duplicate_create,
        ):
            duplicate = self.connector._deliver_spawn_blocking(
                self.cwd, "new-agent", "Do the work."
            )
        invalid, invalid_create = self._deliver(runtime="unsupported")

        self.assertEqual(duplicate[3], "duplicate_name")
        duplicate_create.assert_not_called()
        self.assertEqual(invalid[3], "deliver_failed")
        self.assertIn("MULTIVERSE_SECONDBRAIN_SPAWN_RUNTIME", invalid[4])
        invalid_create.assert_not_called()


class SecondBrainSpawnActionContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cwd = str(Path("/workspace").resolve())
        self.connector = object.__new__(sb_connector.SBConnector)
        self.connector.identity = SimpleNamespace(granted_workspaces=[self.cwd])
        self.connector.dedup = sb_connector.ActionDedup()
        self.connector._send = AsyncMock()

    def _action(self, kind: sb_protocol.ActionKind, payload: dict):
        return sb_protocol.Action(
            action_id="action-id",
            ts="2026-07-19T00:00:00Z",
            workspace_id=f"device-id:{self.cwd}",
            kind=kind,
            payload=payload,
        )

    async def test_direct_spawn_result_schema_is_unchanged(self) -> None:
        action = self._action(
            sb_protocol.ActionKind.SPAWN_AGENT,
            {"name": "codex-child", "instruction": "Do the work."},
        )
        with patch.object(
            self.connector,
            "_deliver_spawn_blocking",
            return_value=(
                "ok",
                "Spawned 'codex-child' in 'workspace' with an initial task.",
                "codex-child",
                "",
                "",
            ),
        ):
            await self.connector._handle_spawn_agent_action(object(), action)

        result = self.connector._send.await_args.args[1]
        self.assertEqual(result.status, sb_protocol.ResultStatus.OK)
        self.assertEqual(
            result.result,
            {
                "status": "Spawned 'codex-child' in 'workspace' with an initial task.",
                "spawned_name": "codex-child",
            },
        )
        self.assertNotIn("runtime", result.result)

    async def test_spawn_action_preserves_workspace_acl(self) -> None:
        action = self._action(
            sb_protocol.ActionKind.SPAWN_AGENT,
            {"name": "codex-child"},
        )
        action.workspace_id = "device-id:/not/granted"
        with patch.object(self.connector, "_deliver_spawn_blocking") as deliver:
            await self.connector._handle_spawn_agent_action(object(), action)

        result = self.connector._send.await_args.args[1]
        self.assertEqual(result.status, sb_protocol.ResultStatus.REJECTED)
        self.assertEqual(result.error["code"], "forbidden")
        deliver.assert_not_called()

    async def test_spawn_action_preserves_validation_and_start_failure_errors(
        self,
    ) -> None:
        invalid = self._action(sb_protocol.ActionKind.SPAWN_AGENT, {"name": ""})
        with patch.object(self.connector, "_deliver_spawn_blocking") as deliver:
            await self.connector._handle_spawn_agent_action(object(), invalid)
        invalid_result = self.connector._send.await_args.args[1]

        self.connector._send.reset_mock()
        self.connector.dedup = sb_connector.ActionDedup()
        failing = self._action(
            sb_protocol.ActionKind.SPAWN_AGENT, {"name": "codex-child"}
        )
        with patch.object(
            self.connector,
            "_deliver_spawn_blocking",
            return_value=(
                "err",
                "",
                "",
                "deliver_failed",
                "create_session failed: tmux failed",
            ),
        ):
            await self.connector._handle_spawn_agent_action(object(), failing)
        failure_result = self.connector._send.await_args.args[1]

        self.assertEqual(invalid_result.status, sb_protocol.ResultStatus.REJECTED)
        self.assertEqual(invalid_result.error["code"], "bad_request")
        deliver.assert_not_called()
        self.assertEqual(failure_result.status, sb_protocol.ResultStatus.REJECTED)
        self.assertEqual(failure_result.error["code"], "deliver_failed")

    async def test_cached_spawn_action_is_not_executed_again(self) -> None:
        action = self._action(
            sb_protocol.ActionKind.SPAWN_AGENT, {"name": "codex-child"}
        )
        cached = sb_protocol.ActionResult(
            action_id=action.action_id,
            status=sb_protocol.ResultStatus.OK,
            result={"status": "already done", "spawned_name": "codex-child"},
        )
        self.connector.dedup.remember(cached)

        with patch.object(self.connector, "_handle_spawn_agent_action") as handle:
            await self.connector._on_action(object(), action)

        self.connector._send.assert_awaited_once_with(unittest.mock.ANY, cached)
        handle.assert_not_called()


class CodexSpawnEventOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_decision_and_creation_publish_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cwd = str((root / "workspace").resolve())
            Path(cwd).mkdir()
            request_dir = root / "spawn_requests"
            connector = object.__new__(sb_connector.SBConnector)
            published: list[str] = []

            def write_and_approve(**kwargs):
                spawn_requests.write_spawn_request(**kwargs)
                result = connector._deliver_spawn_decision_blocking(
                    cwd, "request-id", "approve", "renamed-child"
                )
                self.assertEqual(result[0], "ok")

            env = {
                "MULTIVERSE_SESSION_CWD": cwd,
                "MULTIVERSE_SESSION_NAME": "codex-caller",
                "MULTIVERSE_SESSION_ID": "caller-id",
                "MULTIVERSE_RUNTIME": sessions.RUNTIME_CODEX,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    spawn_requests, "SPAWN_REQUESTS_DIR", request_dir
                ),
                patch.object(
                    spawn_requests, "_caller_alive", return_value=True
                ),
                patch.object(
                    spawn_requests.sb_notify,
                    "record_spawn_request",
                    side_effect=lambda _request: published.append(
                        "agent.spawn_request"
                    ),
                ),
                patch.object(
                    spawn_requests.sb_notify,
                    "record_spawn_decision",
                    side_effect=lambda _request, _status, _name: published.append(
                        "user.spawn_decision"
                    ),
                ),
                patch.object(server, "all_session_names_in_cwd", return_value=set()),
                patch.object(server, "new_request_id", return_value="request-id"),
                patch.object(server, "write_spawn_request", side_effect=write_and_approve),
                patch.object(
                    server,
                    "create_session",
                    side_effect=lambda **_kwargs: published.append("agent.created"),
                ) as create,
                patch.object(server.subprocess, "run"),
            ):
                result = await server.spawn_agent(
                    instruction="Investigate the failure.", name="requested-child"
                )

            self.assertEqual(result, "Spawned agent with name 'renamed-child'.")
            self.assertEqual(
                published,
                [
                    "agent.spawn_request",
                    "user.spawn_decision",
                    "agent.created",
                ],
            )
            create.assert_called_once_with(
                cwd=cwd,
                claude_args=[],
                name="renamed-child",
                runtime=sessions.RUNTIME_CODEX,
                initial_prompt="Investigate the failure.",
            )
            self.assertFalse(request_dir.joinpath("request-id.json").exists())
            self.assertFalse(
                request_dir.joinpath("request-id.resolved.json").exists()
            )


class CodexAgentCreatedEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "sessions"
        self.plugin_root = self.root / "multiverse-plugin"
        (self.plugin_root / "bin").mkdir(parents=True)
        self.codex_plugin_root = self.root / "multiverse-codex"
        self.codex_plugin_root.mkdir()

    def _create_codex(self, run_side_effect=None):
        env = {
            "MULTIVERSE_PLUGIN_ROOT": str(self.plugin_root),
            "MULTIVERSE_CODEX_PLUGIN_ROOT": str(self.codex_plugin_root),
            "PATH": os.environ.get("PATH", ""),
        }
        if run_side_effect is None:
            run_side_effect = lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd, 0, "", ""
            )
        with (
            patch.object(sessions, "STATE_DIR", self.state_dir),
            patch.object(sessions, "MULTIVERSE_DIR", self.root),
            patch.object(sessions, "short_id", return_value="session-id"),
            patch.object(sessions.subprocess, "run", side_effect=run_side_effect),
            patch.object(sb_notify, "record_agent_created") as record_created,
            patch.dict(os.environ, env, clear=True),
        ):
            try:
                created = sessions.create_session(
                    cwd=str(self.root),
                    claude_args=[],
                    name="codex-child",
                    runtime=sessions.RUNTIME_CODEX,
                    initial_prompt="Do the work.",
                )
            except RuntimeError:
                created = None
        return created, record_created

    def test_successful_codex_materialization_emits_shared_created_event(self) -> None:
        created, record_created = self._create_codex()

        self.assertIsNotNone(created)
        record_created.assert_called_once_with(
            str(self.root.resolve()), "codex-child", "Do the work.",
            # The session id is part of agent.created's event_id: SB dedups on that id
            # alone, so name-only would make a re-used agent name collide with the
            # earlier agent's creation and be dropped. short_id is patched above.
            "session-id",
        )
        self.assertEqual(
            sb_events.agent_created_event_fields("Do the work."),
            {"initial_prompt": "Do the work."},
        )

    def test_failed_codex_materialization_emits_no_created_event(self) -> None:
        def fail_new_session(cmd, **kwargs):
            if cmd[:2] == ["tmux", "new-session"]:
                raise subprocess.CalledProcessError(1, cmd, stderr="tmux failed")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        created, record_created = self._create_codex(fail_new_session)

        self.assertIsNone(created)
        record_created.assert_not_called()


if __name__ == "__main__":
    unittest.main()
