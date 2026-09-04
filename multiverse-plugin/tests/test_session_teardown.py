from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import session_teardown, sessions


class SessionTeardownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "sessions"
        self.session_dir = self.state_dir / "wrapper-id"
        self.session_dir.mkdir(parents=True)
        (self.session_dir / "meta.json").write_text(
            json.dumps(
                {
                    "cwd": "/workspace",
                    "created_at": 1,
                    "runtime": sessions.RUNTIME_CODEX,
                    "host": sessions.current_host(),
                    "name": "agent",
                }
            )
        )
        (self.session_dir / "status.json").write_text(
            json.dumps({"status": "ending", "ts": 2})
        )
        (self.session_dir / "ending").touch()

    def _patch_paths(self):
        return (
            patch.object(sessions, "MULTIVERSE_DIR", self.root),
            patch.object(sessions, "STATE_DIR", self.state_dir),
        )

    def test_finishes_only_durable_ending_request(self) -> None:
        with self._patch_paths()[0], self._patch_paths()[1], patch.object(
            sessions, "kill_session"
        ) as kill:
            result = session_teardown.finish_ending_session("wrapper-id")

        self.assertEqual(result, 0)
        kill.assert_called_once()
        self.assertEqual(kill.call_args.args[0].id, "wrapper-id")

    def test_ignores_session_no_longer_ending(self) -> None:
        (self.session_dir / "status.json").write_text(
            json.dumps({"status": "idle", "ts": 3})
        )
        with self._patch_paths()[0], self._patch_paths()[1], patch.object(
            sessions, "kill_session"
        ) as kill:
            result = session_teardown.finish_ending_session("wrapper-id")

        self.assertEqual(result, 0)
        kill.assert_not_called()

    def test_persists_teardown_failure_for_dashboard(self) -> None:
        with self._patch_paths()[0], self._patch_paths()[1], patch.object(
            sessions, "kill_session", side_effect=RuntimeError("signal failed")
        ):
            result = session_teardown.finish_ending_session("wrapper-id")

        self.assertEqual(result, 1)
        payload = json.loads(
            (self.session_dir / "ending_error.json").read_text()
        )
        self.assertEqual(payload["error"], "signal failed")


if __name__ == "__main__":
    unittest.main()
