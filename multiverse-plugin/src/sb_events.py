"""Perception-event payload composition (pure): Multiverse spawn signals → §4b events.

These are **pure functions** of a spawn-request dict (+ decision) — no filesystem, no
imports of the spawn data layer — so they can't create an import cycle and are trivial to
unit-test. Multiverse's spawn choke-points (``spawn_requests.write_spawn_request`` /
``write_resolution``) call the recorder in :mod:`sb_notify`, which uses these to shape the
event a connector will relay to SecondBrain.

Contract locked with the SecondBrain design agent (DESIGN §4b):
  - ``agent.spawn_request`` → ``{new_agent_name, summary, full, request_id}``
  - ``user.spawn_decision`` → ``{request_id, decision, requesting_agent, requested_name,
    final_name, renamed, summary}``
In both, ``summary`` is a connector-produced sentence SB delivers ~verbatim.
"""

from __future__ import annotations

import json

#: How much of the spawn instruction to inline in the default-visible summary.
SPAWN_PROMPT_TRUNC = 240

#: Max length of an agent.response summary (a cheap first-cut summary; upgradeable to a
#: real LLM summary later with zero wire change — ``summary`` stays a plain string).
RESP_SUMMARY_TRUNC = 220


def spawn_request_event_fields(req: dict) -> tuple[str, dict]:
    """``(requesting_agent, payload)`` for an ``agent.spawn_request`` event.

    From the spawn-request dict: ``from`` = requesting agent (-> envelope ``agent``),
    ``default_name`` = suggested name for the new agent, ``instruction`` = the full
    prompt, ``id`` = the request id.
    """
    requesting = req.get("from") or "?"
    new_name = req.get("default_name") or "?"
    full = req.get("instruction") or ""
    truncated = full[:SPAWN_PROMPT_TRUNC].rstrip()
    if len(full) > SPAWN_PROMPT_TRUNC:
        truncated += "…"
    summary = (
        f"agent '{requesting}' requests to spawn a new agent '{new_name}' "
        f"with prompt: {truncated}"
    )
    payload = {
        "new_agent_name": new_name,
        "summary": summary,
        "full": full,  # inline; SB caches it by event_id for view_full
        "request_id": req.get("id") or "",
    }
    return requesting, payload


def spawn_decision_event_fields(
    req: dict, status: str, name: str | None
) -> tuple[str, dict] | None:
    """``(requesting_agent, payload)`` for a ``user.spawn_decision`` event, or ``None``.

    ``status`` is the resolution status the monitor/tool wrote (``"accepted"`` /
    ``"rejected"`` / ``"timeout"``); ``name`` is the user's chosen name on accept. Returns
    ``None`` for anything that isn't an actual user approve/decline (e.g. ``"timeout"`` —
    the user never responded, so it's not a decision worth learning from).

    Summary wording is the user's exact phrasing:
      - decline:              "user rejected <X>'s request to spawn agent <Y>"
      - approve, unchanged:   "user approved <X>'s request to spawn agent <Y>"
      - approve, renamed:     "user approved <X>'s spawn agent request and renamed the
                               new agent <Z>"
    """
    requesting = req.get("from") or "?"
    requested_name = req.get("default_name") or "?"

    if status == "accepted":
        decision = "approve"
        final_name = name or requested_name
        renamed = bool(name) and name != requested_name
        if renamed:
            summary = (
                f"user approved {requesting}'s spawn agent request and renamed the "
                f"new agent {final_name}"
            )
        else:
            summary = f"user approved {requesting}'s request to spawn agent {requested_name}"
    elif status == "rejected":
        decision = "decline"
        final_name = None
        renamed = False
        summary = f"user rejected {requesting}'s request to spawn agent {requested_name}"
    else:
        return None  # timeout / unknown — not a user decision

    payload = {
        "request_id": req.get("id") or "",
        "decision": decision,
        "requesting_agent": requesting,
        "requested_name": requested_name,
        "final_name": final_name,
        "renamed": renamed,
        "summary": summary,
    }
    return requesting, payload


#: Cap for the tool_input inline in ``agent.permission_request.summary``. The full
#: tool_input rides in ``payload.tool_input`` verbatim; this only truncates the
#: human-facing sentence so a paste of a huge file body doesn't blow out the turn text.
_PERM_SUMMARY_INPUT_TRUNC = 400


def _summarize_tool_input(tool_name: str, tool_input) -> str:
    """One-line human description of a tool call for the permission summary. Prefers the
    field that IS the action (Bash.command, Write/Edit.file_path, WebFetch.url); falls
    back to a compact JSON for unknown tools. Truncates to keep the sentence tight —
    the full ``tool_input`` is in the payload for anyone who needs it."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    tn = (tool_name or "").strip()
    if tn == "Bash":
        cmd = str(ti.get("command", "")).strip()
        core = f"`{cmd}`" if cmd else "(no command)"
    elif tn in {"Write", "Edit", "MultiEdit", "NotebookEdit", "Read"}:
        path = ti.get("file_path") or ti.get("path") or ""
        core = f"`{path}`" if path else "(no path)"
    elif tn == "WebFetch":
        url = ti.get("url") or ""
        core = f"`{url}`" if url else "(no url)"
    elif tn == "Task":
        desc = (ti.get("description") or "").strip()
        core = f"`{desc}`" if desc else "(no description)"
    else:
        try:
            core = "`" + json.dumps(ti, separators=(",", ":"), ensure_ascii=False) + "`"
        except (TypeError, ValueError):
            core = "(input not renderable)"
    if len(core) > _PERM_SUMMARY_INPUT_TRUNC:
        core = core[: _PERM_SUMMARY_INPUT_TRUNC - 1].rstrip() + "…`"
    return core


def agent_permission_request_event_fields(
    agent_name: str, tool_use_id: str, tool_name: str, tool_input, transcript: str
) -> dict:
    """``agent.permission_request`` payload — first-kind of the permission pair. Fires
    when Claude Code shows the user a permission prompt for a tool call the agent wants
    to make. ``tool_use_id`` is the pair key echoed by ``user.permission_decision``.
    ``summary`` is connector-composed and delivered ~verbatim ("agent 'X' needs permission
    to run Bash: `rm -rf build`"); ``tool_input`` is the raw tool call verbatim (the
    eventual ``answer_permission_prompt`` action will want it). ``full`` is the partial
    transcript = "why" context, via the same peek/commit mechanism as agent.question."""
    core = _summarize_tool_input(tool_name, tool_input)
    summary = f"agent '{agent_name}' needs permission to run {tool_name}: {core}"
    return {
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "summary": summary,
        "full": transcript,
    }


def user_permission_decision_event_fields(
    agent_name: str,  # noqa: ARG001 - kept for API parity; identity is on the envelope
    tool_use_id: str,
    decision: str,
    tool_name: str,
    tool_input,
) -> dict:
    """``user.permission_decision`` payload — close of the permission pair. Shares
    ``tool_use_id`` with the earlier ``agent.permission_request``. ``decision`` is
    ``"allow"`` or ``"deny"`` (Claude Code UX wording, per the locked contract with SB).

    ``summary`` NAMES THE TOOL AND COMMAND rather than just the verdict, so the event
    stands on its own. It used to be a bare "user allowed", on the reasoning that the
    held first already carried the tool + command — true only if the pair actually
    combines. It doesn't always: SB drops a held first whose close misses its pairing
    window, and a human taking longer than that window to answer a prompt is ordinary
    (79 minutes, twice, on a live workspace). The close then arrives orphaned, and a
    bare verdict renders as "the user responded to a permission prompt — allow" with
    no indication of what was permitted. A decision with no question attached is worse
    than silence.

    The cost is mild redundancy when the pair does combine, which is cosmetic; SB has
    both halves in that case and can trim. Being unreadable when it doesn't combine is
    not recoverable at all.

    No transcript — agent was blocked between prompt and decision, nothing happened."""
    decision = "deny" if str(decision).lower().startswith("d") else "allow"
    verdict = "user allowed" if decision == "allow" else "user denied"
    # Name the tool as well as its argument: "`/etc/hosts`" alone doesn't say whether
    # the agent wanted to read it or overwrite it.
    detail = _summarize_tool_input(tool_name, tool_input)
    parts = [verdict, tool_name.strip(), detail if detail not in ("`{}`", "") else ""]
    summary = " ".join(x for x in parts if x)
    return {
        "tool_use_id": tool_use_id,
        "decision": decision,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "summary": summary,
    }


def _last_assistant_line(full: str) -> str:
    """The last ``[assistant] …`` line in a rendered partial transcript (for a cheap
    idle summary), whitespace-collapsed and length-capped. "" if none."""
    last = ""
    for line in full.splitlines():
        if line.startswith("[assistant] "):
            last = line[len("[assistant] "):]
    last = " ".join(last.split())
    return last[:160] if last else ""


def workspace_connected_event_fields(agents_summary: dict[str, str] | None = None) -> dict:
    """``workspace.connected`` payload — FIRST-EVER connect only (DESIGN §4b).

    Contract history:
      - 2026-07-12 (original): fired on every (re)connect, distinguished by
        ``first_connect: bool`` — first-ever carried the workspace_summary, reconnects
        carried an empty ``{}``.
      - 2026-07-16 (current): reconnect notifications are net-negative on the SB agent's
        context and are now silently dropped SB-side. We stopped emitting them entirely
        (see ``_emit_workspace_connected`` in the connector), so this composer only ever
        builds the first-ever shape.

    Payload:
      ``{first_connect: True, workspace_summary: {<agent>: <text>, ...}}``

    The dict maps each named agent's name to their answer to
    "Please give a concise summary of what you have achieved so far in this workspace."
    (via Multiverse's ``ask_all`` fork). Failed asks (timeout, subprocess error, ...)
    get a bracketed placeholder like ``"[ask failed: timed out after 60s]"``. Empty
    workspace (no agents) → ``workspace_summary`` present but ``{}``.

    ``first_connect: True`` is retained on the wire even though it's now redundant
    (every emitted event is a first-connect): SB's ingest keys off it, and dropping it
    would be a wire break.
    """
    return {"first_connect": True, "workspace_summary": agents_summary or {}}


def agent_created_event_fields(initial_prompt: str | None = None) -> dict:
    """``agent.created`` payload (DESIGN §4b).

    Envelope ``agent`` carries the new agent's name; the payload is minimal:
    ``{initial_prompt}`` if the agent was created via a spawn request (the requesting
    agent's instruction), or ``{}`` if the agent was user-created from the dashboard
    (name is the only input).

    Contract locked with the SB design agent (2026-07-12): SB's ingest renders "agent
    'X' was created" for the empty-payload case and appends "Initial task: <text>" when
    ``initial_prompt`` is present. ``agent_id`` is Multiverse-internal — not carried.
    """
    payload: dict = {}
    if initial_prompt:
        payload["initial_prompt"] = initial_prompt
    return payload


def user_prompt_event_fields(agent_name: str, text: str, transcript: str) -> dict:  # noqa: ARG001
    """``user.prompt`` payload (DESIGN §4b): ``{text, overview_since_last, full}``.

    Fires when the user submits a prompt to a session (typed at the terminal, delivered
    via SB's own ``prompt_agent`` send-keys, or auto-submitted as a resume's positional
    prompt — all three route through the same UserPromptSubmit hook so a single event
    kind covers them cleanly).

    Contract (locked with SB):
      - ``text`` — the prompt verbatim.
      - ``overview_since_last`` — CHEAP connector-composed summary of AGENT activity
        between the last transcript delivery and now. Empty (``""``) in the steady state
        — because if the agent was idle when the user prompted, then between the last
        ``agent.response`` (which advanced the marker) and this new user prompt, the
        agent literally did nothing worth summarizing. Only non-empty when the user
        prompts a currently-WORKING agent (mid-turn, no ``agent.response`` since the last
        marker); in that case we surface the last completed assistant line so SB knows
        what the agent was mid-way through. Same cheap-now-LLM-later pattern as
        ``agent.response.summary`` — zero wire change on upgrade.
      - ``full`` — the partial transcript from the delivery marker → now, INCLUDING this
        new user prompt at the end. Kept deliberately redundant with ``text`` (SB
        confirmed this reads cleaner than a "jagged" full that stops just before the
        current prompt) — ``full`` is view-on-demand, held out of the default turn
        context.

    Design note on the marker: there's ONE delivery marker per (workspace, agent), shared
    by every ``.full``-bearing event (``agent.response`` / ``user.prompt`` /
    ``agent.question`` / ``agent.permission_request``). Any of them advances it. So in
    steady on-time operation:
      1. Agent finishes a turn → ``agent.response`` fires + marker advances to that point.
      2. User submits next prompt → transcript window since marker contains ONLY the new
         user turn → no assistant lines → ``overview_since_last`` is ``""``.
    The field carries information only in the Q3 case (prompted mid-turn) and in
    offline-recovery paths where multiple activities accumulate before delivery.
    """
    last = _last_assistant_line(transcript)
    # Steady state (agent was idle at prompt time, or dormant, or brand new) → no agent
    # activity to summarize. The current user prompt is separately in ``text``, so
    # counting it as "activity" would be misleading redundant noise. Return empty and
    # let SB's parenthetical drop out.
    overview = last if last else ""
    return {
        "text": text,
        "overview_since_last": overview,
        "full": transcript,
    }


def _render_questions_verbatim(questions: list) -> str:
    """Render an AskUserQuestion payload (1..N questions, each with options) as a
    human-readable text block that SB delivers ~verbatim as the turn — nothing truncated,
    no view_full round-trip needed for the question itself."""
    if not questions:
        return "(no questions)"
    lines: list[str] = []
    for i, q in enumerate(questions):
        prefix = f"Q{i+1}: " if len(questions) > 1 else ""
        lines.append(f"{prefix}{q.get('question') or '(no text)'}")
        opts = q.get("options") or []
        if q.get("multiSelect"):
            lines.append("  (choose one or more)")
        for opt in opts:
            label = opt.get("label") or "?"
            desc = (opt.get("description") or "").strip()
            lines.append(f"  - {label}: {desc}" if desc else f"  - {label}")
        lines.append("")
    return "\n".join(lines).rstrip()


def agent_question_event_fields(
    agent_name: str, question_id: str, questions: list, transcript: str
) -> dict:
    """``agent.question`` payload — the first-kind of the ask-tool question pair.

    Per the user: the full question(s) + choices ride in ``summary`` VERBATIM (not
    truncated); SB delivers that as the turn text without needing view_full for the
    question. ``full`` carries the partial transcript from the last marker → now, so SB has
    the "why" context (same transcript mechanism as agent.response). Structured
    ``questions`` are retained for the eventual ``answer_question`` action; ``question_id``
    (= the AskUserQuestion tool_use_id) is the pair key echoed by ``user.answered``.
    """
    rendered = _render_questions_verbatim(questions)
    summary = f"agent '{agent_name}' asks:\n{rendered}"
    return {
        "question_id": question_id,
        "questions": questions,
        "summary": summary,
        "full": transcript,
    }


#: Marker for a user-typed answer (the always-present "Other" option). Makes it obvious
#: — in the summary SB delivers verbatim — that the user rejected every offered choice
#: and wrote in their own answer, which is a stronger signal than "picked from the menu".
_CUSTOM_TAIL = " (typed by user)"


def _mark_custom(text: str) -> str:
    return f"'{text.strip()}'{_CUSTOM_TAIL}" if text and text.strip() else ""


def _render_one_answer(question: dict | None, answer, note) -> str:
    """Render one question's answer, distinguishing preset picks from user-typed text.

    A pick is "custom" (Other) when either the answer is not among the question's
    ``options.label`` set, or the answer is the literal ``"Other"`` with a ``notes``
    annotation carrying the typed text. Preset picks with an accompanying note keep the
    note parenthetically (some ask flows let the user annotate any pick, not just Other).
    Multi-select splits on commas and marks each part independently.

      Postgres, no note                        → ``Postgres``
      Postgres, note "prefer TLS"              → ``Postgres (prefer TLS)``
      "Other", note "SQLite"                   → ``'SQLite' (typed by user)``
      "SQLite" alone (inline Other)            → ``'SQLite' (typed by user)``
      multiSelect ci,cd + custom "smoke"       → ``ci, cd, 'smoke' (typed by user)``
    """
    q = question or {}
    offered = {(o.get("label") or "").strip() for o in (q.get("options") or []) if o.get("label")}
    multi = bool(q.get("multiSelect"))
    ans = ("" if answer is None else str(answer)).strip()
    n = ("" if note is None else str(note)).strip()

    # "Other" + notes: the typed text IS the pick (SB sees it was custom).
    if ans.lower() == "other" and n:
        return _mark_custom(n)
    # No answer but a note → note is the typed pick.
    if not ans and n:
        return _mark_custom(n)

    def _fmt(part: str) -> str:
        part = part.strip()
        if not part:
            return ""
        # If we know the offered options and this part isn't among them, it's custom.
        # (No `offered` known → we can't tell; fall through as a plain pick.)
        return part if not offered or part in offered else _mark_custom(part)

    if multi:
        parts = [p for p in (x.strip() for x in ans.split(",")) if p]
        rendered = ", ".join(f for f in (_fmt(p) for p in parts) if f) or "(no answer)"
    else:
        rendered = _fmt(ans) or "(no answer)"

    # Preset + note (a clarification, not the Other-shortcut): append parenthetically.
    if n and ans.lower() != "other":
        rendered = f"{rendered} ({n})"
    return rendered


def _render_answer_picks(answers, questions: list | None, annotations) -> str:
    """Render the user's selection(s) as JUST the pick(s) — no restated question — with
    user-typed ("Other") answers explicitly marked so SB and its agent can tell them
    apart from preset picks.

    SB's pair-combine joins ``held.summary`` (the question + options) with this via
    ``" → "``, so restating the question would be noise. Handles the canonical
    ``AskUserQuestion`` shape (``answers`` dict keyed by question text, optional parallel
    ``annotations`` dict of ``{notes, preview}``), and a defensive list shape (per-index
    aligned to ``questions``). Multi-select within a question → comma-joined; across
    questions → semicolon-separated.
    """
    ann = annotations if isinstance(annotations, dict) else {}
    qs = questions or []
    q_by_text = {(q.get("question") or ""): q for q in qs}

    def _note_for(qtext: str):
        entry = ann.get(qtext) if isinstance(qtext, str) else None
        return (entry or {}).get("notes") if isinstance(entry, dict) else None

    picks: list[str] = []

    if isinstance(answers, dict):
        for qtext, val in answers.items():
            q = q_by_text.get(qtext)
            picks.append(_render_one_answer(q, val, _note_for(qtext)))
    elif isinstance(answers, list):
        for i, val in enumerate(answers):
            q = qs[i] if i < len(qs) else None
            qtext = (q.get("question") if q else "") or ""
            note = _note_for(qtext)
            if isinstance(val, dict):  # accept {answer, notes?} shape defensively
                inner_note = val.get("notes")
                val = val.get("answer") or val.get("selected") or ""
                note = note or inner_note
            if isinstance(val, list):
                val = ", ".join(str(x) for x in val if x)
            picks.append(_render_one_answer(q, val, note))
    else:
        return str(answers) if answers else "(no answer)"

    return "; ".join(p for p in picks if p) or "(no answer)"


def user_answered_event_fields(
    agent_name: str,  # noqa: ARG001 - kept for API parity; identity is on the envelope
    question_id: str,
    answers,
    questions: list | None = None,
    annotations: dict | None = None,
) -> dict:
    """``user.answered`` payload — close of the ask-tool question pair. Shares
    ``question_id`` with the earlier ``agent.question``. ``summary`` is JUST the pick(s)
    (SB restates the question from the held first's summary on pair-combine).

    An ``"Other"`` custom-text answer is preserved even when the tool routes it via
    ``annotations.notes`` rather than inline in ``answers``; both structured fields ride
    in the payload so SB can render as it likes. No transcript here — the agent was
    BLOCKED in the ask between PreToolUse and PostToolUse.
    """
    picks = _render_answer_picks(answers, questions, annotations)
    payload = {
        "question_id": question_id,
        "answers": answers,
        "questions": questions,
        "summary": picks,
    }
    if annotations:
        payload["annotations"] = annotations
    return payload


def agent_response_event_fields(agent_name: str, full: str) -> dict:
    """The ``agent.response`` payload (DESIGN §4b): the agent finished a turn **normally**
    and went idle → ``{state: "idle", summary, full}``.

    ``agent.response`` is ONLY the normal idle finish. Waiting-on-permission and
    waiting-on-a-question (the ask tool) are their own separate event pairs, not this event
    — so there is no ``waiting`` state and no ``waiting_detail`` here. ``full`` is the
    partial transcript from :mod:`sb_transcript`; ``summary`` is a **cheap** connector-
    produced string (the last assistant line) — upgradeable to a real LLM summary later
    with no wire change. ``state`` is retained (always ``"idle"``) to match SB's shape.
    """
    tail = _last_assistant_line(full)
    summary = (
        f"agent '{agent_name}' finished a turn: {tail}"
        if tail
        else f"agent '{agent_name}' finished a turn"
    )
    return {"state": "idle", "summary": summary[:RESP_SUMMARY_TRUNC].rstrip(), "full": full}
