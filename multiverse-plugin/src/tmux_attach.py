"""Attach to tmux without exposing its capability probes to the outer terminal.

tmux probes every newly attached terminal with DA1, DA2, and XTVERSION queries.
Browser terminals such as xterm.js answer those queries asynchronously. If a reply
returns after tmux's query window closes, tmux treats it as keyboard input and sends
the printable tail to the focused pane.

The relay below places a local PTY between the monitor and ``tmux attach-session``.
It answers tmux's attach-time probes immediately, before they reach the real terminal,
and removes any already-in-flight identification replies from real terminal input.
Ordinary terminal bytes pass through unchanged in both directions.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable


# Responses describe a conservative xterm-compatible terminal. tmux uses them only
# to supplement the xterm-256color terminfo entry supplied by the real client.
_PROBE_RESPONSES = (
    (b"\x1b[c", b"\x1b[?1;2c"),
    (b"\x1b[>c", b"\x1b[>0;0;0c"),
    (b"\x1b[>q", b"\x1bP>|XTerm(0)\x1b\\"),
)

# A short hold joins an escape sequence fragmented across adjacent PTY reads without
# making a standalone Escape key noticeably laggy.
_PARTIAL_SEQUENCE_TIMEOUT_S = 0.02
_MAX_REPLY_BYTES = 512


def _write_all(fd: int, data: bytes) -> None:
    """Write all bytes, retrying interrupted and partial writes."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError(errno.EIO, "short terminal write")
        view = view[written:]


class _ProbeResponder:
    """Strip tmux's terminal queries and return their local responses."""

    def __init__(self) -> None:
        self._pending = b""

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def feed(self, data: bytes) -> tuple[bytes, list[bytes]]:
        data = self._pending + data
        self._pending = b""
        forwarded = bytearray()
        responses: list[bytes] = []
        index = 0

        while index < len(data):
            matched = False
            for query, response in _PROBE_RESPONSES:
                if data.startswith(query, index):
                    responses.append(response)
                    index += len(query)
                    matched = True
                    break
            if matched:
                continue

            tail = data[index:]
            if any(query.startswith(tail) for query, _ in _PROBE_RESPONSES):
                self._pending = tail
                break

            forwarded.append(data[index])
            index += 1

        return bytes(forwarded), responses

    def flush(self) -> bytes:
        pending, self._pending = self._pending, b""
        return pending


class _TerminalReplyFilter:
    """Remove DA1, DA2, and XTVERSION replies from terminal input.

    The parser is streaming so replies remain filtered even when the PTY splits an
    escape sequence across reads. Invalid candidates are emitted byte-for-byte.
    """

    _IDLE = 0
    _ESC = 1
    _CSI = 2
    _CSI_PARAMS = 3
    _DCS = 4
    _DCS_GT = 5
    _DCS_PAYLOAD = 6
    _DCS_PAYLOAD_ESC = 7

    def __init__(self) -> None:
        self._state = self._IDLE
        self._candidate = bytearray()
        self._saw_param = False

    @property
    def has_pending(self) -> bool:
        return bool(self._candidate)

    def _reset(self) -> None:
        self._state = self._IDLE
        self._candidate.clear()
        self._saw_param = False

    def _reject(self, forwarded: bytearray) -> None:
        forwarded.extend(self._candidate)
        self._reset()

    def feed(self, data: bytes) -> bytes:
        forwarded = bytearray()

        for byte in data:
            if self._state == self._IDLE:
                if byte == 0x1B:
                    self._candidate.append(byte)
                    self._state = self._ESC
                else:
                    forwarded.append(byte)
                continue

            self._candidate.append(byte)
            if len(self._candidate) > _MAX_REPLY_BYTES:
                self._reject(forwarded)
                continue

            if self._state == self._ESC:
                if byte == ord("["):
                    self._state = self._CSI
                elif byte == ord("P"):
                    self._state = self._DCS
                else:
                    self._reject(forwarded)
            elif self._state == self._CSI:
                if byte in (ord("?"), ord(">")):
                    self._state = self._CSI_PARAMS
                else:
                    self._reject(forwarded)
            elif self._state == self._CSI_PARAMS:
                if ord("0") <= byte <= ord("9") or byte == ord(";"):
                    self._saw_param = True
                elif byte == ord("c") and self._saw_param:
                    # Complete DA1/DA2 reply: discard the candidate.
                    self._reset()
                else:
                    self._reject(forwarded)
            elif self._state == self._DCS:
                if byte == ord(">"):
                    self._state = self._DCS_GT
                else:
                    self._reject(forwarded)
            elif self._state == self._DCS_GT:
                if byte == ord("|"):
                    self._state = self._DCS_PAYLOAD
                else:
                    self._reject(forwarded)
            elif self._state == self._DCS_PAYLOAD:
                if byte == 0x9C:
                    # Complete XTVERSION reply using the C1 ST terminator.
                    self._reset()
                elif byte == 0x1B:
                    self._state = self._DCS_PAYLOAD_ESC
            elif self._state == self._DCS_PAYLOAD_ESC:
                if byte == ord("\\"):
                    # Complete XTVERSION reply using the ESC \ terminator.
                    self._reset()
                else:
                    self._reject(forwarded)

        return bytes(forwarded)

    def flush(self) -> bytes:
        pending = bytes(self._candidate)
        self._reset()
        return pending


def _copy_window_size(source_fd: int, target_fd: int) -> None:
    """Best-effort copy of the real terminal size to the relay PTY."""
    try:
        size = fcntl.ioctl(source_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(target_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _relay(
    master_fd: int,
    *,
    stdin_fd: int,
    stdout_fd: int,
) -> None:
    probes = _ProbeResponder()
    replies = _TerminalReplyFilter()
    readers = [master_fd, stdin_fd]
    probe_pending_since: float | None = None
    reply_pending_since: float | None = None

    while master_fd in readers:
        now = time.monotonic()
        deadlines = [
            started + _PARTIAL_SEQUENCE_TIMEOUT_S
            for started in (probe_pending_since, reply_pending_since)
            if started is not None
        ]
        timeout = max(0.0, min(deadlines) - now) if deadlines else None
        try:
            readable, _, _ = select.select(readers, [], [], timeout)
        except InterruptedError:
            continue

        if master_fd in readable:
            try:
                data = os.read(master_fd, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    data = b""
                else:
                    raise
            if not data:
                pending = probes.flush()
                if pending:
                    _write_all(stdout_fd, pending)
                break

            forwarded, responses = probes.feed(data)
            if probes.has_pending:
                probe_pending_since = probe_pending_since or time.monotonic()
            else:
                probe_pending_since = None
            for response in responses:
                _write_all(master_fd, response)
            if forwarded:
                _write_all(stdout_fd, forwarded)

        if stdin_fd in readable:
            data = os.read(stdin_fd, 65536)
            if not data:
                readers.remove(stdin_fd)
            else:
                forwarded = replies.feed(data)
                if replies.has_pending:
                    reply_pending_since = reply_pending_since or time.monotonic()
                else:
                    reply_pending_since = None
                if forwarded:
                    _write_all(master_fd, forwarded)

        # Flush partial candidates on their own deadlines even if the other side of
        # the relay is continuously readable (for example, a busy TUI while the user
        # presses Escape by itself).
        now = time.monotonic()
        if (
            probe_pending_since is not None
            and now - probe_pending_since >= _PARTIAL_SEQUENCE_TIMEOUT_S
        ):
            pending_output = probes.flush()
            probe_pending_since = None
            if pending_output:
                _write_all(stdout_fd, pending_output)
        if (
            reply_pending_since is not None
            and now - reply_pending_since >= _PARTIAL_SEQUENCE_TIMEOUT_S
        ):
            pending_input = replies.flush()
            reply_pending_since = None
            if pending_input:
                _write_all(master_fd, pending_input)


def attach_tmux_session(
    tmux_session: str,
    *,
    on_client_started: Callable[[int], None] | None = None,
) -> int:
    """Attach to ``tmux_session`` through the capability-probe relay."""
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    master_fd, slave_fd = os.openpty()
    _copy_window_size(stdin_fd, slave_fd)

    try:
        process = subprocess.Popen(
            ["tmux", "attach-session", "-t", tmux_session],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
        if on_client_started is not None:
            on_client_started(process.pid)
    finally:
        os.close(slave_fd)

    original_attrs = None
    original_winch = signal.getsignal(signal.SIGWINCH)

    try:
        try:
            original_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        except termios.error:
            pass

        def resize(_signum: int, _frame: object) -> None:
            _copy_window_size(stdin_fd, master_fd)
            try:
                process.send_signal(signal.SIGWINCH)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGWINCH, resize)
        _copy_window_size(stdin_fd, master_fd)
        _relay(master_fd, stdin_fd=stdin_fd, stdout_fd=stdout_fd)
    finally:
        signal.signal(signal.SIGWINCH, original_winch)
        if original_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, original_attrs)
        os.close(master_fd)

    while True:
        try:
            return process.wait()
        except InterruptedError:
            time.sleep(0)
