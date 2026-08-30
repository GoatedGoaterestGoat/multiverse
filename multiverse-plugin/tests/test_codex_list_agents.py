from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import server, sessions


class MixedRuntimeListAgentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cwd = str(Path(self.temp_dir.name).resolve())
        self.agents = [
            sessions.DashboardRow(
                name="alpha",
                cwd=self.cwd,
                status="idle",
                status_ts=1,
                runtime=sessions.RUNTIME_CLAUDE,
            ),
            sessions.DashboardRow(
                name="beta",
                cwd=self.cwd,
                status="working",
                status_ts=2,
                runtime=sessions.RUNTIME_CODEX,
            ),
        ]

    def _list_as(self, name: str, runtime: str) -> str:
        with (
            patch.dict(
                os.environ,
                {
                    "MULTIVERSE_SESSION_CWD": self.cwd,
                    "MULTIVERSE_SESSION_NAME": name,
                    "MULTIVERSE_RUNTIME": runtime,
                },
                clear=False,
            ),
            patch.object(
                server,
                "list_named_agents_in_cwd",
                return_value=list(self.agents),
            ),
        ):
            return server.list_agents()

    def test_shared_registry_contains_wrapped_agents_from_both_runtimes(self) -> None:
        wrapped = [
            sessions.Session(
                id="claude-wrapper",
                cwd=self.cwd,
                created_at=1,
                status="idle",
                status_ts=3,
                name="alpha",
                runtime=sessions.RUNTIME_CLAUDE,
                claude_uuid="claude-thread",
            ),
            sessions.Session(
                id="codex-wrapper",
                cwd=self.cwd,
                created_at=2,
                status="working",
                status_ts=4,
                name="beta",
                runtime=sessions.RUNTIME_CODEX,
                codex_session_id="codex-thread",
            ),
        ]
        with (
            patch.object(sessions, "list_sessions", return_value=wrapped),
            patch.object(sessions, "list_codex_sessions_in", return_value=[]),
            patch.object(
                sessions, "CLAUDE_PROJECTS_DIR", Path(self.temp_dir.name) / "missing"
            ),
            patch.object(sessions.forks, "gc"),
            patch.object(sessions.forks, "active_uuids", return_value=set()),
            patch.object(sessions, "list_locked", return_value=set()),
        ):
            agents = sessions.list_named_agents_in_cwd(self.cwd)

        self.assertEqual(
            {(agent.name, agent.runtime) for agent in agents},
            {
                ("alpha", sessions.RUNTIME_CLAUDE),
                ("beta", sessions.RUNTIME_CODEX),
            },
        )

    def test_claude_caller_sees_codex_agent_without_runtime_column(self) -> None:
        result = self._list_as("alpha", sessions.RUNTIME_CLAUDE)

        self.assertEqual(
            result,
            "2 agent(s) in workspace (2 live)\n"
            "\n"
            "- alpha  idle  ← this is you\n"
            "- beta   working",
        )

    def test_codex_caller_sees_claude_agent_without_runtime_column(self) -> None:
        result = self._list_as("beta", sessions.RUNTIME_CODEX)

        self.assertEqual(
            result,
            "2 agent(s) in workspace (2 live)\n"
            "\n"
            "- beta   working  ← this is you\n"
            "- alpha  idle",
        )

    def test_cross_workspace_listing_suppresses_self_marker_and_runtime(self) -> None:
        other = str((Path(self.temp_dir.name) / "other").resolve())
        Path(other).mkdir()
        agents = [
            sessions.DashboardRow(
                name="remote",
                cwd=other,
                status="dormant",
                status_ts=1,
                runtime=sessions.RUNTIME_CODEX,
            )
        ]
        with (
            patch.dict(
                os.environ,
                {
                    "MULTIVERSE_SESSION_CWD": self.cwd,
                    "MULTIVERSE_SESSION_NAME": "remote",
                    "MULTIVERSE_RUNTIME": sessions.RUNTIME_CLAUDE,
                },
                clear=False,
            ),
            patch.object(
                server, "list_named_agents_in_cwd", return_value=agents
            ),
        ):
            result = server.list_agents(working_directory=other)

        self.assertEqual(
            result,
            f"1 agent(s) in workspace '{other}' (0 live)\n\n"
            "- remote  dormant",
        )


if __name__ == "__main__":
    unittest.main()
