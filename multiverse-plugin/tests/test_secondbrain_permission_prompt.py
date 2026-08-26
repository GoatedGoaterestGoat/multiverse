"""SecondBrain ``answer_permission_prompt`` — parser, target resolution, handler.

This verb approves work the user's own settings flagged for a human, so the tests
lean on the cases where a wrong keystroke would approve something. Two properties
matter more than the happy path:

* deny must never land on the "don't ask again" row, which sits between Yes and No
  and only renders sometimes;
* anything the pane doesn't clearly say must produce a refusal with NO keystrokes
  sent, rather than a hopeful guess.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))

from secondbrain import protocol as p  # noqa: E402

from src import compact_state, sb_actions, sb_connector, sb_notify, sessions  # noqa: E402
from src.sb_actions import (  # noqa: E402
    _codex_permission_target,
    _permission_target,
    parse_codex_permission_options,
    parse_permission_options,
)

LABELS = ["Yes", "Yes, and don't ask again for rm commands in /r", "No"]
GONE = "agent output, no modal\n"


class FakeTmux:
    """A permission modal that tracks its own cursor.

    Honours the keys the driver actually sends: a digit selects, Down/Up move, Tab
    enters input mode, Enter submits. ``sticky`` keeps the modal up after answering
    so the post-condition check can be exercised.
    """

    def __init__(self, *, sticky: bool = False) -> None:
        self.sent: list[list[str]] = []
        self.focus = 1
        self.done = False
        self.sticky = sticky
        self.input_mode = False
        self.typed = ""

    def pane(self) -> str:
        if self.done and not self.sticky:
            return GONE
        rows = ["│ Do you want to proceed?   │"]
        for i, label in enumerate(LABELS, 1):
            cursor = "❯" if i == self.focus else " "
            text = (
                f"{label}: {self.typed}"
                if self.input_mode and i == self.focus and self.typed
                else label
            )
            rows.append(f"│ {cursor} {i}. {text}   │")
        return "\n".join(rows) + "\n"

    def run(self, args, capture=False, input_text=None):  # noqa: ANN001, ARG002
        if args[0] == "capture-pane":
            return True, self.pane()
        self.sent.append(args)
        key = args[-1]
        if "-l" in args:
            if key.isdigit():
                self.focus = int(key)
                self.done = True
            else:
                self.typed += key
        elif key == "Down":
            self.focus = min(len(LABELS), self.focus + 1)
        elif key == "Up":
            self.focus = max(1, self.focus - 1)
        elif key == "Tab":
            self.input_mode = True
        elif key == "Enter":
            self.done = True
        return True, ""

    @property
    def keys(self) -> list[str]:
        return [a[-1] for a in self.sent]


class NoModalTmux(FakeTmux):
    def pane(self) -> str:
        return GONE


CODEX_LABELS = [
    "Yes, proceed (y)",
    "Yes, and don't ask again for commands that start with `rm build` (p)",
    "No, and tell Codex what to do differently (esc)",
]


class FakeCodexTmux:
    """Codex approval overlay followed by its normal focused composer."""

    def __init__(self, *, sticky: bool = False, draft: str = "") -> None:
        self.sent: list[list[str]] = []
        self.focus = 1
        self.done = False
        self.sticky = sticky
        self.draft = draft
        self.buffer = ""
        self.submitted: list[str] = []

    def pane(self) -> str:
        if self.done and not self.sticky:
            return (
                "agent output\n\n"
                f"› {self.draft}\n"
                "  gpt-test · Ready · Context 99% used\n"
            )
        rows = ["  Would you like to run the following command?", ""]
        for index, label in enumerate(CODEX_LABELS, 1):
            cursor = "›" if index == self.focus else " "
            rows.append(f"{cursor} {index}. {label}")
        rows.extend(["", "  Press enter to confirm or esc to cancel"])
        return "\n".join(rows) + "\n"

    def run(self, args, capture=False, input_text=None):  # noqa: ANN001, ARG002
        command = args[0]
        if command == "capture-pane":
            return True, self.pane()
        if command == "display-message":
            return True, "2 1\n"
        if command == "load-buffer":
            self.buffer = input_text or ""
            self.sent.append(args)
            return True, ""
        if command == "paste-buffer":
            self.draft += self.buffer
            self.sent.append(args)
            return True, ""

        self.sent.append(args)
        keys = args[3:] if len(args) > 3 else [args[-1]]
        for key in keys:
            if key == "Down":
                self.focus = min(len(CODEX_LABELS), self.focus + 1)
            elif key == "Up":
                self.focus = max(1, self.focus - 1)
            elif key == "BSpace":
                self.draft = self.draft[:-1]
            elif key == "Enter":
                if not self.done:
                    self.done = True
                else:
                    self.submitted.append(self.draft)
                    self.draft = ""
        return True, ""

    @property
    def keys(self) -> list[str]:
        return [args[-1] for args in self.sent if args[0] == "send-keys"]


class NoComposerAfterCodexTmux(FakeCodexTmux):
    def pane(self) -> str:
        if self.done and not self.sticky:
            return "agent output, approval gone but composer not focused\n"
        return super().pane()


class MockWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class PermissionParseTests(unittest.TestCase):
    def test_parses_rows_and_cursor(self) -> None:
        pane = (
            "│ Do you want to proceed?  │\n"
            "│ ❯ 1. Yes                 │\n"
            "│   2. Yes, and don't ask again for rm commands in /r │\n"
            "│   3. No                  │\n"
        )
        options, focused = parse_permission_options(pane)
        self.assertEqual([label for _, label in options], LABELS)
        self.assertEqual(focused, 1)

    def test_ignores_numbered_list_in_scrollback(self) -> None:
        pane = (
            "  Here is my plan:\n"
            "  1. refactor the parser\n"
            "  2. add tests\n"
            "\n"
            "│ ❯ 1. Yes │\n"
            "│   2. No  │\n"
        )
        options, focused = parse_permission_options(pane)
        self.assertEqual([label for _, label in options], ["Yes", "No"])
        self.assertEqual(focused, 1)

    def test_refuses_when_no_modal_or_gappy_numbering(self) -> None:
        self.assertEqual(parse_permission_options(GONE), ([], None))
        self.assertEqual(parse_permission_options("  1. Yes\n  3. No\n"), ([], None))

    def test_deny_never_resolves_to_the_always_allow_row(self) -> None:
        three = list(enumerate(LABELS, 1))
        self.assertEqual(_permission_target(three, "allow"), 1)
        self.assertEqual(_permission_target(three, "deny"), 3)
        two = [(1, "Yes"), (2, "No")]
        self.assertEqual(_permission_target(two, "deny"), 2)

    def test_refuses_unrecognised_option_sets(self) -> None:
        self.assertIsNone(_permission_target([(1, "Nope"), (2, "No")], "allow"))
        self.assertIsNone(_permission_target([(1, "Yes"), (2, "Yes, always")], "deny"))

    def test_refuses_exit_plan_mode_which_has_no_plain_yes_or_no(self) -> None:
        # ExitPlanMode offers only elevated-mode variants. A prefix rule would read
        # row 1 as an allow and silently switch the agent into bypass-permissions,
        # and would deny by picking Ultraplan (a web round-trip) over "keep
        # planning". Neither decision is expressible here, so both must refuse.
        exit_plan = [
            (1, "Yes, clear context (12% used) and bypass permissions"),
            (2, "Yes, and bypass permissions"),
            (3, "Yes, manually approve edits"),
            (4, "No, refine with Ultraplan on Claude Code on the web"),
            (5, "No, keep planning"),
        ]
        self.assertIsNone(_permission_target(exit_plan, "allow"))
        self.assertIsNone(_permission_target(exit_plan, "deny"))

    def test_matches_through_an_inline_description(self) -> None:
        # Rows render with inline descriptions/placeholders after a run of spaces;
        # the label's own words never contain one.
        rows = [(1, "Yes"), (2, "No   Tell Claude what to do differently")]
        self.assertEqual(_permission_target(rows, "deny"), 2)

    def test_parses_codex_overlay_and_chooses_only_one_time_options(self) -> None:
        pane = FakeCodexTmux().pane()
        options, focused = parse_codex_permission_options(pane)
        self.assertEqual([label for _, label in options], CODEX_LABELS)
        self.assertEqual(focused, 1)
        self.assertEqual(_codex_permission_target(options, "allow"), 1)
        self.assertEqual(_codex_permission_target(options, "deny"), 3)

    def test_codex_parser_requires_approval_title_and_refuses_standing_grants(self) -> None:
        transcript = "› 1. Yes, proceed (y)\n  2. No (n)\n"
        self.assertEqual(parse_codex_permission_options(transcript), ([], None))
        standing_only = [
            (1, "Yes, and don't ask again for this command (a)"),
            (2, "No, and block this host in the future (d)"),
        ]
        self.assertIsNone(_codex_permission_target(standing_only, "allow"))
        self.assertIsNone(_codex_permission_target(standing_only, "deny"))

    def test_codex_parser_supports_mcp_approval_without_persistent_choice(self) -> None:
        pane = (
            "  Tool call needs your approval. Reason: Requires repository access\n\n"
            "› 1. Allow   Run the tool and continue.\n"
            "  2. Allow for this session   Remember this choice for this session.\n"
            "  3. Cancel   Cancel this tool call\n"
        )
        options, focused = parse_codex_permission_options(pane)
        self.assertEqual(focused, 1)
        self.assertEqual(_codex_permission_target(options, "allow"), 1)
        self.assertEqual(_codex_permission_target(options, "deny"), 3)

    def test_codex_parser_recognizes_each_supported_approval_surface(self) -> None:
        surfaces = [
            (
                "Would you like to make the following edits?",
                "Yes, proceed (y)",
                "No, and tell Codex what to do differently (esc)",
            ),
            (
                "Would you like to grant these permissions?",
                "Yes, grant these permissions for this turn (y)",
                "No, continue without permissions (esc)",
            ),
            (
                "Do you want to approve network access to api.example.com?",
                "Yes, just this once (y)",
                "No (esc)",
            ),
            (
                "Approve app tool call?",
                "Allow   Run the tool and continue.",
                "Cancel   Cancel this tool call",
            ),
        ]
        for title, allow, deny in surfaces:
            with self.subTest(title=title):
                pane = f"  {title}\n\n› 1. {allow}\n  2. {deny}\n"
                options, focused = parse_codex_permission_options(pane)
                self.assertEqual(focused, 1)
                self.assertEqual(_codex_permission_target(options, "allow"), 1)
                self.assertEqual(_codex_permission_target(options, "deny"), 2)

    def test_codex_parser_joins_visually_wrapped_safe_label(self) -> None:
        pane = (
            "  Would you like to run the following command?\n\n"
            "› 1. Yes, proceed (y)\n"
            "  2. Yes, and don't ask again for commands that\n"
            "     start with `rm build` (p)\n"
            "  3. No, and tell Codex what to do\n"
            "     differently (esc)\n\n"
            "  Press enter to confirm or esc to cancel\n"
        )
        options, focused = parse_codex_permission_options(pane)
        self.assertEqual(focused, 1)
        self.assertEqual(_codex_permission_target(options, "allow"), 1)
        self.assertEqual(_codex_permission_target(options, "deny"), 3)


class PermissionHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        os.environ["MULTIVERSE_ROOT"] = self.temp.name
        self.cwd = str(Path(self.temp.name, "workspace").resolve())
        Path(self.cwd).mkdir()
        self.wsid = f"dev:{self.cwd}"
        self.row = sessions.DashboardRow(
            name="Forge", cwd=self.cwd, status="waiting", status_ts=1,
            session=SimpleNamespace(tmux_name="mv-forge"), claude_uuid="u-1",
            preview="", is_ghost=False, folded_at=None,
        )
        # The driver's real pacing is irrelevant here and would only slow the suite.
        for attr in ("_KEY_DELAY_S", "_CLAUDE_SUBMIT_DELAY_S"):
            patcher = patch.object(sb_actions, attr, 0.0)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _run(
        self, payload, *, row=..., tmux=None, locked=False,
        pending=None, wsid=None,
    ):
        row = self.row if row is ... else row
        if pending is None:
            pending = {"agent": "Forge"}
        elif pending == "__none__":
            pending = None
        fake = tmux or FakeTmux()
        ws = MockWS()
        conn = object.__new__(sb_connector.SBConnector)
        conn.identity = SimpleNamespace(device_id="dev", granted_workspaces=[self.cwd])
        conn.dedup = sb_connector.ActionDedup()
        conn.channel = None
        action = p.Action(
            action_id="a1", ts="2026-08-25T00:00:00Z", workspace_id=wsid or self.wsid,
            kind=p.ActionKind.ANSWER_PERMISSION_PROMPT, actor="sb",
            authority="autonomous", agent="Forge", payload=payload,
        )
        with patch.object(
            sessions, "find_agent_by_name",
            lambda c, n: row if row and n == row.name else None,
        ), patch.object(sb_actions, "_run_tmux", fake.run), patch(
            "src.sessions.tmux_session_exists", lambda n: True
        ), patch("src.compact_state.is_locked", lambda u: locked), patch.object(
            sb_notify, "pending_tool_data", lambda tid: pending
        ):
            await conn._on_action(ws, action)
            for _ in range(300):
                if ws.sent:
                    break
                await asyncio.sleep(0.02)
        self.assertTrue(ws.sent, "handler produced no ActionResult")
        return ws.sent[-1], fake

    async def test_allow_types_the_yes_digit(self) -> None:
        result, fake = await self._run({"tool_use_id": "t1", "decision": "allow"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("Yes", json.dumps(result))
        self.assertEqual(fake.keys, ["1"])

    async def test_deny_skips_the_always_allow_row(self) -> None:
        result, fake = await self._run({"tool_use_id": "t1", "decision": "deny"})
        self.assertEqual(result["status"], "ok")
        # Row 2 is "Yes, and don't ask again" — landing there would silently widen
        # what this agent may do from now on, while reporting a denial.
        self.assertEqual(fake.keys, ["3"])

    async def test_feedback_navigates_then_types(self) -> None:
        result, fake = await self._run(
            {"tool_use_id": "t1", "decision": "deny", "feedback": "use --dry-run first"}
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            fake.keys, ["Down", "Down", "Tab", "use --dry-run first", "Enter"]
        )

    async def test_codex_allow_confirms_explicit_one_time_option(self) -> None:
        row = sessions.DashboardRow(
            name="Forge",
            cwd=self.cwd,
            status="waiting",
            status_ts=1,
            runtime=sessions.RUNTIME_CODEX,
            session=SimpleNamespace(tmux_name="mv-forge"),
            session_id="thread-1",
        )
        result, fake = await self._run(
            {"tool_use_id": "t1", "decision": "allow"},
            row=row,
            tmux=FakeCodexTmux(),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(fake.keys, ["Enter"])
        self.assertIn("Yes, proceed", json.dumps(result))

    async def test_codex_deny_then_feedback_uses_focused_composer(self) -> None:
        row = sessions.DashboardRow(
            name="Forge",
            cwd=self.cwd,
            status="waiting",
            status_ts=1,
            runtime=sessions.RUNTIME_CODEX,
            session=SimpleNamespace(tmux_name="mv-forge"),
            session_id="thread-1",
        )
        fake = FakeCodexTmux(draft="unfinished user draft")
        result, fake = await self._run(
            {
                "tool_use_id": "t1",
                "decision": "deny",
                "feedback": "use --dry-run first",
            },
            row=row,
            tmux=fake,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(fake.keys[:3], ["Down", "Down", "Enter"])
        self.assertEqual(fake.submitted, ["use --dry-run first"])
        self.assertEqual(fake.draft, "unfinished user draft")

    async def test_codex_feedback_fails_closed_without_focused_composer(self) -> None:
        row = sessions.DashboardRow(
            name="Forge",
            cwd=self.cwd,
            status="waiting",
            status_ts=1,
            runtime=sessions.RUNTIME_CODEX,
            session=SimpleNamespace(tmux_name="mv-forge"),
            session_id="thread-1",
        )
        result, fake = await self._run(
            {
                "tool_use_id": "t1",
                "decision": "deny",
                "feedback": "use --dry-run first",
            },
            row=row,
            tmux=NoComposerAfterCodexTmux(),
        )
        self.assertEqual(result["error"]["code"], "deliver_failed")
        self.assertEqual(fake.keys, ["Down", "Down", "Enter"])
        self.assertEqual(fake.submitted, [])

    async def test_refuses_without_sending_keys_when_no_prompt_is_up(self) -> None:
        result, fake = await self._run({"decision": "deny"}, tmux=NoModalTmux())
        self.assertEqual(result["error"]["code"], "no_pending_permission")
        self.assertEqual(fake.sent, [], "must not touch a pane it can't read")

    async def test_reports_failure_when_prompt_survives(self) -> None:
        result, _ = await self._run(
            {"decision": "allow"}, tmux=FakeTmux(sticky=True)
        )
        self.assertEqual(result["error"]["code"], "deliver_failed")

    async def test_rejects_bad_input_and_stale_or_foreign_handles(self) -> None:
        # MISSING is distinct from None here: None means "use the default pending
        # record", MISSING means "no record exists", which is the stale case.
        MISSING = object()
        cases = [
            ("unknown agent", {"decision": "allow"}, {"row": None}, "not_found"),
            ("bad decision", {"decision": "sideways"}, {}, "bad_request"),
            ("compacting", {"decision": "allow"}, {"locked": True}, "locked"),
            ("handle already answered",
             {"tool_use_id": "x", "decision": "allow"}, {"pending": MISSING},
             "stale_prompt"),
            ("handle owned by another agent",
             {"tool_use_id": "x", "decision": "allow"},
             {"pending": {"agent": "Other"}}, "stale_prompt"),
            ("ungranted workspace", {"decision": "allow"},
             {"wsid": "dev:/tmp/elsewhere"}, "forbidden"),
        ]
        for name, payload, kwargs, code in cases:
            with self.subTest(case=name):
                if kwargs.get("pending") is MISSING:
                    kwargs = {**kwargs, "pending": "__none__"}
                result, fake = await self._run(payload, **kwargs)
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(fake.sent, [], "a rejected action must send no keys")


class PromptAgentModalGuardTests(unittest.IsolatedAsyncioTestCase):
    """``prompt_agent`` must not type into a modal, even when status says otherwise.

    ``waiting`` is refused up front, but status is a snapshot: a target can raise a
    modal between that read and the keystrokes. Typing into one isn't a benign miss --
    the modal reads digits as selections, so an ordinary prompt containing a "1" picks
    option 1, which on some prompts is an elevated-mode option. No intent required,
    which is what makes the second check worth its cost.
    """

    MODAL = (
        "│ Do you want to proceed?  │\n"
        "│ ❯ 1. Yes                 │\n"
        "│   2. No                  │\n"
    )
    COMPOSER = "──────────────────── Forge ──\n❯\xa0\n─────────────────────────────\n"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        os.environ["MULTIVERSE_ROOT"] = self.temp.name
        self.cwd = str(Path(self.temp.name, "workspace").resolve())
        Path(self.cwd).mkdir()

    def _target(self) -> sessions.DashboardRow:
        session = sessions.Session(
            id="w1", cwd=self.cwd, created_at=1, status="idle", status_ts=2,
            name="Forge", runtime=sessions.RUNTIME_CLAUDE, host="local-host",
        )
        return sessions.DashboardRow(
            name="Forge", cwd=self.cwd, status="idle", status_ts=2,
            runtime=sessions.RUNTIME_CLAUDE, session=session, claude_uuid="u-1",
        )

    async def _deliver(self, pane: str):
        sent: list[list[str]] = []

        def run(args, capture=False, input_text=None):  # noqa: ANN001, ARG001
            if args[0] == "capture-pane":
                return True, pane
            sent.append(args)
            return True, ""

        ws = MockWS()
        conn = object.__new__(sb_connector.SBConnector)
        conn.identity = SimpleNamespace(device_id="dev", granted_workspaces=[self.cwd])
        conn.dedup = sb_connector.ActionDedup()
        conn.channel = None
        action = p.Action(
            action_id="a1", ts="2026-08-25T00:00:00Z", workspace_id=f"dev:{self.cwd}",
            kind=p.ActionKind.PROMPT_AGENT, actor="sb", authority="autonomous",
            agent="Forge", payload={"text": "try running step 1 of the plan"},
        )
        with patch.object(sessions, "find_agent_by_name", return_value=self._target()),              patch.object(sessions, "current_host", return_value="local-host"),              patch.object(sessions, "tmux_session_exists", return_value=True),              patch.object(compact_state, "is_locked", return_value=False),              patch.object(sb_actions, "_run_tmux", run):
            await conn._on_action(ws, action)
            for _ in range(200):
                if ws.sent:
                    break
                await asyncio.sleep(0.02)
        typed = [a[-1] for a in sent if "-l" in a]
        return ws.sent[-1], typed

    async def test_refuses_and_types_nothing_when_a_modal_replaced_the_composer(self) -> None:
        result, typed = await self._deliver(self.MODAL)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(typed, [], "a prompt's digits would have selected options")

    async def test_still_delivers_when_the_composer_is_present(self) -> None:
        result, typed = await self._deliver(self.COMPOSER)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(typed)


if __name__ == "__main__":
    unittest.main()
