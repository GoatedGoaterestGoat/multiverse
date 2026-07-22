from __future__ import annotations

import unittest

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

