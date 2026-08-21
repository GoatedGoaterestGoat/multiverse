"""One outstanding wake prompt at a time.

Delivery is always active, so every message_agent call to a live agent types a
"check your inbox" prompt into its terminal. Because reading an inbox drains
all of it, a second prompt sent before the agent acted on the first is pure
waste — it burns a turn to find an empty inbox. These tests pin the state
machine that suppresses it.

"Outstanding" means the prompt was *accepted* by the target's composer (both
runtimes confirm the text left the composer before reporting success) and
remains unacted-on until the inbox is actually consumed — not that it is
sitting in some queue until end-of-turn.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from src import inbox, server, sessions


class WakeMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        for mod, attr, value in (
            (inbox, "INBOX_DIR", f"{root}/inbox"),
            (inbox, "WAKE_PENDING_DIR", f"{root}/wake_pending"),
        ):
            p = patch.object(mod, attr, __import__("pathlib").Path(value))
            p.start()
            self.addCleanup(p.stop)
        self.cwd = "/tmp/ws"

    def _queue(self, n: int = 1) -> None:
        for i in range(n):
            inbox.write_message_to_inbox("alice", "bob", self.cwd, f"msg {i}")

    def test_unset_marker_is_not_outstanding(self) -> None:
        self._queue()
        self.assertFalse(inbox.wake_is_outstanding(self.cwd, "bob"))

    def test_marker_plus_pending_message_is_outstanding(self) -> None:
        self._queue()
        inbox.mark_wake_sent(self.cwd, "bob")
        self.assertTrue(inbox.wake_is_outstanding(self.cwd, "bob"))

    def test_clear_retires_the_marker(self) -> None:
        self._queue()
        inbox.mark_wake_sent(self.cwd, "bob")
        inbox.clear_wake_sent(self.cwd, "bob")
        self.assertFalse(inbox.wake_is_outstanding(self.cwd, "bob"))

    def test_empty_inbox_self_heals_a_stale_marker(self) -> None:
        """An agent killed after being pinged must not be muted forever."""
        inbox.mark_wake_sent(self.cwd, "bob")  # marker set, nothing queued
        self.assertFalse(inbox.wake_is_outstanding(self.cwd, "bob"))
        # ...and the stale marker is gone, not merely ignored.
        self._queue()
        self.assertFalse(inbox.wake_is_outstanding(self.cwd, "bob"))

    def test_claiming_the_inbox_does_not_by_itself_clear(self) -> None:
        """Clearing is the consumer's explicit call, not a claim side effect.

        Pinning this so the two consumers (deliver-inbox.py, check_inbox) stay
        responsible for it — a future refactor that moves clearing into claim
        would silently change when senders are allowed to prompt again.
        """
        self._queue()
        inbox.mark_wake_sent(self.cwd, "bob")
        inbox.claim_pending_messages(self.cwd, "bob")
        self.assertTrue(inbox._wake_marker_path(self.cwd, "bob").exists())

    def test_clear_is_idempotent_and_safe_when_absent(self) -> None:
        inbox.clear_wake_sent(self.cwd, "bob")
        inbox.clear_wake_sent(self.cwd, "bob")

    def test_lock_body_exception_propagates_once(self) -> None:
        """A failure inside the protected body must surface as itself.

        Regression: swallowing OSError around the ``yield`` caught exceptions
        thrown back in from the body and then yielded a second time, so a tmux
        OSError during delivery surfaced as "generator didn't stop after
        throw()" instead of the real error.
        """
        with self.assertRaises(OSError) as caught:
            with inbox.wake_delivery_lock(self.cwd, "bob"):
                raise OSError("tmux blew up mid-delivery")
        self.assertIn("tmux blew up mid-delivery", str(caught.exception))

    def test_lock_body_runs_exactly_once(self) -> None:
        runs: list = []
        with inbox.wake_delivery_lock(self.cwd, "bob"):
            runs.append(1)
        self.assertEqual(runs, [1])

    def test_lock_falls_back_to_unlocked_when_acquisition_fails(self) -> None:
        """No fcntl / odd filesystem must not break delivery."""
        runs: list = []
        with patch.object(
            inbox, "exclusive_file_lock", side_effect=OSError("no flock here")
        ):
            with inbox.wake_delivery_lock(self.cwd, "bob"):
                runs.append(1)
        self.assertEqual(runs, [1])


class MessageAgentWakeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(
            os.environ,
            {
                "MULTIVERSE_ROOT": self.tmp.name,
                "MULTIVERSE_SESSION_CWD": "/tmp/ws",
                "MULTIVERSE_SESSION_NAME": "alice",
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

    def _target(self, runtime=sessions.RUNTIME_CLAUDE, status="working"):
        sess = sessions.Session(
            id="ab", cwd="/tmp/ws", created_at=0, status=status,
            name="bob", runtime=runtime, claude_uuid="U",
        )
        return sessions.DashboardRow(
            name="bob", cwd="/tmp/ws", status=status, status_ts=0,
            session=sess, runtime=runtime, claude_uuid="U",
        )

    def _send(self, target, *, outstanding: bool, confirmed: bool = True):
        fn = getattr(server.message_agent, "fn", server.message_agent)
        with (
            patch.object(server, "find_agent_by_name", return_value=target),
            patch.object(server, "is_locked", return_value=False),
            patch.object(server, "wake_is_outstanding", return_value=outstanding),
            patch.object(server, "mark_wake_sent") as mark,
            patch.object(
                server, "_claude_wake_confirmed", return_value=confirmed
            ),
            patch.object(
                server, "send_prompt_via_tmux", return_value=(True, "", "")
            ) as claude_send,
            patch.object(server, "_wake_via_tmux", return_value=True) as codex_send,
        ):
            result = fn("bob", "hello")
        return result, mark, claude_send, codex_send

    def test_no_outstanding_wake_pings_and_marks(self) -> None:
        result, mark, claude_send, _ = self._send(
            self._target(), outstanding=False
        )
        claude_send.assert_called_once()
        mark.assert_called_once_with("/tmp/ws", "bob")
        self.assertIn("pinged its terminal", result)

    def test_outstanding_wake_suppresses_the_second_prompt(self) -> None:
        result, mark, claude_send, _ = self._send(
            self._target(), outstanding=True
        )
        claude_send.assert_not_called()
        mark.assert_not_called()
        self.assertIn("hasn't done so yet", result)
        self.assertEqual(
            self.routed.call_args.kwargs["route"], "skipped_wake_outstanding"
        )

    def test_gate_applies_to_codex_too(self) -> None:
        result, _, _, codex_send = self._send(
            self._target(runtime=sessions.RUNTIME_CODEX), outstanding=True
        )
        codex_send.assert_not_called()
        self.assertIn("hasn't done so yet", result)

    def test_codex_ping_marks_when_not_outstanding(self) -> None:
        _, mark, _, codex_send = self._send(
            self._target(runtime=sessions.RUNTIME_CODEX), outstanding=False
        )
        codex_send.assert_called_once()
        mark.assert_called_once_with("/tmp/ws", "bob")

    def test_unconfirmed_claude_submit_does_not_mark(self) -> None:
        """The dangerous case: tmux succeeded but Claude never took the prompt.

        Marking here would suppress every later ping, and the empty-inbox
        self-heal cannot recover it — nothing is being delivered, so the inbox
        never empties. Delivery to that agent would stop for good.
        """
        result, mark, claude_send, _ = self._send(
            self._target(), outstanding=False, confirmed=False
        )
        claude_send.assert_called_once()
        mark.assert_not_called()
        # Still reported as delivered — we simply don't promise to stop pinging.
        self.assertIn("pinged its terminal", result)

    def test_confirm_is_checked_against_the_restored_draft(self) -> None:
        """Confirmation compares the composer to what we put back, not to empty."""
        fn = getattr(server.message_agent, "fn", server.message_agent)
        with (
            patch.object(server, "find_agent_by_name", return_value=self._target()),
            patch.object(server, "is_locked", return_value=False),
            patch.object(server, "wake_is_outstanding", return_value=False),
            patch.object(server, "mark_wake_sent"),
            patch.object(
                server, "_claude_wake_confirmed", return_value=True
            ) as confirm,
            patch.object(
                server,
                "send_prompt_via_tmux",
                return_value=(True, "user's half-typed draft", ""),
            ),
        ):
            fn("bob", "hello")
        self.assertEqual(confirm.call_args.args[1], "user's half-typed draft")

    def test_failed_ping_does_not_mark(self) -> None:
        """A failed ping must not mute future ones."""
        fn = getattr(server.message_agent, "fn", server.message_agent)
        with (
            patch.object(server, "find_agent_by_name", return_value=self._target()),
            patch.object(server, "is_locked", return_value=False),
            patch.object(server, "wake_is_outstanding", return_value=False),
            patch.object(server, "mark_wake_sent") as mark,
            patch.object(
                server, "send_prompt_via_tmux", return_value=(False, "", "tmux died")
            ),
        ):
            result = fn("bob", "hello")
        mark.assert_not_called()
        self.assertIn("terminal ping failed", result)

    def test_hidden_composer_does_not_mark(self) -> None:
        """Nothing was typed, so nothing is outstanding."""
        fn = getattr(server.message_agent, "fn", server.message_agent)
        with (
            patch.object(server, "find_agent_by_name", return_value=self._target()),
            patch.object(server, "is_locked", return_value=False),
            patch.object(server, "wake_is_outstanding", return_value=False),
            patch.object(server, "mark_wake_sent") as mark,
            patch.object(
                server,
                "send_prompt_via_tmux",
                return_value=(False, "", "Claude input composer is not visible"),
            ),
        ):
            result = fn("bob", "hello")
        mark.assert_not_called()
        self.assertIn("isn't on screen", result)

    def test_dormant_target_is_unaffected_by_the_gate(self) -> None:
        """Auto-resume still runs — the gate only guards terminal typing."""
        target = sessions.DashboardRow(
            name="bob", cwd="/tmp/ws", status="dormant", status_ts=0,
            session=None, runtime=sessions.RUNTIME_CLAUDE, claude_uuid="U",
        )
        fn = getattr(server.message_agent, "fn", server.message_agent)
        with (
            patch.object(server, "find_agent_by_name", return_value=target),
            patch.object(server, "is_locked", return_value=False),
            patch.object(server, "wake_is_outstanding", return_value=True),
            patch.object(server, "_auto_resume") as resume,
        ):
            result = fn("bob", "hello")
        resume.assert_called_once()
        self.assertIn("Woke 'bob'", result)


class ClaudeWakeConfirmationTests(unittest.TestCase):
    """Claude, unlike Codex, doesn't confirm its own submit — so we do."""

    def _pane(self, composer: str) -> str:
        rule = "─" * 60
        return f"{rule}\n❯\xa0{composer}\n{rule}\n  ⏵⏵ auto mode on\n"

    def test_confirmed_when_composer_holds_the_restored_draft(self) -> None:
        with patch.object(
            server, "capture_pane", return_value=(True, self._pane("my draft"))
        ):
            self.assertTrue(server._claude_wake_confirmed("t", "my draft"))

    def test_confirmed_when_composer_empty_and_no_draft(self) -> None:
        with patch.object(server, "capture_pane", return_value=(True, self._pane(""))):
            self.assertTrue(server._claude_wake_confirmed("t", ""))

    def test_unconfirmed_when_the_wake_prompt_is_still_sitting_there(self) -> None:
        """A dropped Enter leaves our own text in the box."""
        with (
            patch.object(
                server, "capture_pane", return_value=(True, self._pane("Please check"))
            ),
            patch.object(server, "_WAKE_CONFIRM_TIMEOUT_S", 0.05),
        ):
            self.assertFalse(server._claude_wake_confirmed("t", ""))

    def test_unconfirmed_when_composer_is_gone(self) -> None:
        with (
            patch.object(server, "capture_pane", return_value=(True, "a modal\n  1. Yes")),
            patch.object(server, "_WAKE_CONFIRM_TIMEOUT_S", 0.05),
        ):
            self.assertFalse(server._claude_wake_confirmed("t", ""))

    def test_unconfirmed_when_capture_fails(self) -> None:
        with (
            patch.object(server, "capture_pane", return_value=(False, "tmux error")),
            patch.object(server, "_WAKE_CONFIRM_TIMEOUT_S", 0.05),
        ):
            self.assertFalse(server._claude_wake_confirmed("t", ""))

    def test_polls_until_the_tui_renders(self) -> None:
        """A healthy submit that needs a render cycle must still confirm."""
        panes = [
            (True, self._pane("Please check")),  # pre-render: prompt still shown
            (True, self._pane("")),              # rendered: submitted
        ]
        with (
            patch.object(server, "capture_pane", side_effect=panes),
            patch.object(server, "_WAKE_CONFIRM_POLL_S", 0.001),
        ):
            self.assertTrue(server._claude_wake_confirmed("t", ""))


class ConsumersClearTheMarkerTests(unittest.TestCase):
    """The two places an agent consumes its inbox must retire the prompt."""

    def test_check_inbox_clears_even_when_empty(self) -> None:
        with (
            patch.object(
                server, "_resolve_self_identity", return_value=("bob", "/tmp/ws")
            ),
            patch.object(server, "claim_pending_messages", return_value=[]),
            patch.object(server, "clear_wake_sent") as clear,
        ):
            fn = getattr(server.check_inbox, "fn", server.check_inbox)
            out = fn()
        clear.assert_called_once_with("/tmp/ws", "bob")
        self.assertIn("no pending inbox messages", out)

    def test_check_inbox_clears_when_delivering(self) -> None:
        msgs = [{"from": "alice", "body": "hi", "ts": 1, "message_id": "m"}]
        with (
            patch.object(
                server, "_resolve_self_identity", return_value=("bob", "/tmp/ws")
            ),
            patch.object(server, "claim_pending_messages", return_value=msgs),
            patch.object(server, "commit_claimed_messages"),
            patch.object(server, "log_inbox_delivered"),
            patch.object(server, "clear_wake_sent") as clear,
        ):
            fn = getattr(server.check_inbox, "fn", server.check_inbox)
            fn()
        clear.assert_called_once_with("/tmp/ws", "bob")

    def test_deliver_inbox_hook_clears(self) -> None:
        """Claude consumes via the hook, so the hook must clear."""
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "scripts" / "deliver-inbox.py"
        spec = importlib.util.spec_from_file_location("deliver_inbox_mod", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        msgs = [{"from": "alice", "body": "hi", "ts": 1, "message_id": "m"}]
        with (
            patch.dict(
                os.environ,
                {"MULTIVERSE_SESSION_CWD": "/tmp/ws",
                 "MULTIVERSE_SESSION_NAME": "bob"},
                clear=False,
            ),
            patch.object(mod, "claim_pending_messages", return_value=msgs),
            patch.object(mod, "commit_claimed_messages"),
            patch.object(mod, "log_inbox_delivered"),
            patch.object(mod, "render_messages_as_system_reminder", return_value="x"),
            patch.object(mod, "_read_input", return_value={"hook_event_name": "Stop"}),
            patch.object(mod, "clear_wake_sent") as clear,
        ):
            mod.main()
        clear.assert_called_once_with("/tmp/ws", "bob")


class EndToEndBurstTests(unittest.TestCase):
    """The actual scenario, with the real marker + real inbox files."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from pathlib import Path

        root = Path(self.tmp.name)
        for attr, sub in (("INBOX_DIR", "inbox"), ("WAKE_PENDING_DIR", "wake_pending")):
            p = patch.object(inbox, attr, root / sub)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(
            os.environ,
            {
                "MULTIVERSE_SESSION_CWD": "/tmp/ws",
                "MULTIVERSE_SESSION_NAME": "alice",
                "MULTIVERSE_RUNTIME": sessions.RUNTIME_CLAUDE,
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        for name in ("log_message_sent", "log_notification_routed"):
            p = patch.object(server, name)
            p.start()
            self.addCleanup(p.stop)

    def _busy_target(self):
        sess = sessions.Session(
            id="ab", cwd="/tmp/ws", created_at=0, status="working",
            name="bob", runtime=sessions.RUNTIME_CLAUDE, claude_uuid="U",
        )
        return sessions.DashboardRow(
            name="bob", cwd="/tmp/ws", status="working", status_ts=0,
            session=sess, runtime=sessions.RUNTIME_CLAUDE, claude_uuid="U",
        )

    def _send(self, body: str, sends: list):
        fn = getattr(server.message_agent, "fn", server.message_agent)

        def fake_send(tmux, prompt, **kw):
            sends.append(prompt)
            return True, "", ""

        with (
            patch.object(
                server, "_resolve_self_identity", return_value=("alice", "/tmp/ws")
            ),
            patch.object(server, "find_agent_by_name", return_value=self._busy_target()),
            patch.object(server, "is_locked", return_value=False),
            patch.object(server, "_claude_wake_confirmed", return_value=True),
            patch.object(server, "send_prompt_via_tmux", side_effect=fake_send),
        ):
            return fn("bob", body)

    def test_burst_of_three_types_exactly_one_prompt(self) -> None:
        sends: list = []
        first = self._send("one", sends)
        second = self._send("two", sends)
        third = self._send("three", sends)

        self.assertEqual(len(sends), 1, "only the first message may type a prompt")
        self.assertIn("pinged its terminal", first)
        for later in (second, third):
            self.assertIn("hasn't done so yet", later)

        # All three are still queued — suppressing the prompt must never drop
        # the message itself.
        self.assertEqual(inbox.pending_inbox_count("/tmp/ws", "bob"), 3)

    def test_concurrent_senders_produce_one_prompt(self) -> None:
        """Two sender processes racing must still cost the agent one prompt.

        The check and the mark are separated by the ping, so without the
        per-recipient lock both senders read "nothing outstanding" and both
        type — exactly the double interruption this feature removes.
        """
        import threading

        sends: list = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def fake_send(tmux, prompt, **kw):
            # Widen the check→mark window so an unlocked implementation loses.
            with lock:
                sends.append(prompt)
            return True, "", ""

        def send_one(body: str) -> None:
            fn = getattr(server.message_agent, "fn", server.message_agent)
            with (
                patch.object(
                    server, "_resolve_self_identity", return_value=("alice", "/tmp/ws")
                ),
                patch.object(
                    server, "find_agent_by_name", return_value=self._busy_target()
                ),
                patch.object(server, "is_locked", return_value=False),
                patch.object(server, "_claude_wake_confirmed", return_value=True),
                patch.object(server, "send_prompt_via_tmux", side_effect=fake_send),
            ):
                barrier.wait(timeout=5)
                fn("bob", body)

        threads = [
            threading.Thread(target=send_one, args=(f"msg {i}",)) for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(sends), 1, f"expected one prompt, got {len(sends)}")
        self.assertEqual(inbox.pending_inbox_count("/tmp/ws", "bob"), 2)

    def test_after_the_agent_consumes_the_next_message_pings_again(self) -> None:
        sends: list = []
        self._send("one", sends)
        self._send("two", sends)
        self.assertEqual(len(sends), 1)

        # Agent wakes up and drains its inbox (the deliver-inbox/check_inbox path).
        claimed = inbox.claim_pending_messages("/tmp/ws", "bob")
        inbox.commit_claimed_messages(claimed)
        inbox.clear_wake_sent("/tmp/ws", "bob")

        self._send("three", sends)
        self.assertEqual(len(sends), 2, "a fresh message after consumption must ping")


if __name__ == "__main__":
    unittest.main()
