"""``prompt_agent`` must refuse a retiring target, before it writes anything.

``retiring`` reserves an already-dormant transcript while context is generated from it.
Nothing is running, so the risk is not that a prompt fails to arrive -- it is that the
routes beneath the guard SUCCEED. The dormant route resumes the session, disturbing the
transcript being read; pi's route queues a prompt durably against it. Either would
quietly undo the reservation.

That is why these tests assert on the ABSENCE of effects rather than on the error text:
a guard that returned the right message while still resuming the agent would pass a
message-only test. Each case patches all three write routes and requires zero calls.

The status is read twice on purpose. A compaction lock overlays the row's status with
``compacting``, so a retiring record mid-compaction reads as ``compacting`` on the row
while the persisted Session still says ``retiring``. Consulting only the row would
postpone the prompt ("try again later, it's compacting") rather than refuse it, and the
retry would land once the lock cleared -- on a transcript still reserved.

The refusal is transient by design ("try again later"), because the reservation is;
``retired`` is the permanent one.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))

from secondbrain import protocol as p  # noqa: E402

from src import compact_state, sb_actions, sb_connector, sb_queries, sessions  # noqa: E402

# Pinned literal: the wire text Aeon specified. Asserted against the helper once,
# below, so the contract is checked in exactly one place and the connector is then
# checked against the helper itself rather than against a second copy.
RETIRING_TEXT = "This agent is being retired, please try again later"
# A pane the composer parser accepts, so the healthy-agent case reaches delivery.
COMPOSER = "──────────────────── Forge ──\n❯\xa0\n─────────────────────────────\n"
# A rendered permission prompt, so that verb reaches its live-tmux requirement.
PERMISSION_PANE = (
    "│ Do you want to proceed?  │\n"
    "│ ❯ 1. Yes                 │\n"
    "│   2. No                  │\n"
)


class MockWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)


class RetiringPromptRefusalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Restore on teardown: a bare assignment leaks a path that no longer
        # exists into any suite run after this one.
        self.enterContext(
            patch.dict(os.environ, {"MULTIVERSE_ROOT": self.temp.name})
        )
        self.cwd = str(Path(self.temp.name, "workspace").resolve())
        Path(self.cwd).mkdir()
        # Build the connector through its real constructor so a new __init__ attribute
        # can't silently diverge from a hand-listed fixture.
        sb = Path(self.temp.name, "secondbrain")
        sb.mkdir()
        key = ed25519.Ed25519PrivateKey.generate()
        (sb / "device.key").write_bytes(key.private_bytes_raw())
        (sb / "device.key.json").write_text(json.dumps({
            "device_id": "dev",
            "pinned_server_fingerprint": "FP",
            "granted_workspaces": [self.cwd],
            "device_name": "test",
        }))
        (sb / "state.json").write_text(
            json.dumps({"seeded": [], "exposed": [self.cwd]})
        )

    def _row(
        self,
        *,
        row_status: str,
        session_status: str,
        runtime: str = sessions.RUNTIME_CLAUDE,
    ) -> sessions.DashboardRow:
        session = sessions.Session(
            id="w1", cwd=self.cwd, created_at=1, status=session_status, status_ts=2,
            name="Forge", runtime=runtime, host="local-host",
        )
        return sessions.DashboardRow(
            name="Forge", cwd=self.cwd, status=row_status, status_ts=2,
            runtime=runtime, session=session, claude_uuid="u-1",
        )

    async def _prompt(self, row, *, locked: bool = False) -> dict:
        """Drive prompt_agent and report both the reply and every write it attempted."""
        effects: dict = {"typed": [], "resumed": [], "queued": []}

        def run_tmux(args, capture=False, input_text=None):  # noqa: ANN001, ARG001
            if args and args[0] == "capture-pane":
                return True, COMPOSER
            if "-l" in args:
                effects["typed"].append(args[-1])
            return True, ""

        ws = MockWS()
        conn = sb_connector.SBConnector("ws://test", sb_connector.load_identity())
        conn.channel = None
        action = p.Action(
            action_id="a1", ts="2026-09-06T00:00:00Z", workspace_id=f"dev:{self.cwd}",
            kind=p.ActionKind.PROMPT_AGENT, actor="sb", authority="autonomous",
            agent="Forge", payload={"text": "please pick this up"},
        )

        def fake_resume(*a, **k):  # noqa: ANN001, ANN003, ARG001
            effects["resumed"].append(a)

        def fake_write_prompt(*a, **k):  # noqa: ANN001, ANN003, ARG001
            effects["queued"].append(a)

        from src import pi_prompt_queue

        with patch.object(sessions, "find_agent_by_name", return_value=row), \
             patch.object(sessions, "current_host", return_value="local-host"), \
             patch.object(sessions, "tmux_session_exists", return_value=True), \
             patch.object(compact_state, "is_locked", return_value=locked), \
             patch.object(sb_connector.SBConnector, "_auto_resume_with_prompt",
                          fake_resume), \
             patch.object(pi_prompt_queue, "write_prompt", fake_write_prompt), \
             patch.object(sb_actions, "_run_tmux", run_tmux):
            await conn._on_action(ws, action)
            for _ in range(200):
                if ws.sent:
                    break
                await asyncio.sleep(0.02)

        effects["reply"] = json.loads(ws.sent[-1])
        return effects

    def _assert_refused_and_wrote_nothing(self, eff: dict) -> None:
        err = eff["reply"].get("error") or {}
        self.assertEqual(eff["reply"].get("status"), "rejected", eff["reply"])
        # Wired to the shared helper, not to a copy of its text.
        self.assertEqual(err.get("message"), sessions.retiring_agent_message())
        self.assertEqual(err.get("code"), "retiring")
        self.assertEqual(eff["typed"], [], "typed into a retiring agent's terminal")
        self.assertEqual(eff["resumed"], [], "resumed a retiring agent")
        self.assertEqual(eff["queued"], [], "durably queued a prompt for a retiring agent")

    async def test_refuses_a_plainly_retiring_target(self) -> None:
        eff = await self._prompt(
            self._row(row_status="retiring", session_status="retiring")
        )
        self._assert_refused_and_wrote_nothing(eff)

    async def test_refuses_when_compaction_overlays_the_row_status(self) -> None:
        # The row reads `compacting`; only the persisted Session still says retiring.
        # Reading the row alone would answer "try again later, it's compacting" and
        # invite a retry that lands once the lock clears, on a reserved transcript.
        eff = await self._prompt(
            self._row(row_status="compacting", session_status="retiring")
        )
        self._assert_refused_and_wrote_nothing(eff)

    async def test_refuses_ahead_of_the_compaction_lock(self) -> None:
        # Same precedence, but via the lock rather than the status field.
        eff = await self._prompt(
            self._row(row_status="retiring", session_status="retiring"), locked=True
        )
        self._assert_refused_and_wrote_nothing(eff)

    async def test_refuses_instead_of_resuming_a_dormant_looking_row(self) -> None:
        # The dormant route CREATES a session. Without the guard reading the Session,
        # an `ended` row whose record is retiring would be resumed out from under
        # the context generation that reserved it.
        eff = await self._prompt(
            self._row(row_status="ended", session_status="retiring")
        )
        self._assert_refused_and_wrote_nothing(eff)

    async def test_refuses_instead_of_queueing_for_pi(self) -> None:
        # pi's route persists the prompt for later delivery -- the "inbox write" case.
        eff = await self._prompt(
            self._row(
                row_status="retiring", session_status="retiring",
                runtime=sessions.RUNTIME_PI,
            )
        )
        self._assert_refused_and_wrote_nothing(eff)

    async def test_still_delivers_to_a_healthy_agent(self) -> None:
        # The guard must not silence an ordinary target.
        eff = await self._prompt(self._row(row_status="idle", session_status="idle"))
        self.assertEqual(eff["reply"].get("status"), "ok", eff["reply"])
        self.assertIn("please pick this up", eff["typed"])


class RetiringQueryReportingTests(unittest.TestCase):
    """SB's read paths must show the hand-off rather than hiding it."""

    def _row(self, status: str) -> sessions.DashboardRow:
        session = sessions.Session(
            id="w1", cwd="/ws", created_at=1, status=status, status_ts=2,
            name="Forge", runtime=sessions.RUNTIME_CLAUDE, host="local-host",
        )
        return sessions.DashboardRow(
            name="Forge", cwd="/ws", status=status, status_ts=2,
            runtime=sessions.RUNTIME_CLAUDE, session=session, claude_uuid="u-1",
        )

    def test_retiring_survives_the_liveness_correction(self) -> None:
        # Retiring is non-running, so its tmux is legitimately gone. Rewriting the row
        # to `ended` would erase the hand-off from every SB read path at once.
        with patch.object(sessions, "current_host", return_value="local-host"), \
             patch.object(sessions, "tmux_session_exists", return_value=False):
            self.assertEqual(sb_queries._corrected_status(self._row("retiring")),
                             "retiring")

    def test_peek_reports_not_running_rather_than_a_stale_screen(self) -> None:
        with patch.object(sessions, "find_agent_by_name",
                          return_value=self._row("retiring")), \
             patch.object(sessions, "current_host", return_value="local-host"), \
             patch.object(sessions, "tmux_session_exists", return_value=False):
            kind, payload = sb_queries.peek_agent("/ws", "Forge")
        self.assertEqual(kind, "not_running")
        self.assertIn("retiring", payload["message"])


class RetiringAnswerVerbAuditTests(unittest.TestCase):
    """The answer verbs need no ``retiring`` guard of their own -- but only because
    each requires a live tmux BEFORE it writes, and a retiring record is dormant.

    That is a property of code they don't own, so it is pinned here rather than left
    as a note: if either check were ever moved below its send-keys, both verbs would
    start driving a terminal for a reserved transcript and nothing would say so.
    """

    QUESTIONS = [{
        "question": "Pick",
        "multiSelect": False,
        "options": [{"label": "Alpha"}, {"label": "Beta"}],
    }]

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Restore on teardown: a bare assignment leaks a path that no longer
        # exists into any suite run after this one.
        self.enterContext(
            patch.dict(os.environ, {"MULTIVERSE_ROOT": self.temp.name})
        )
        self.cwd = str(Path(self.temp.name, "workspace").resolve())
        Path(self.cwd).mkdir()
        self.conn = object.__new__(sb_connector.SBConnector)

    def _retiring_row(self) -> sessions.DashboardRow:
        # Retiring == an already-dormant transcript, so its tmux is legitimately gone.
        session = sessions.Session(
            id="w1", cwd=self.cwd, created_at=1, status="retiring", status_ts=2,
            name="Forge", runtime=sessions.RUNTIME_CLAUDE, host="local-host",
        )
        return sessions.DashboardRow(
            name="Forge", cwd=self.cwd, status="retiring", status_ts=2,
            runtime=sessions.RUNTIME_CLAUDE, session=session, claude_uuid="u-1",
        )

    def _run(self, verb: str):
        # Count EVERY tmux invocation except the read-only capture. An AskUserQuestion
        # answer is delivered as key presses and never sends literal text, so recording
        # only `-l` calls would make this assertion unfalsifiable -- it would hold even
        # while the modal was being driven.
        drove: list[list[str]] = []

        def run_tmux(args, capture=False, input_text=None):  # noqa: ANN001, ARG001
            if args and args[0] == "capture-pane":
                return True, PERMISSION_PANE if verb == "permission" else COMPOSER
            drove.append(list(args))
            return True, ""

        from src import sb_notify

        # Each verb must see a pending record of ITS OWN kind, or it refuses for an
        # unrelated reason and the live-tmux requirement never gets exercised.
        pending = (
            {"tool_use_id": "t1", "tool_name": "Bash", "agent": "Forge",
             "cwd": self.cwd, "tool_input": {"command": "ls"}}
            if verb == "permission" else
            {"tool_use_id": "t1", "tool_name": "AskUserQuestion", "agent": "Forge",
             "cwd": self.cwd, "tool_input": {"questions": self.QUESTIONS}}
        )
        with patch.object(sessions, "find_agent_by_name",
                          return_value=self._retiring_row()), \
             patch.object(sessions, "current_host", return_value="local-host"), \
             patch.object(sessions, "tmux_session_exists", return_value=False), \
             patch.object(compact_state, "is_locked", return_value=False), \
             patch.object(sb_notify, "pending_tool_data", return_value=pending), \
             patch.object(sb_actions, "_run_tmux", run_tmux):
            if verb == "answer":
                out = self.conn._deliver_answer_blocking(
                    self.cwd, "Forge", "t1", ["Alpha"],
                )
            else:
                out = self.conn._deliver_permission_blocking(
                    self.cwd, "Forge", "t1", "deny", "",
                )
        return out, drove

    def test_answer_question_refuses_and_drives_nothing(self) -> None:
        (status, _ok, err), drove = self._run("answer")
        self.assertNotEqual(status, "ok", err)
        self.assertEqual(drove, [], "drove a modal for a reserved transcript")

    def test_answer_permission_prompt_refuses_and_drives_nothing(self) -> None:
        (status, _ok, err), drove = self._run("permission")
        self.assertNotEqual(status, "ok", err)
        self.assertEqual(drove, [], "drove a modal for a reserved transcript")


class RetiringMessageContractTests(unittest.TestCase):
    def test_helper_returns_the_agreed_wire_text(self) -> None:
        # Pinned once, here. Everything else asserts against the helper, so the text
        # exists in exactly one place that can drift.
        self.assertEqual(sessions.retiring_agent_message(), RETIRING_TEXT)


if __name__ == "__main__":
    unittest.main()
