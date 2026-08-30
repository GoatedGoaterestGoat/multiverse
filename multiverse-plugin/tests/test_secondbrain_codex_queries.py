from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import sb_connector, sb_queries, sessions
from secondbrain import protocol as sb_protocol


class SecondBrainCodexQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cwd = str(Path(self.temp_dir.name).resolve())
        self.other_cwd = str((Path(self.temp_dir.name) / "other").resolve())
        Path(self.other_cwd).mkdir()
        self.connector = object.__new__(sb_connector.SBConnector)
        self.connector.identity = SimpleNamespace(
            device_id="device-id",
            granted_workspaces=[self.cwd, self.other_cwd],
        )
        self.connector.channel = None

        claude_session = sessions.Session(
            id="mv-claude",
            cwd=self.cwd,
            created_at=1,
            status="working",
            status_ts=1_721_234_567,
            name="claude-agent",
            runtime=sessions.RUNTIME_CLAUDE,
            claude_uuid="claude-thread-id",
        )
        live_codex_session = sessions.Session(
            id="mv-codex",
            cwd=self.cwd,
            created_at=2,
            status="idle",
            status_ts=1_721_234_568,
            name="codex-live",
            runtime=sessions.RUNTIME_CODEX,
            codex_session_id="codex-live-thread-id",
        )
        ended_codex_session = sessions.Session(
            id="mv-ended",
            cwd=self.cwd,
            created_at=3,
            status="ended",
            status_ts=1_721_234_569,
            name="codex-ended",
            runtime=sessions.RUNTIME_CODEX,
            codex_session_id="codex-ended-thread-id",
        )
        self.rows = [
            sessions.DashboardRow(
                name="claude-agent",
                cwd=self.cwd,
                status="working",
                status_ts=claude_session.status_ts,
                runtime=sessions.RUNTIME_CLAUDE,
                session=claude_session,
                session_id="claude-thread-id",
                claude_uuid="claude-thread-id",
                preview="Claude preview",
            ),
            sessions.DashboardRow(
                name="codex-live",
                cwd=self.cwd,
                status="idle",
                status_ts=live_codex_session.status_ts,
                runtime=sessions.RUNTIME_CODEX,
                session=live_codex_session,
                session_id="codex-live-thread-id",
                preview="Codex live preview",
            ),
            sessions.DashboardRow(
                name="codex-ended",
                cwd=self.cwd,
                status="ended",
                status_ts=ended_codex_session.status_ts,
                runtime=sessions.RUNTIME_CODEX,
                session=ended_codex_session,
                session_id="codex-ended-thread-id",
                preview="Codex ended preview",
            ),
            sessions.DashboardRow(
                name="codex-dormant",
                cwd=self.cwd,
                status="dormant",
                status_ts=1_721_234_570,
                runtime=sessions.RUNTIME_CODEX,
                session_id="codex-dormant-thread-id",
                preview="Codex dormant preview",
            ),
        ]

    def _query(self, kind: sb_protocol.QueryKind, **params) -> sb_protocol.Query:
        return sb_protocol.Query(
            query_id=f"query-{kind.value}",
            kind=kind,
            workspace_id=f"device-id:{self.cwd}",
            params=params,
        )

    async def _run_query(
        self,
        query: sb_protocol.Query,
        rows: list[sessions.DashboardRow] | None = None,
    ):
        ws = SimpleNamespace(send=AsyncMock())
        with (
            patch.object(
                sb_queries,
                "list_named_agents_in_cwd",
                return_value=list(self.rows if rows is None else rows),
            ),
            patch.object(sb_queries, "tmux_session_exists", return_value=True),
            patch.object(
                sb_queries,
                "pending_inbox_count",
                side_effect=lambda _cwd, name: {
                    "claude-agent": 1,
                    "codex-live": 2,
                    "codex-ended": 3,
                    "codex-dormant": 4,
                }.get(name, 0),
            ),
            patch.object(sb_queries, "live_stats_cached", return_value=(100, 0)),
            patch.object(sb_queries, "structural_tokens_cached", return_value=20),
            patch.object(sb_queries, "build_enabled", return_value=True),
        ):
            await self.connector._on_query(ws, query)
        return sb_protocol.decode(ws.send.await_args.args[0])

    async def test_get_dashboard_preserves_contract_for_mixed_runtime_rows(self) -> None:
        wire = await self._run_query(
            self._query(sb_protocol.QueryKind.GET_DASHBOARD)
        )

        self.assertEqual(wire.status, sb_protocol.ResultStatus.OK)
        self.assertEqual(
            wire.result,
            {
                "sessions": [
                    {
                        "id": "mv-claude",
                        "name": "claude-agent",
                        "status": "working",
                        "tokens": 120,
                        "inbox": 1,
                        "build": "on",
                        "updated": "2024-07-17T16:42:47Z",
                        "preview": "Claude preview",
                    },
                    {
                        "id": "mv-codex",
                        "name": "codex-live",
                        "status": "idle",
                        "tokens": "-",
                        "inbox": 2,
                        "build": "off",
                        "updated": "2024-07-17T16:42:48Z",
                        "preview": "Codex live preview",
                    },
                    {
                        "id": "mv-ended",
                        "name": "codex-ended",
                        "status": "ended",
                        "tokens": "-",
                        "inbox": 3,
                        "build": "off",
                        "updated": "2024-07-17T16:42:49Z",
                        "preview": "Codex ended preview",
                    },
                    {
                        "id": "codex-do",
                        "name": "codex-dormant",
                        "status": "dormant",
                        "tokens": "-",
                        "inbox": 4,
                        "build": "off",
                        "updated": "2024-07-17T16:42:50Z",
                        "preview": "Codex dormant preview",
                    },
                ]
            },
        )
        for row in wire.result["sessions"]:
            self.assertNotIn("runtime", row)

    async def test_list_agents_preserves_existing_fields_for_codex(self) -> None:
        wire = await self._run_query(self._query(sb_protocol.QueryKind.LIST_AGENTS))

        self.assertEqual(wire.status, sb_protocol.ResultStatus.OK)
        self.assertEqual(
            wire.result["agents"][1],
            {
                "name": "codex-live",
                "status": "idle",
                "inbox": 2,
                "tokens": "-",
                "last_updated": "2024-07-17T16:42:48Z",
            },
        )
        self.assertEqual(wire.result["agents"][2]["status"], "ended")
        self.assertEqual(wire.result["agents"][3]["status"], "dormant")
        for row in wire.result["agents"]:
            self.assertNotIn("runtime", row)

    async def test_get_status_returns_codex_fields_and_missing_shape(self) -> None:
        found = await self._run_query(
            self._query(sb_protocol.QueryKind.GET_STATUS, agent="codex-dormant")
        )
        missing = await self._run_query(
            self._query(sb_protocol.QueryKind.GET_STATUS, agent="missing")
        )

        self.assertEqual(
            found.result,
            {
                "status": "dormant",
                "inbox": 4,
                "tokens": "-",
                "last_updated": "2024-07-17T16:42:50Z",
            },
        )
        self.assertNotIn("runtime", found.result)
        self.assertEqual(missing.result, {"status": None})

    async def test_get_status_uses_the_requested_granted_workspace(self) -> None:
        remote = sessions.DashboardRow(
            name="remote-codex",
            cwd=self.other_cwd,
            status="dormant",
            status_ts=1_721_234_571,
            runtime=sessions.RUNTIME_CODEX,
            session_id="remote-codex-thread-id",
        )
        query = self._query(
            sb_protocol.QueryKind.GET_STATUS,
            agent="remote-codex",
        )
        query.workspace_id = f"device-id:{self.other_cwd}"

        wire = await self._run_query(query, rows=[remote])

        self.assertEqual(wire.status, sb_protocol.ResultStatus.OK)
        self.assertEqual(
            wire.result,
            {
                "status": "dormant",
                "inbox": 0,
                "tokens": "-",
                "last_updated": "2024-07-17T16:42:51Z",
            },
        )

    async def test_query_acl_and_required_agent_contract_are_unchanged(self) -> None:
        for kind in (
            sb_protocol.QueryKind.GET_DASHBOARD,
            sb_protocol.QueryKind.LIST_AGENTS,
        ):
            query = self._query(kind)
            query.workspace_id = "device-id:/not/granted"
            wire = await self._run_query(query)
            self.assertEqual(wire.status, sb_protocol.ResultStatus.REJECTED)
            self.assertEqual(wire.error["code"], "forbidden")

        missing_agent = await self._run_query(
            self._query(sb_protocol.QueryKind.GET_STATUS)
        )
        self.assertEqual(missing_agent.status, sb_protocol.ResultStatus.REJECTED)
        self.assertEqual(missing_agent.error["code"], "bad_request")

    async def test_local_missing_tmux_corrects_live_codex_status_to_ended(self) -> None:
        with (
            patch.object(
                sb_queries,
                "list_named_agents_in_cwd",
                return_value=[self.rows[1]],
            ),
            patch.object(sb_queries, "tmux_session_exists", return_value=False),
            patch.object(sb_queries, "pending_inbox_count", return_value=0),
        ):
            result = sb_queries.get_status(self.cwd, "codex-live")

        self.assertEqual(result["status"], "ended")
        self.assertEqual(result["tokens"], "-")


if __name__ == "__main__":
    unittest.main()
