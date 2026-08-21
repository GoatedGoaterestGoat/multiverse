from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src import tmux_attach


class ProbeResponderTests(unittest.TestCase):
    def test_answers_and_removes_fragmented_tmux_probes(self) -> None:
        responder = tmux_attach._ProbeResponder()
        forwarded = bytearray()
        responses: list[bytes] = []

        for chunk in (b"before\x1b[", b">cbetween\x1b[>q", b"after"):
            visible, local = responder.feed(chunk)
            forwarded.extend(visible)
            responses.extend(local)

        self.assertEqual(forwarded, b"beforebetweenafter")
        self.assertEqual(
            responses,
            [b"\x1b[>0;0;0c", b"\x1bP>|XTerm(0)\x1b\\"],
        )
        self.assertFalse(responder.has_pending)

    def test_preserves_non_probe_terminal_output(self) -> None:
        responder = tmux_attach._ProbeResponder()
        output = b"\x1b[2J\x1b[Hnormal output"

        forwarded, responses = responder.feed(output)

        self.assertEqual(forwarded, output)
        self.assertEqual(responses, [])

    def test_flush_preserves_incomplete_non_query(self) -> None:
        responder = tmux_attach._ProbeResponder()

        forwarded, responses = responder.feed(b"text\x1b[")

        self.assertEqual(forwarded, b"text")
        self.assertEqual(responses, [])
        self.assertEqual(responder.flush(), b"\x1b[")


class TerminalReplyFilterTests(unittest.TestCase):
    def test_drops_exact_xtermjs_replies_even_when_fragmented(self) -> None:
        reply_filter = tmux_attach._TerminalReplyFilter()
        chunks = (
            b"\x1b[?1;2c",
            b"\x1b[>0;276;",
            b"0c\x1bP>|xterm.js(6.1.0-",
            b"beta.288)\x1b\\",
        )

        self.assertEqual(b"".join(reply_filter.feed(c) for c in chunks), b"")
        self.assertFalse(reply_filter.has_pending)

    def test_removes_replies_without_losing_adjacent_user_input(self) -> None:
        reply_filter = tmux_attach._TerminalReplyFilter()
        data = (
            b"hello"
            b"\x1b[>0;276;0c"
            b" world"
            b"\x1bP>|xterm.js(6.1.0-beta.288)\x1b\\"
            b"\r"
        )

        self.assertEqual(reply_filter.feed(data), b"hello world\r")

    def test_preserves_queries_and_other_control_sequences(self) -> None:
        reply_filter = tmux_attach._TerminalReplyFilter()
        data = b"\x1b[c\x1b[>c\x1b[6;10R\x1b[A"

        self.assertEqual(reply_filter.feed(data), data)

    def test_flush_preserves_standalone_escape_key(self) -> None:
        reply_filter = tmux_attach._TerminalReplyFilter()

        self.assertEqual(reply_filter.feed(b"\x1b"), b"")
        self.assertEqual(reply_filter.flush(), b"\x1b")


class AttachClientIdentityTests(unittest.TestCase):
    def test_reports_tmux_client_pid_before_relaying(self) -> None:
        process = MagicMock(pid=4242)
        process.wait.return_value = 0
        events: list[str] = []
        callback = MagicMock(side_effect=lambda _pid: events.append("client"))
        terminal = MagicMock()
        terminal.fileno.return_value = 7

        with (
            patch.object(tmux_attach.sys, "stdin", terminal),
            patch.object(tmux_attach.sys, "stdout", terminal),
            patch.object(tmux_attach.os, "openpty", return_value=(10, 11)),
            patch.object(tmux_attach.os, "close"),
            patch.object(tmux_attach, "_copy_window_size"),
            patch.object(tmux_attach.subprocess, "Popen", return_value=process),
            patch.object(tmux_attach.termios, "tcgetattr", return_value=None),
            patch.object(tmux_attach.signal, "getsignal", return_value=None),
            patch.object(tmux_attach.signal, "signal"),
            patch.object(
                tmux_attach,
                "_relay",
                side_effect=lambda *_args, **_kwargs: events.append("relay"),
            ) as relay,
        ):
            result = tmux_attach.attach_tmux_session(
                "multiverse-caller",
                on_client_started=callback,
            )

        self.assertEqual(result, 0)
        callback.assert_called_once_with(4242)
        self.assertEqual(events, ["client", "relay"])
        relay.assert_called_once_with(10, stdin_fd=7, stdout_fd=7)
