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


def _last_assistant_line(full: str) -> str:
    """The last ``[assistant] …`` line in a rendered partial transcript (for a cheap
    idle summary), whitespace-collapsed and length-capped. "" if none."""
    last = ""
    for line in full.splitlines():
        if line.startswith("[assistant] "):
            last = line[len("[assistant] "):]
    last = " ".join(last.split())
    return last[:160] if last else ""


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
