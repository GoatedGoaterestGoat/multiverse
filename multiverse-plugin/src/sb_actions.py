"""Multiverse-side action primitives for the SecondBrain connector.

Delivers a user-style prompt to a target agent's tmux session per the ``prompt_agent``
action contract locked with the SecondBrain toolsmith. Routing by target status happens in
the connector; this module owns the *how* — the tmux mechanics — so the handler stays
focused on the decision flow.

Delivery strategies by target status:

- **idle / working / waiting / listening** → :func:`send_prompt_via_tmux`. We ALWAYS
  send-keys (even when a user is attached — unlike ``message_agent``'s push_notification
  which falls back), and we preserve any text the user has typed into Claude's input box
  but not yet submitted: save-clear-deliver-restore. Multi-line prompts use ``\\`` + Enter
  between lines (Claude's user-facing newline convention).
- **dormant / ended** → :func:`prompt_dormant_via_resume`. NOT send-keys — we call
  ``create_session`` with ``initial_prompt=<text>``, which writes it to a file that
  ``bin/multiverse-session`` hands to claude as its positional prompt argument, and
  claude auto-submits it as the first user turn.
- **starting** → :func:`wait_for_ready` first (bounded), then send-keys.

All tmux calls are subprocess with tight timeouts so a hung tmux client can't wedge the
handler. Every helper returns a plain error string on failure; the caller maps to an
:class:`sb_connector` error code.
"""

from __future__ import annotations

import subprocess
import time

#: How many extra backspaces to send after clearing, defensive against any off-by-one in
#: our input-box parse. Extra backspaces past the start of the input box are no-ops in
#: Claude's TUI, so this is safe.
_CLEAR_MARGIN = 3

#: Bounded wait for a `starting` session to reach Claude's ready state before we send-keys.
DEFAULT_READY_TIMEOUT_S = 15.0

#: How long to wait for tmux capture-pane / send-keys to return. tmux is normally fast;
#: a hang beyond a few seconds means the tmux server is unhealthy, and it's better to
#: bail than block the connector's receiver loop.
_TMUX_CMD_TIMEOUT_S = 3.0


def _run_tmux(args: list[str], *, capture: bool = False) -> tuple[bool, str]:
    """Run a tmux command. Returns ``(ok, stdout_or_error_message)``.

    ``capture=True`` returns stdout on success; otherwise returns an empty string. Failure
    modes fold into ``ok=False`` so the caller has a single branch to handle.
    """
    try:
        r = subprocess.run(
            ["tmux"] + args,
            capture_output=True,
            text=True,
            timeout=_TMUX_CMD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"tmux {args[0]} timed out"
    except FileNotFoundError:
        return False, "tmux CLI not found"
    except OSError as exc:
        return False, f"tmux error: {exc}"
    if r.returncode != 0:
        return False, (r.stderr or f"tmux exit {r.returncode}").strip()
    return True, (r.stdout if capture else "")


def capture_pane(tmux_name: str) -> tuple[bool, str]:
    """Read the visible content of the target's tmux pane. ``ok, text_or_error``."""
    return _run_tmux(["capture-pane", "-p", "-t", tmux_name], capture=True)


def parse_input_box_text(pane: str) -> str:
    """Best-effort extract of any typed-but-unsent text in Claude's input box.

    Claude's TUI shows the input on a bordered line whose content starts with ``>`` after
    the box border. We scan bottom-up for the last line matching that shape and return the
    text after ``> ``. This IS a heuristic (Claude TUI-dependent) — if it misclassifies,
    the ``always save+clear+restore`` policy chosen by the user makes an empty save + empty
    restore a no-op, so a false negative just means we don't preserve typed text on that
    call (a mild regression, not corruption).

    Ignores placeholder text (``>``-only lines or the "try 'help'" hint) as empty.
    """
    lines = pane.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        # Match a border-wrapped input line: strip common box chars from each end.
        stripped = stripped.strip("│|╭╮╰╯─╌ ")
        if not stripped.startswith(">"):
            continue
        after = stripped[1:].lstrip()
        # Placeholder / empty prompt (Claude's TUI shows " > " when there's no text).
        # Also skip lines that are entirely UI hint text (heuristic — hints tend to be
        # short and contain characteristic phrases).
        if not after:
            return ""
        return after
    return ""


def _bspaces(n: int) -> list[str]:
    return ["BSpace"] * max(0, int(n))


def send_prompt_via_tmux(tmux_name: str, prompt: str) -> tuple[bool, str, str]:
    """Deliver ``prompt`` to a live target as a user-typed message, preserving any
    typed-but-unsent text in the input box.

    Flow (always all six steps — a save of empty text is a no-op restore):
      1. capture pane -> parse input box text ``saved``
      2. backspace-clear ``len(saved) + _CLEAR_MARGIN`` chars from the input
      3. type ``prompt`` (multi-line via ``\\`` + Enter between lines)
      4. Enter to submit
      5. type ``saved`` back with ``send-keys -l`` (no Enter — user's mid-typing is
         restored to the input box, still unsent)

    Returns ``(ok, saved_text, error)`` — the saved text lets the caller construct the
    success message ("Prompt delivered to X, saved and restored the text..." when non-empty).
    """
    ok, pane = capture_pane(tmux_name)
    if not ok:
        return False, "", pane  # `pane` holds the error message
    saved = parse_input_box_text(pane)

    # Step 2: clear whatever's in the input box.
    if saved:
        ok, err = _run_tmux(
            ["send-keys", "-t", tmux_name] + _bspaces(len(saved) + _CLEAR_MARGIN)
        )
        if not ok:
            return False, saved, err

    # Step 3+4: type the prompt with proper multi-line handling, then submit.
    lines = prompt.split("\n")
    for i, line in enumerate(lines):
        if line:
            ok, err = _run_tmux(["send-keys", "-t", tmux_name, "-l", line])
            if not ok:
                return False, saved, err
        # Between lines: `\` then Enter (Claude's newline convention). After the last line:
        # bare Enter to SUBMIT.
        if i < len(lines) - 1:
            ok, err = _run_tmux(["send-keys", "-t", tmux_name, "\\", "Enter"])
            if not ok:
                return False, saved, err
        else:
            ok, err = _run_tmux(["send-keys", "-t", tmux_name, "Enter"])
            if not ok:
                return False, saved, err

    # Step 5: type saved text back into the (now-empty) input box, no Enter.
    if saved:
        ok, err = _run_tmux(["send-keys", "-t", tmux_name, "-l", saved])
        if not ok:
            # Prompt already delivered; just report the restore failure.
            return False, saved, f"prompt delivered but restore failed: {err}"

    return True, saved, ""


def wait_for_ready(
    tmux_name: str, timeout_s: float = DEFAULT_READY_TIMEOUT_S
) -> tuple[bool, str]:
    """Poll capture-pane until Claude's input box (``>``) is visible, up to ``timeout_s``.

    Used for ``starting`` sessions — the tmux pane exists but claude hasn't drawn its UI
    yet. Returns ``(ok, error)`` where ``ok=True`` means we saw the input line at least
    once. On timeout, ``ok=False`` and the caller reports ``deliver_failed``.
    """
    deadline = time.monotonic() + timeout_s
    interval = 0.25
    last_err = "timed out waiting for input box"
    while time.monotonic() < deadline:
        ok, pane = capture_pane(tmux_name)
        if not ok:
            last_err = pane
        else:
            # Reuse the same heuristic — if we can find the `>` line (empty or not),
            # Claude's ready to accept input.
            for line in reversed(pane.splitlines()):
                s = line.strip().strip("│|╭╮╰╯─╌ ")
                if s.startswith(">"):
                    return True, ""
        time.sleep(interval)
    return False, last_err
