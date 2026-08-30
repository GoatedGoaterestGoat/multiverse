"""Claude push-notification delivery is gated on the composer, not attachment.

The pane fixtures here are copied from real ``tmux capture-pane -p -J -e`` output of
live Claude Code sessions, including the SGR codes — in particular the dim (SGR 2)
span Claude uses for ghost/placeholder text, which is what makes the
"is this a real draft?" question non-trivial.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from src import sb_actions, server, sessions

RULE = "─" * 97


def claude_pane(draft: str = "", *, ghost: bool = False, name: str = "agent") -> str:
    """A Claude pane whose composer holds ``draft``.

    ``ghost=True`` wraps the text in SGR 2 the way Claude renders its dim
    tab-to-accept suggestion — visually identical to a draft under a plain capture.
    """
    body = f"\x1b[2m{draft}\x1b[0m" if ghost else draft
    return "\n".join(
        [
            "  Some earlier transcript output",
            "",
            f"\x1b[38;5;37m{'─' * 80} {name} ──",
            f"\x1b[39m❯\xa0{body}",
            f"\x1b[38;5;37m{RULE}",
            "\x1b[39m  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent",
        ]
    )


def claude_pane_ghost_reset_on_next_line(ghost: str = "Pull on the remote") -> str:
    """Real-world shape captured from a live agent (GOAT).

    Claude opens the dim span on the marker line but emits the closing ``\\x1b[0m`` at
    the START of the following line (the closing rule). A DOTALL dim-strip eats the
    newline along with it, welding the marker line onto the rule and making the
    composer look absent — which silently degraded every push to queued delivery.
    """
    return "\n".join(
        [
            "  earlier transcript output",
            "",
            f"\x1b[38;5;37m{RULE}",
            f"\x1b[39m❯\xa0\x1b[2m{ghost}",
            f"\x1b[0m\x1b[38;5;37m{RULE}",
            "\x1b[39m  ⏵⏵ auto mode on (shift+tab to cycle)",
        ]
    )


def modal_pane() -> str:
    """A pane with a permission prompt up — the composer is gone."""
    return "\n".join(
        [
            "  Bash command",
            "  rm -rf build/",
            "",
            "  Do you want to proceed?",
            "  ❯ 1. Yes",
            "    2. No, and tell Claude what to do differently",
        ]
    )


class ClaudeInputBoxDetectionTests(unittest.TestCase):
    def test_empty_composer_is_visible_with_no_draft(self) -> None:
        self.assertEqual(sb_actions.claude_input_box(claude_pane()), (True, ""))

    def test_real_draft_is_captured(self) -> None:
        visible, draft = sb_actions.claude_input_box(claude_pane("fix the parser"))
        self.assertTrue(visible)
        self.assertEqual(draft, "fix the parser")

    def test_dim_ghost_text_is_not_a_draft(self) -> None:
        """Regression: ghost text restored as literal input corrupts the session."""
        visible, draft = sb_actions.claude_input_box(
            claude_pane("Restart sb and check the tunnel", ghost=True)
        )
        self.assertTrue(visible)
        self.assertEqual(draft, "")

    def test_ghost_whose_reset_lands_on_the_next_line(self) -> None:
        """Regression: the composer must stay visible, and the ghost is not a draft.

        Live bug this reproduces — a DOTALL dim-strip consumed the newline before the
        reset, welding ``❯`` onto the closing rule. The weld itself scored as a rule
        line, so the region between the two rules came back empty and the composer
        read as ABSENT: every push notification to that agent fell back to queued.
        """
        pane = claude_pane_ghost_reset_on_next_line("Pull on the remote and restart")
        self.assertEqual(sb_actions.claude_input_box(pane), (True, ""))

    def test_ghost_reset_next_line_still_delivers(self) -> None:
        """The end-to-end consequence: this pane must be deliverable, not queued."""
        pane = claude_pane_ghost_reset_on_next_line()
        visible, _ = sb_actions.claude_input_box(pane)
        self.assertTrue(visible, "composer must be visible or delivery degrades")

    def test_queued_messages_placeholder_is_not_a_draft(self) -> None:
        """Claude shows this whenever prompts are queued mid-turn.

        Captured verbatim from a live session: the marker, then a dim span
        holding the placeholder. Treating it as a draft means typing "Press up
        to edit queued messages" back into the box after every delivery.
        """
        pane = "\n".join(
            [
                f"\x1b[38;5;244m{RULE}",
                "\x1b[38;5;246m❯\xa0\x1b[2m\x1b[39mPress up to edit queued messages\x1b[0m",
                f"\x1b[38;5;244m{RULE}",
                "\x1b[39m  ⏵⏵ auto mode on (shift+tab to cycle)",
            ]
        )
        self.assertEqual(sb_actions.claude_input_box(pane), (True, ""))

    def test_placeholder_ignored_even_without_style_codes(self) -> None:
        """The dim strip needs ``-e``; the text match must not.

        A caller that captures without styles sees the placeholder as ordinary
        glyphs. Before the literal-text fallback that read back as a draft, so
        the protection hung entirely on one flag surviving in every call site.
        """
        pane = "\n".join(
            [
                RULE,
                "❯\xa0Press up to edit queued messages",
                RULE,
                "  ⏵⏵ auto mode on (shift+tab to cycle)",
            ]
        )
        self.assertEqual(sb_actions.claude_input_box(pane), (True, ""))

    def test_placeholder_match_is_case_insensitive(self) -> None:
        pane = "\n".join([RULE, "❯\xa0PRESS UP TO EDIT QUEUED MESSAGES", RULE, "  x"])
        self.assertEqual(sb_actions.claude_input_box(pane)[1], "")

    def test_a_real_draft_is_still_preserved_without_styles(self) -> None:
        """The fallback must not swallow genuine typed text."""
        pane = "\n".join([RULE, "❯\xa0press up and check the logs", RULE, "  x"])
        self.assertEqual(
            sb_actions.claude_input_box(pane), (True, "press up and check the logs")
        )

    def test_queued_placeholder_is_never_typed_back(self) -> None:
        """End-to-end: delivery against a queued-state pane types only the wake."""
        pane = "\n".join(
            [
                f"\x1b[38;5;244m{RULE}",
                "\x1b[38;5;246m❯\xa0\x1b[2m\x1b[39mPress up to edit queued messages\x1b[0m",
                f"\x1b[38;5;244m{RULE}",
                "\x1b[39m  ⏵⏵ auto mode on",
            ]
        )
        sends: list[list[str]] = []

        def fake_run_tmux(args, *, capture=False, input_text=None):
            if args and args[0] == "capture-pane":
                return True, pane
            sends.append(args)
            return True, ""

        with patch.object(sb_actions, "_run_tmux", side_effect=fake_run_tmux):
            ok, saved, err = sb_actions.send_prompt_via_tmux(
                "multiverse-x", "WAKE", runtime="claude", require_composer=True
            )

        self.assertTrue(ok, err)
        self.assertEqual(saved, "")
        flat = " ".join(" ".join(map(str, c)) for c in sends)
        self.assertNotIn("Press up", flat)
        self.assertNotIn("BSpace", flat)

    def test_modal_hides_composer(self) -> None:
        self.assertEqual(sb_actions.claude_input_box(modal_pane()), (False, ""))

    def test_codex_composer_is_not_a_claude_composer(self) -> None:
        codex = "\n".join(
            [
                "• Working (1m 15s • esc to interrupt)",
                "",
                "› Find and fix a bug in @filename",
                "  gpt-5.6 · Multiverse · main · Working · Context 25% left",
            ]
        )
        self.assertEqual(sb_actions.claude_input_box(codex), (False, ""))

    def test_marker_in_scrollback_is_not_a_composer(self) -> None:
        """``❯`` also prefixes sent history and quoted inter-agent messages."""
        pane = "\n".join(
            [
                "❯ an earlier prompt the user already sent",
                "  ⏺ Claude replied to it",
                "❯ another sent prompt",
            ]
        )
        self.assertEqual(sb_actions.claude_input_box(pane), (False, ""))

    def test_multiline_draft_rows_are_joined(self) -> None:
        pane = "\n".join(
            [
                f"\x1b[38;5;37m{RULE}",
                "\x1b[39m❯\xa0first line",
                "second line",
                f"\x1b[38;5;37m{RULE}",
                "  ⏵⏵ auto mode on",
            ]
        )
        visible, draft = sb_actions.claude_input_box(pane)
        self.assertTrue(visible)
        self.assertEqual(draft, "first line\nsecond line")

    def test_draft_only_helper_matches_tuple_form(self) -> None:
        pane = claude_pane("hello")
        self.assertEqual(
            sb_actions.parse_input_box_text(pane, runtime="claude"), "hello"
        )


class ClaudeSendPreservesDraftTests(unittest.TestCase):
    """send_prompt_via_tmux: save → clear → send+submit → restore."""

    def _run(self, pane: str, *, require_composer: bool = True):
        calls: list[list[str]] = []

        def fake_run_tmux(args, *, capture=False, input_text=None):
            calls.append(args)
            if args and args[0] == "capture-pane":
                return True, pane
            return True, ""

        with patch.object(sb_actions, "_run_tmux", side_effect=fake_run_tmux):
            ok, saved, err = sb_actions.send_prompt_via_tmux(
                "multiverse-x",
                "WAKE",
                runtime="claude",
                require_composer=require_composer,
            )
        return ok, saved, err, calls

    def test_empty_composer_sends_without_clearing_or_restoring(self) -> None:
        ok, saved, err, calls = self._run(claude_pane())
        self.assertTrue(ok, err)
        self.assertEqual(saved, "")
        sends = [c for c in calls if c[0] == "send-keys"]
        # Exactly the wake text plus the submit — no BSpace, no restore.
        self.assertIn(["send-keys", "-t", "multiverse-x", "-l", "--", "WAKE"], sends)
        self.assertIn(["send-keys", "-t", "multiverse-x", "Enter"], sends)
        self.assertFalse(any("BSpace" in c for c in sends))

    def test_draft_is_cleared_then_restored_after_submit(self) -> None:
        ok, saved, err, calls = self._run(claude_pane("half typed thought"))
        self.assertTrue(ok, err)
        self.assertEqual(saved, "half typed thought")

        flat = [" ".join(c) for c in calls if c[0] == "send-keys"]
        clear_at = next(i for i, c in enumerate(flat) if "BSpace" in c)
        send_at = next(i for i, c in enumerate(flat) if c.endswith("-- WAKE"))
        submit_at = next(
            i for i, c in enumerate(flat) if c.endswith("Enter") and i > send_at
        )
        restore_at = next(
            i for i, c in enumerate(flat) if c.endswith("-- half typed thought")
        )
        # Order matters: clearing after the send would eat our own message, and
        # restoring before the submit would send the draft as part of the message.
        self.assertLess(clear_at, send_at)
        self.assertLess(send_at, submit_at)
        self.assertLess(submit_at, restore_at)

    def test_ghost_text_is_neither_cleared_nor_restored(self) -> None:
        ok, saved, err, calls = self._run(claude_pane("ghosty", ghost=True))
        self.assertTrue(ok, err)
        self.assertEqual(saved, "")
        flat = [" ".join(c) for c in calls if c[0] == "send-keys"]
        self.assertFalse(any("BSpace" in c for c in flat))
        self.assertFalse(any("ghosty" in c for c in flat))

    def test_missing_composer_fails_when_required(self) -> None:
        ok, _, err, calls = self._run(modal_pane())
        self.assertFalse(ok)
        self.assertIn("composer is not visible", err)
        # Nothing may be typed into a modal.
        self.assertFalse([c for c in calls if c[0] == "send-keys"])

    def test_missing_composer_still_sends_when_not_required(self) -> None:
        """The SecondBrain connector delivers to `waiting` targets; unchanged."""
        ok, _, err, calls = self._run(modal_pane(), require_composer=False)
        self.assertTrue(ok, err)
        self.assertTrue([c for c in calls if c[0] == "send-keys"])


class MessageAgentClaudeRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = "/tmp/ws"
        env = patch.dict(
            os.environ,
            {
                "MULTIVERSE_ROOT": self.tmp.name,
                "MULTIVERSE_SESSION_CWD": self.cwd,
                "MULTIVERSE_SESSION_NAME": "sender",
                "MULTIVERSE_RUNTIME": sessions.RUNTIME_CLAUDE,
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        for name in ("write_message_to_inbox", "log_message_sent",
                     "log_notification_routed"):
            p = patch.object(server, name)
            mock = p.start()
            self.addCleanup(p.stop)
            if name == "write_message_to_inbox":
                mock.return_value = type("P", (), {"stem": "mid"})()
        self.routed = server.log_notification_routed

    def _target(self, status: str = "idle"):
        sess = sessions.Session(
            id="ab", cwd=self.cwd, created_at=0, status=status,
            name="bravo", runtime=sessions.RUNTIME_CLAUDE, claude_uuid="U",
        )
        return sessions.DashboardRow(
            name="bravo", cwd=self.cwd, status=status, status_ts=0,
            session=sess, runtime=sessions.RUNTIME_CLAUDE, claude_uuid="U",
        )

    def _message(self, target):
        fn = getattr(server.message_agent, "fn", server.message_agent)
        with (
            patch.object(server, "find_agent_by_name", return_value=target),
            patch.object(server, "is_locked", return_value=False),
            # Routing is what's under test here. Left unstubbed, the submit
            # postcondition polls a pane that doesn't exist for its full
            # timeout on every one of these.
            patch.object(server, "_claude_wake_confirmed", return_value=True),
        ):
            return fn("bravo", "hello")

    def test_attached_user_no_longer_blocks_delivery(self) -> None:
        """The whole point of the change: attachment is not a reason to hold."""
        with patch.object(
            server, "send_prompt_via_tmux", return_value=(True, "", "")
        ) as send:
            result = self._message(self._target("idle"))

        send.assert_called_once()
        self.assertIn("pinged its terminal", result)
        self.assertEqual(
            self.routed.call_args.kwargs["route"], "wake_tmux"
        )

    def test_requires_composer(self) -> None:
        with patch.object(server, "send_prompt_via_tmux") as send:
            send.return_value = (True, "", "")
            self._message(self._target("working"))
        self.assertTrue(send.call_args.kwargs["require_composer"])
        self.assertEqual(send.call_args.kwargs["runtime"], sessions.RUNTIME_CLAUDE)

    def test_preserved_draft_is_reported_to_the_sender(self) -> None:
        with patch.object(
            server, "send_prompt_via_tmux", return_value=(True, "my draft", "")
        ):
            result = self._message(self._target("idle"))
        self.assertIn("saved and restored", result)

    def test_missing_composer_falls_back_to_queued(self) -> None:
        with patch.object(
            server,
            "send_prompt_via_tmux",
            return_value=(False, "", "Claude input composer is not visible"),
        ):
            result = self._message(self._target("idle"))
        self.assertIn("isn't on screen", result)
        self.assertIn("next turn", result)
        self.assertEqual(
            self.routed.call_args.kwargs["route"], "passive_fallback_no_composer"
        )

    def test_other_send_failures_report_the_ping_failure(self) -> None:
        with patch.object(
            server, "send_prompt_via_tmux", return_value=(False, "", "tmux exploded")
        ):
            result = self._message(self._target("idle"))
        self.assertIn("terminal ping failed", result)
        self.assertEqual(self.routed.call_args.kwargs["route"], "wake_tmux")
        self.assertFalse(self.routed.call_args.kwargs["success"])

    def test_codex_target_uses_the_same_attachment_independent_policy(self) -> None:
        target = self._target("idle")
        target.runtime = sessions.RUNTIME_CODEX
        target.session.runtime = sessions.RUNTIME_CODEX
        with (
            patch.object(server, "send_prompt_via_tmux") as send,
            patch.object(server, "_wake_via_tmux", return_value=True) as wake,
        ):
            result = self._message(target)

        send.assert_not_called()
        wake.assert_called_once_with(
            target.session.tmux_name,
            runtime=sessions.RUNTIME_CODEX,
        )
        self.assertIn("pinged to call check_inbox now", result)


if __name__ == "__main__":
    unittest.main()
