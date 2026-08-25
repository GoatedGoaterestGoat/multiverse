"""Denied permission prompts must close their pair.

Claude Code fires no hook when a user denies a permission prompt. Three events look
like they should and none does: ``PostToolUse`` runs only after a *successful* tool,
``PostToolUseFailure`` sits in the catch around tool execution (the denial path
returns before it), and ``PermissionDenied`` is gated on auto-mode classifier
denials rather than a human answering the modal.

So an allow emitted ``user.permission_decision`` and a deny emitted nothing at all,
leaving ``agent.permission_request`` unclosed forever. From SB's side a denied prompt
was indistinguishable from one still waiting on a human -- and denial is the case it
most needs to know about.

Detection is by absence: PostToolUse deletes the ``.perm`` marker when a tool runs,
so a marker surviving to Stop means the tool never ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src import sb_notify


class DeniedPermissionSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        os.environ["MULTIVERSE_ROOT"] = self.temp.name
        self.addCleanup(os.environ.pop, "MULTIVERSE_ROOT", None)
        root = Path(self.temp.name)
        sb = root / "secondbrain"
        sb.mkdir(parents=True)
        self.cwd = str((root / "ws").resolve())
        Path(self.cwd).mkdir()
        (sb / "device.key.json").write_text(
            json.dumps({"device_id": "dev", "granted_workspaces": [self.cwd]}),
            encoding="utf-8",
        )
        (sb / "state.json").write_text(
            json.dumps({"seeded": [f"dev:{self.cwd}"], "exposed": [self.cwd]}),
            encoding="utf-8",
        )

    def _prompt(self, tool_use_id: str, agent: str = "Alice") -> None:
        sb_notify.record_pending_tool(
            self.cwd, agent, tool_use_id, "Bash", {"command": f"echo {tool_use_id}"}
        )
        sb_notify.mark_permission_requested(tool_use_id)

    def _decisions(self) -> list[tuple[str, str]]:
        d = sb_notify.permission_decision_signals_dir()
        if not d.is_dir():
            return []
        out = []
        for p in d.glob("*.json"):
            rec = json.loads(p.read_text(encoding="utf-8"))
            out.append((rec["tool_use_id"], rec["decision"]))
        return sorted(out)

    def test_denied_prompt_is_closed_and_allowed_one_is_untouched(self) -> None:
        self._prompt("t_denied")
        self._prompt("t_allowed")
        # The allowed one completes, so PostToolUse fires and clears its marker.
        sb_notify.record_permission_decision_signal(
            self.cwd, "Alice", "t_allowed", "allow", "Bash", {}, "ok"
        )
        sb_notify.cleanup_pending_tool("t_allowed")

        self.assertEqual(sb_notify.sweep_unresolved_permissions(self.cwd, "Alice"), 1)
        self.assertEqual(
            self._decisions(), [("t_allowed", "allow"), ("t_denied", "deny")]
        )

    def test_does_not_close_another_agents_prompt(self) -> None:
        self._prompt("t_mine", agent="Alice")
        self._prompt("t_theirs", agent="Bob")
        sb_notify.sweep_unresolved_permissions(self.cwd, "Alice")
        self.assertEqual(self._decisions(), [("t_mine", "deny")])

    def test_is_idempotent_across_repeated_stops(self) -> None:
        self._prompt("t_once")
        self.assertEqual(sb_notify.sweep_unresolved_permissions(self.cwd, "Alice"), 1)
        self.assertEqual(sb_notify.sweep_unresolved_permissions(self.cwd, "Alice"), 0)
        self.assertEqual(self._decisions(), [("t_once", "deny")])

    def test_respects_the_exposed_gate(self) -> None:
        (Path(self.temp.name) / "secondbrain" / "state.json").write_text(
            json.dumps({"seeded": [f"dev:{self.cwd}"], "exposed": []}), encoding="utf-8"
        )
        self._prompt("t_gated")
        sb_notify.sweep_unresolved_permissions(self.cwd, "Alice")
        self.assertEqual(self._decisions(), [])

    def test_stop_hook_runs_the_sweep(self) -> None:
        self._prompt("t_via_hook")
        env = {
            **os.environ,
            "MULTIVERSE_SESSION_CWD": self.cwd,
            "MULTIVERSE_SESSION_NAME": "Alice",
            "MULTIVERSE_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
        }
        script = Path(__file__).resolve().parents[1] / "scripts" / "sb-emit-response.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps({"hook_event_name": "Stop"}),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decisions(), [("t_via_hook", "deny")])


if __name__ == "__main__":
    unittest.main()
