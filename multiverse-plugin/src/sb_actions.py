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


import re

# Claude Code's input box is drawn with box-drawing chars. The input line looks like
# ``│ > <typed text> │`` (padded with spaces to the box width), preceded within a few
# lines by a top border ``╭───...───╮``.
#
# We had a live E2E failure with a looser heuristic that matched ANY ``>`` after
# stripping box chars — that misfires on inter-agent message quote blocks (``> ...``
# prefix), tool logs, system reminders, and anywhere else a ``>`` character appears in
# rendered pane content. This stricter version requires BOTH the specific input-line
# shape AND a nearby top-border line, which the false-positive scenarios don't satisfy.
_INPUT_LINE_RE = re.compile(r"^│\s*>\s*(.*?)\s*│\s*$")
_TOP_BORDER_LOOKBACK = 6  # lines to scan above for ╭...╮


def parse_input_box_text(pane: str) -> str:
    """Best-effort extract of any typed-but-unsent text in Claude's input box.

    Requires the FULL Claude-input-box shape: a line matching ``│ > <text> │`` (leading
    box border, ``>`` prompt, trailing box border) AND a top-border line ``╭...╮`` within
    :data:`_TOP_BORDER_LOOKBACK` lines above. A ``>`` in an inter-agent message quote
    block, a system reminder, or a tool's output no longer matches — those lack the
    left-border pattern and/or the accompanying top border.

    Failure mode is still safe: a mismatch returns ``""`` (no save, no restore), which
    just means we don't preserve typed text on that call — a mild regression, not
    corruption of the pane.
    """
    lines = pane.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].rstrip()
        m = _INPUT_LINE_RE.match(line)
        if not m:
            continue
        # Confirm a Claude top-border line sits within a few lines above.
        for j in range(max(0, i - _TOP_BORDER_LOOKBACK), i):
            top = lines[j]
            if "╭" in top and "╮" in top:
                return m.group(1)
        # Matched an input-shaped line but no accompanying top border → skip.
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


#: Down-count large enough to always dead-end at "Chat about this" from anywhere in an
def _find_label_index(s: str, options: list) -> int | None:
    """0-based index of the option whose ``label`` matches ``s`` exactly; ``None`` if
    no exact match. Non-matching text is treated as a custom 'Other' typed answer, per
    the answer_question contract."""
    for i, opt in enumerate(options):
        if isinstance(opt, dict) and str(opt.get("label", "")) == s:
            return i
    return None


def _clean_text(s: str) -> str:
    """Strip newlines/tabs from custom typed text. Enter is our navigation key, so a
    stray newline in typed text would prematurely advance the modal."""
    return s.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def build_answer_key_sequence(questions: list, answers: list) -> list:
    """Compute the send-keys batches to answer a pending AskUserQuestion modal.

    Returns a list of ``("keys"|"text", value)`` batches: ``keys`` is a list of tmux
    key names (``["Down","Down","Enter"]``) sent as ``tmux send-keys ...``; ``text`` is
    a string sent as ``tmux send-keys -l <text>`` (literal typing, no key-name parsing).

    Modal layout (empirically verified with the user, 2026-07-13):
      Single-select Q with P presets: positions 1..P (presets), P+1 (Other = typed
        answer, focused by navigating to it), P+2 (Chat about this = dead-end sink).
      Multi-select Q with P presets: 1..P (presets), P+1 (Other), P+2 (Next/Submit),
        P+3 (Chat about this).

    Navigation semantics:
      - Down goes through everything in order, dead-ends at Chat about this.
      - Up cycles within {presets + Other}, wrapping: from position 1 Up → Other.
        For multi-select, Up from Chat first passes through Next then Other.
      - Single-select: Enter on a preset selects AND advances.
      - Multi-select: Enter (and Space) toggles a preset; advancement is via
        Next/Submit + Enter. Typing in Other implicitly selects it (no extra key).
      - After a question advances, focus resets to position 1 of the next question.
      - After the last question advances, we're on the submission confirmation page;
        one more Enter submits and closes the modal.

    Strategy:
      - Ground once at start: ``Left × num_questions`` (safety in case focus was on
        a later question), then ``Down × (P + 1 or P + 2)`` (max needed to dead-end at
        Chat of Q1).
      - Q1: navigate from Chat (Up-arrow math).
      - Q2+: navigate from position 1 (Down-arrow math + Up-wrap for Other).
      - After last question: one final Enter to submit.
    """
    n = len(questions)
    if n == 0:
        return []

    # Grounding: navigate to Q1 (Left × n) then Down enough to dead-end at Chat about
    # this of Q1. Max Downs needed from any starting position ≤ Chat is:
    #   single-select: P + 1  (positions 1..P presets, P+1 Other, P+2 Chat)
    #   multi-select:  P + 2  (positions 1..P, P+1 Other, P+2 Next/Submit, P+3 Chat)
    # Down dead-ends at Chat, so overshooting is harmless; undershooting misaligns the
    # Up-arrow math below. Use exact-max.
    q1 = questions[0]
    P1 = len(q1.get("options") or [])
    q1_multi = bool(q1.get("multiSelect"))
    ground_downs = P1 + 2 if q1_multi else P1 + 1

    batches: list = [("keys", ["Left"] * n + ["Down"] * ground_downs)]

    for i, (q, a) in enumerate(zip(questions, answers)):
        opts = q.get("options") or []
        P = len(opts)
        is_multi = bool(q.get("multiSelect"))
        from_chat = (i == 0)
        if is_multi:
            batches.extend(_multi_answer_batches(opts, P, a, from_chat))
        else:
            batches.extend(_single_answer_batches(opts, P, a, from_chat))

    # Submission confirmation page → Enter to submit
    batches.append(("keys", ["Enter"]))
    return batches


def _single_answer_batches(options: list, P: int, a, from_chat: bool) -> list:
    """Single-select: pick a preset (Enter selects + advances) or type into Other
    (Enter submits typed text + advances).

    Empirical modal layout for single-select (verified E2E, 2026-07-13):
      positions 1..P (presets), P+1 (Chat about this = Down dead-end).
      There is no arrow-nav "Other" position — the free-text input is accessed
      differently (Tab or click), not via arrow keys.
    """
    a_str = a if isinstance(a, str) else (a[0] if isinstance(a, list) and a else "")
    match = _find_label_index(a_str, options)

    if from_chat:
        # Chat position = P+1. Up N reaches position P+1-N (Chat → preset P → ... → 1).
        if match is not None:
            k = match + 1  # 1-indexed preset position
            ups = P + 1 - k
            return [("keys", ["Up"] * ups + ["Enter"])]
        # Custom text path: NOT YET REVALIDATED against the corrected model. Fall back
        # to the old assumption (Up 1 = Other) — if empirical testing shows Other is
        # reachable via a different key, update here.
        return [
            ("keys", ["Up"]),
            ("text", _clean_text(a_str)),
            ("keys", ["Enter"]),
        ]
    # From position 1 (post-auto-advance)
    if match is not None:
        k = match + 1
        keys = (["Down"] * (k - 1) if k > 1 else []) + ["Enter"]
        return [("keys", keys)]
    # Custom text: NOT YET REVALIDATED (see above).
    return [
        ("keys", ["Down"] * P),
        ("text", _clean_text(a_str)),
        ("keys", ["Enter"]),
    ]


def _multi_answer_batches(options: list, P: int, a, from_chat: bool) -> list:
    """Multi-select: toggle each selected preset (Enter/Space toggles), optionally
    type into Other (implicitly selects it), then navigate to Next/Submit and Enter."""
    if isinstance(a, str):
        selections = [a] if a else []
    else:
        selections = list(a) if isinstance(a, list) else []
    preset_indexes: list[int] = []
    custom_text: str | None = None
    for sel in selections:
        m = _find_label_index(sel, options)
        if m is not None:
            preset_indexes.append(m)
        else:
            custom_text = sel  # at-most-one; caller validated

    batches: list = []
    if from_chat:
        # Empirical multi-select modal layout (mirrors the single-select correction
        # from Forge's E2E-5 analysis): positions 1..P (presets), P+1 (Next/Submit),
        # P+2 (Chat about this = Down dead-end). No arrow-nav "Other".
        # Up N from Chat reaches position P+2-N. Re-ground Downs = P + 1 (max needed
        # from position 1 to Chat).
        reground = P + 1
        for m in preset_indexes:
            k = m + 1
            ups = P + 2 - k
            batches.append(("keys", ["Up"] * ups + ["Enter"]))  # toggle
            batches.append(("keys", ["Down"] * reground))        # re-ground to Chat
        if custom_text is not None:
            # Custom text path: NOT YET REVALIDATED. Placeholder.
            batches.append(("keys", ["Up", "Up"]))
            batches.append(("text", _clean_text(custom_text)))
            batches.append(("keys", ["Down"] * reground))
        # From Chat, Up 1 = Next/Submit. Enter → advances.
        batches.append(("keys", ["Up", "Enter"]))
        return batches

    # From position 1 (post-auto-advance): walk positions in ascending order.
    pos = 1
    sorted_presets = sorted(set(preset_indexes))
    for m in sorted_presets:
        k = m + 1
        delta = k - pos
        if delta > 0:
            batches.append(("keys", ["Down"] * delta))
        elif delta < 0:
            batches.append(("keys", ["Up"] * (-delta)))
        batches.append(("keys", ["Enter"]))  # toggle
        pos = k
    if custom_text is not None:
        target = P + 1  # Other position
        delta = target - pos
        if delta > 0:
            batches.append(("keys", ["Down"] * delta))
        elif delta < 0:
            batches.append(("keys", ["Up"] * (-delta)))
        batches.append(("text", _clean_text(custom_text)))
        pos = target
    # Navigate to Next/Submit (position P+2) and Enter.
    target = P + 2
    delta = target - pos
    if delta > 0:
        batches.append(("keys", ["Down"] * delta))
    elif delta < 0:
        batches.append(("keys", ["Up"] * (-delta)))
    batches.append(("keys", ["Enter"]))
    return batches


#: Delay between individual navigation keys (Up/Down/Left/Enter/etc.). Claude Code's
#: TUI needs a render cycle between keystrokes; without a delay, keys sent in one
#: ``tmux send-keys`` batch arrive within microseconds and can be coalesced or dropped,
#: producing off-by-N errors that vary with system load. During the answer_question
#: debugging phase this is intentionally slow (300ms/key) so the user watching the
#: pane can actually track which keys land where. Can be reduced (e.g. 50ms) once
#: the arithmetic is confirmed correct.
_KEY_DELAY_S = 0.30


def answer_askuserquestion_via_tmux(
    tmux_name: str, questions: list, answers: list
) -> tuple[bool, str]:
    """Deliver an answer to a pending AskUserQuestion modal via tmux send-keys.

    Assumes the target's pane currently has the modal open (caller verified via
    :func:`sb_notify.pending_tool_data`). Builds and sends the full key sequence.
    Returns ``(ok, err)``.
    """
    import logging
    log = logging.getLogger("sb_connector")
    try:
        batches = build_answer_key_sequence(questions, answers)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not build key sequence: {exc}"
    # DIAGNOSTIC: log input + full batch sequence to daemon.log for E2E debugging.
    try:
        q_summary = [
            {"P": len(q.get("options") or []), "multi": bool(q.get("multiSelect")),
             "labels": [o.get("label") for o in (q.get("options") or [])]}
            for q in questions
        ]
        log.info("answer_askuserquestion questions=%s answers=%s", q_summary, answers)
        log.info("answer_askuserquestion batches=%s", batches)
    except Exception:  # noqa: BLE001
        pass
    for kind, val in batches:
        if kind == "keys":
            if not val:
                continue
            # Send each nav key as its own tmux invocation with a small sleep between,
            # so the TUI has time to render each keystroke. Batching multiple nav keys
            # into one send-keys call was causing keys to be dropped/coalesced.
            for i, key in enumerate(val):
                if i > 0:
                    time.sleep(_KEY_DELAY_S)
                ok, err = _run_tmux(["send-keys", "-t", tmux_name, key])
                if not ok:
                    return False, err
        else:  # "text"
            if not val:
                continue
            # Text typing (Other free-text) doesn't need per-char delays — the input
            # field buffers characters normally, unlike modal navigation.
            ok, err = _run_tmux(["send-keys", "-t", tmux_name, "-l", val])
            if not ok:
                return False, err
    return True, ""


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
