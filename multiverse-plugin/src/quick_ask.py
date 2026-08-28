"""Quick-ask overlay — a side question about what is on screen right now.

Runs inside a ``tmux display-popup`` floating over an attached agent session.
The monitor already exposes the same engine on its ``a`` key, but ``_attach_to``
suspends the monitor while the user is attached, so that key is dead at exactly
the moment the user is reading output and wants to ask about it. This is the
second door to the same engine, reachable from inside the attached session.

Mechanism is unchanged: ``ask_agents.ask_one`` forks the target's session into
a throwaway tool-less copy, runs one print-mode turn, reads the answer, deletes
the fork. The target's own process is never involved and its conversation is
never touched, so this is free to use mid-turn.

Invoked by the tmux binding installed in ``sessions.create_session``:

    tmux bind-key -T prefix <key> display-popup -E -w .. -h .. \
        "<plugin_root>/bin/multiverse-ask '#{pane_id}' '#{session_name}'"

The pane id must be passed in from the binding. Inside a popup ``#{pane_id}``
resolves to the *popup's own* pane, so capturing without the argument silently
yields an empty screen.
"""

from __future__ import annotations

import contextlib
import os
import select
import subprocess
import sys
import termios
import threading
import time
import tty
from typing import Optional

from .ask_agents import AskTarget, ask_one_streaming
from .sessions import TMUX_PREFIX, Session, load_session

#: How many lines of the underlying pane to send as context.
CONTEXT_LINES = int(os.environ.get("MULTIVERSE_ASK_CONTEXT_LINES", "150"))
#: Hard cap on context characters. A pane full of wide output can be far
#: larger than the line count suggests, and the fork pays for every token.
CONTEXT_MAX_CHARS = int(os.environ.get("MULTIVERSE_ASK_CONTEXT_CHARS", "12000"))
#: Time-to-first-token budget. This covers the fork rebuilding the target's
#: context, which scales with transcript size: measured 4.6s warm to 15.0s cold
#: on one 9.7MB session, against ~2.6s on a 2.3MB one. A tight cap here fails
#: exactly on the big sessions worth asking about, so it is generous and the
#: user aborts with Escape when they are done waiting.
TIMEOUT = float(os.environ.get("MULTIVERSE_ASK_TIMEOUT", "90"))
#: Max gap between tokens once text is flowing. Catches a genuine stall without
#: ever cutting off an answer for being long.
IDLE_TIMEOUT = float(os.environ.get("MULTIVERSE_ASK_IDLE_TIMEOUT", "30"))


def _capture_pane(pane_id: str) -> str:
    """Plain text of the underlying pane's last ``CONTEXT_LINES`` lines.

    No ``-e``, so tmux strips escape sequences and we get clean text. ``-J``
    rejoins lines the pane wrapped, which otherwise arrive as hard breaks
    mid-sentence and confuse the reader downstream.
    """
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-S", f"-{CONTEXT_LINES}",
             "-t", pane_id],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    text = out.stdout.strip("\n")
    if len(text) > CONTEXT_MAX_CHARS:
        # Keep the tail: the user is asking about what they just read, which
        # is at the bottom of the pane.
        text = "…(earlier output trimmed)…\n" + text[-CONTEXT_MAX_CHARS:]
    return text


def _looks_unexpanded(arg: str) -> bool:
    """True for a literal ``#{pane_id}`` that tmux never substituted.

    Not every tmux command expands format strings in a shell-command argument,
    and the behaviour differs by version. Rather than depend on it, treat an
    unexpanded placeholder as "no argument given" and fall through to the
    other resolution tiers.
    """
    return "#{" in arg or not arg.strip()


def _tmux(*args: str) -> str:
    try:
        out = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _resolve_session_id(arg_session_name: str) -> tuple[Optional[str], str]:
    """Find the multiverse session id, most reliable source first.

    1. ``MULTIVERSE_SESSION_ID`` from the tmux session environment, which the
       monitor sets via ``tmux new-session -e``. Exact, and independent of
       whether the binding's format strings were expanded.
    2. The session name passed by the binding, when it was expanded.
    3. A live ``display-message`` query from inside the popup, whose pane sits
       in the same session as the pane that triggered it.

    Returns ``(sid, source)``; ``sid`` is None when every tier fails.
    """
    env_sid = os.environ.get("MULTIVERSE_SESSION_ID", "").strip()
    if env_sid:
        return env_sid, "session env"

    if not _looks_unexpanded(arg_session_name):
        if arg_session_name.startswith(TMUX_PREFIX):
            return arg_session_name[len(TMUX_PREFIX):], "binding argument"
        return None, f"not a multiverse session ({arg_session_name})"

    live = _tmux("display-message", "-p", "#{session_name}")
    if live.startswith(TMUX_PREFIX):
        return live[len(TMUX_PREFIX):], "live tmux query"
    if live:
        return None, f"not a multiverse session ({live})"
    return None, "could not determine the tmux session"


def _resolve_pane(arg_pane_id: str, sid: str) -> str:
    """Pane to capture: the binding's argument, else the session's first pane.

    Never the popup's own pane — that is empty, and is what ``#{pane_id}``
    resolves to if evaluated inside the popup rather than by the binding.
    """
    if not _looks_unexpanded(arg_pane_id) and arg_pane_id.startswith("%"):
        return arg_pane_id
    panes = _tmux("list-panes", "-t", f"{TMUX_PREFIX}{sid}", "-F", "#{pane_id}")
    return panes.splitlines()[0] if panes else ""


def _resolve_target(sid: str) -> tuple[Optional[AskTarget], Optional[str]]:
    """Map a multiverse session id to the agent to ask. Returns (target, error)."""
    session: Optional[Session] = load_session(sid)
    if session is None:
        return None, f"No multiverse session on disk for id '{sid}'."
    uuid = session.effective_claude_uuid
    if not uuid:
        return None, (
            "This session has not reported a transcript id yet — it is "
            "probably still starting. Try again in a moment."
        )
    return AskTarget(
        name=session.name or sid,
        cwd=session.cwd,
        claude_uuid=uuid,
        harness=session.harness,
    ), None


def _compose(question: str, screen: str) -> str:
    """Frame the on-screen text as context around the user's question.

    The harness adapter prepends its own SIDE_QUESTION_WRAPPER on top of this,
    which is what establishes the no-tools, one-shot contract.
    """
    if not screen:
        return question
    return (
        "The user is looking at the tail of your terminal output right now. "
        "This is what is on their screen:\n\n"
        "<screen>\n" + screen + "\n</screen>\n\n"
        "Answer their question about it directly and briefly. They are "
        "reading, not delegating — do not propose next steps unless asked.\n\n"
        "Question: " + question
    )


class _Spinner:
    """Minimal inline progress so a slow fork does not look like a hang."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str) -> None:
        self._label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        # Only animate on a real terminal. Under a pipe (tests, `| cat`) the
        # \r frames do not overwrite and 12 frames/sec becomes a wall of text.
        if not sys.stdout.isatty():
            sys.stdout.write(f"{self._label}…\n")
            sys.stdout.flush()
            self._stop.wait()
            return
        started = time.monotonic()
        i = 0
        while not self._stop.is_set():
            elapsed = time.monotonic() - started
            sys.stdout.write(
                f"\r{self.FRAMES[i % len(self.FRAMES)]} {self._label} "
                f"({elapsed:0.0f}s, esc to abort)  "
            )
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.08)
        if sys.stdout.isatty():
            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()

    def start(self) -> "_Spinner":
        self._thread.start()
        return self

    def stop(self) -> None:
        """Idempotent — called by the first delta and again in a finally."""
        if self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=1.0)

    def __enter__(self) -> "_Spinner":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


ESC = "\x1b"


def _stdin_ready(timeout: float) -> bool:
    try:
        return bool(select.select([sys.stdin], [], [], timeout)[0])
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def _cbreak():
    """Unbuffered, unechoed input while keeping output post-processing.

    ``cbreak`` rather than ``raw`` on purpose: raw mode also clears ONLCR, so
    every newline the streaming answer prints would stair-step across the pane
    instead of returning to column zero.
    """
    if not sys.stdin.isatty():
        yield False
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _read_key() -> str:
    """One keypress, with a bare Escape told apart from an escape sequence.

    Escape is also the first byte of every arrow key and function key, so a
    lone ``\x1b`` is only really Escape when nothing follows it immediately.
    Anything that does follow is drained and reported as an empty string, so
    pressing an arrow key never reads as an abort.
    """
    ch = sys.stdin.read(1)
    if ch != ESC:
        return ch
    if not _stdin_ready(0.05):
        return ESC
    while _stdin_ready(0.0):
        sys.stdin.read(1)
    return ""


def _read_question(prompt: str) -> Optional[str]:
    """Read one line. Returns None if the user pressed Escape or Ctrl+C.

    A hand-rolled editor because ``input()`` cannot see Escape at all — it hands
    back a line only on Enter, and swallows the key we most need.
    """
    if not sys.stdin.isatty():
        try:
            return input(prompt).strip() or None
        except (EOFError, KeyboardInterrupt):
            return None

    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf: list[str] = []
    with _cbreak():
        while True:
            ch = _read_key()
            if ch == ESC or ch == "\x03":          # Escape, Ctrl+C
                sys.stdout.write("\n")
                return None
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                return "".join(buf).strip() or None
            if ch in ("\x7f", "\b"):              # Backspace
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
            elif ch == "\x15":                     # Ctrl+U
                sys.stdout.write("\b \b" * len(buf))
                buf.clear()
            elif ch and ch.isprintable():
                buf.append(ch)
                sys.stdout.write(ch)
            sys.stdout.flush()


class _EscWatcher:
    """Watches stdin for Escape while an answer streams, and sets ``event``.

    Runs only on a TTY. The thread is a daemon and polls rather than blocking
    on read, so it can be told to stand down the moment streaming ends without
    leaving a reader parked on the same stdin ``_wait_for_key`` needs next.
    """

    def __init__(self, event: threading.Event) -> None:
        self._event = event
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # The watcher owns cbreak for the whole streaming phase. Without it the
        # tty is back in canonical mode after the prompt, so the Escape byte
        # sits in the line buffer until Enter and select() never reports it —
        # the abort then silently does nothing until the timeout fires.
        self._stack: Optional[contextlib.ExitStack] = None

    def _run(self) -> None:
        while not self._done.is_set():
            if not _stdin_ready(0.1):
                continue
            if _read_key() == ESC:
                self._event.set()
                return

    def __enter__(self) -> "_EscWatcher":
        if sys.stdin.isatty():
            self._stack = contextlib.ExitStack()
            self._stack.enter_context(_cbreak())
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if self._stack is not None:
            self._stack.close()
            self._stack = None


def _wait_for_key() -> None:
    print("\n\033[2m— any key to close —\033[0m", end="", flush=True)
    if not sys.stdin.isatty():
        return
    try:
        with _cbreak():
            sys.stdin.read(1)
    except (OSError, termios.error):
        pass


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    arg_pane = args[0] if len(args) > 0 else ""
    arg_session = args[1] if len(args) > 1 else ""

    sid, source = _resolve_session_id(arg_session)
    if sid is None:
        print(f"\033[31mQuick ask unavailable: {source}.\033[0m")
        print(
            "\033[2mThis works while attached to a multiverse agent session. "
            "If you are on the monitor dashboard, press 'a' there instead."
            "\033[0m"
        )
        _wait_for_key()
        return 1

    target, err = _resolve_target(sid)
    if target is None:
        print(f"\033[31m{err}\033[0m")
        _wait_for_key()
        return 1

    pane = _resolve_pane(arg_pane, sid)
    screen = _capture_pane(pane) if pane else ""
    header = f"\033[2mquick ask → {target.name}"
    if not screen:
        header += "  (no screen context)"
    header += "   ·  esc aborts"
    if os.environ.get("MULTIVERSE_ASK_DEBUG"):
        header += f"  [sid {sid} via {source}; pane {pane or 'none'}]"
    header += "\033[0m"
    print(header)

    question = _read_question("\033[36m?\033[0m ")
    if not question:
        return 0

    started = time.monotonic()
    spinner = _Spinner(f"asking {target.name}")
    spinner.start()
    first_word: Optional[float] = None

    def on_delta(text: str) -> None:
        # The spinner and the answer share one line of the terminal, so the
        # spinner has to be torn down by the first delta rather than at the end
        # — otherwise its next frame overwrites the opening words.
        nonlocal first_word
        if first_word is None:
            first_word = time.monotonic() - started
            spinner.stop()
        sys.stdout.write(text)
        sys.stdout.flush()

    cancel = threading.Event()
    try:
        with _EscWatcher(cancel):
            result = ask_one_streaming(
                _compose(question, screen), target, timeout=TIMEOUT,
                idle_timeout=IDLE_TIMEOUT, on_delta=on_delta, cancel=cancel,
            )
    finally:
        spinner.stop()
    elapsed = time.monotonic() - started

    if result.response:
        print()
        if cancel.is_set():
            print("\033[33m… aborted\033[0m")
    elif cancel.is_set():
        print("\033[33maborted\033[0m")
    elif result.error:
        print(f"\033[31m{result.error}\033[0m")
    else:
        print("\033[33mNo answer came back.\033[0m")

    timing = f"{elapsed:0.1f}s"
    if first_word is not None:
        timing = f"{first_word:0.1f}s to first word · {timing} total"
    print(f"\n\033[2m{timing} · fork discarded\033[0m")
    _wait_for_key()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
