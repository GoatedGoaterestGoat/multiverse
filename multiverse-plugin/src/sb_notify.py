"""Firsthand recording of Multiverse spawn events into the SecondBrain pending queue.

This is the "connect Multiverse directly to SecondBrain" seam on the *production* side.
Instead of the connector polling ``~/.multiverse/spawn_requests/`` and racing the spawn
tool's cleanup (which deletes a rejection's resolution within ~250ms), Multiverse's own
spawn choke-points call in here **at the moment the event happens**, where the data is
authoritative and can't be missed:

  - ``spawn_requests.write_spawn_request`` → :func:`record_spawn_request`
  - ``spawn_requests.write_resolution``    → :func:`record_spawn_decision`

Each drops a **durable event record** into ``~/.multiverse/secondbrain/pending/``. The
connector daemon (:mod:`src.sb_connector`) consumes those, assigns the monotonic ``seq``,
and relays to SB with its durable-buffer/replay machinery.

**Gate (per the user):** a record is written only when this **workspace has connected to
SecondBrain before** — i.e. its ``workspace_id`` is in the connector's persisted ``seeded``
set. A never-connected workspace records nothing, so no stale queue ever accumulates.

Kept **dependency-light on purpose** (stdlib + :mod:`.paths` + :mod:`.sb_events`): it is
imported by ``spawn_requests`` — which the MCP server and monitor import — so it must not
pull in ``websockets``/``cryptography`` or create an import cycle. All failures are
swallowed: recording an SB event must never break the core spawn flow.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import sb_events
from .atomic_files import (
    append_text_locked,
    atomic_create_json,
    atomic_write_json,
)
from .paths import multiverse_dir

#: Drop pending records older than this if never consumed (device seeded but connector
#: never ran). Spawn events are rare, so this is just a safety bound.
_MAX_PENDING_AGE_S = 7 * 24 * 3600


# ---- paths (lightweight; mirror sb_connector's layout without importing it) ----------


def _sb_dir() -> Path:
    return multiverse_dir() / "secondbrain"


def pending_dir() -> Path:
    return _sb_dir() / "pending"


def _sidecar_path() -> Path:
    return _sb_dir() / "device.key.json"


def _state_path() -> Path:
    return _sb_dir() / "state.json"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve(cwd: str) -> str:
    return str(Path(cwd).expanduser().resolve())


def _label(cwd: str) -> str:
    return Path(cwd).name or cwd


# ---- the "connected before" gate -----------------------------------------------------


def _device_id() -> str | None:
    data = _read_json(_sidecar_path())
    return data.get("device_id") if data else None


def _seeded() -> set[str]:
    data = _read_json(_state_path()) or {}
    return set(data.get("seeded", []))


def _exposed() -> set[str] | None:
    """The set of ``cwd`` strings the user has opted-in for SB observation, or ``None``
    if the field is missing (legacy state.json → treat as "all granted are exposed").

    Callers combine with device_id + resolved cwd to compare against workspace_id.
    """
    data = _read_json(_state_path()) or {}
    if "exposed" not in data:
        return None
    return {str(x) for x in (data.get("exposed") or [])}


def _granted() -> set[str]:
    """Set of ``cwd`` strings from the identity sidecar's granted_workspaces."""
    data = _read_json(_sidecar_path()) or {}
    return {str(x) for x in (data.get("granted_workspaces") or [])}


def _recordable_workspace_id(cwd: str) -> str | None:
    """``workspace_id`` if this device is enrolled AND this workspace is currently
    exposed (user-opted-in for SB observation); otherwise ``None`` — meaning: do
    not record.

    "Exposed" was added 2026-07-20 for per-workspace connect/disconnect. Backward
    compat: if state.json has no ``exposed`` field yet (fresh state written by
    pre-refactor code), fall back to the sidecar's ``granted_workspaces`` — every
    granted workspace is implicitly exposed, matching pre-refactor behavior.
    """
    device_id = _device_id()
    if not device_id:
        return None
    cwd_r = _resolve(cwd)
    exposed = _exposed()
    if exposed is None:
        exposed = _granted()  # legacy default
    if cwd_r not in exposed:
        return None
    return f"{device_id}:{cwd_r}"


def _hook_log_path() -> Path:
    """Diagnostic log for hook invocations. Separate from ``daemon.log`` because hooks
    are short-lived subprocesses that CAN'T share the daemon's Python logger — this
    file is what a user greps to answer "did hook X fire for agent Y and what
    happened?" when SB reports a missing event.
    """
    return _sb_dir() / "hooks.log"


def log_hook_outcome(
    script: str,
    outcome: str,
    *,
    cwd: str = "",
    agent: str = "",
    detail: str = "",
) -> None:
    """Append a single line to ``hooks.log`` describing what a hook decided.

    Format: ``<iso-ts> <script> agent=<a> cwd=<c> outcome=<o> detail=<d>``. Callers
    should pass a compact ``outcome`` slug (``recorded`` / ``no-multiverse-env`` /
    ``empty-prompt`` / ``filter-task-notification`` / ``filter-local-command-caveat``
    / ``gate-drop-workspace-not-seeded`` / ``no-device-identity`` / ``exception``)
    and any free-form context in ``detail``.

    Silent on failure — a hook diagnostic that CRASHED the hook would be worse
    than the drop it was trying to explain.

    Also silent when the device isn't enrolled at all: this log exists to explain why
    an EXPECTED event didn't fire, and with no SB set up here none was expected. An
    enrolled-but-unexposed workspace still logs, which is the case someone actually
    investigates (it is how a workspace whose hooks weren't firing was diagnosed).
    """
    try:
        if not _device_id():
            return
        line = (
            f"{_now_iso()} {script} agent={agent or '<unset>'} "
            f"cwd={cwd or '<unset>'} outcome={outcome}"
        )
        if detail:
            line += f" detail={detail}"
        line += "\n"
        p = _hook_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        append_text_locked(p, line)
    except Exception:  # noqa: BLE001
        pass


# ---- writing pending records ---------------------------------------------------------


def _safe(event_id: str) -> str:
    return event_id.replace(":", "-").replace("/", "_")


def _write_record(
    event_id: str, kind: str, workspace_id: str, cwd: str, agent: str, payload: dict
) -> None:
    pd = pending_dir()
    pd.mkdir(parents=True, exist_ok=True)
    path = pd / f"{_safe(event_id)}.json"
    record = {
        "event_id": event_id,
        "kind": kind,
        "workspace_id": workspace_id,
        "workspace_label": _label(cwd),
        "agent": agent,
        "payload": payload,
        "ts": _now_iso(),
    }
    if atomic_create_json(path, record):
        _prune_old()


def _prune_old() -> None:
    pd = pending_dir()
    if not pd.is_dir():
        return
    cutoff = time.time() - _MAX_PENDING_AGE_S
    for p in pd.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def record_spawn_request(req: dict) -> None:
    """Record an ``agent.spawn_request`` event at creation time (``write_spawn_request``)."""
    try:
        wsid = _recordable_workspace_id(req.get("cwd", ""))
        if wsid is None:
            return
        agent, payload = sb_events.spawn_request_event_fields(req)
        _write_record(
            f"spawnreq:{req.get('id', '')}", "agent.spawn_request",
            wsid, req.get("cwd", ""), agent, payload,
        )
    except Exception:  # noqa: BLE001 - never break the spawn flow
        pass


def response_signals_dir() -> Path:
    """Queue of lightweight turn-end signals the connector turns into agent.response
    events (generating the transcript off the agent's critical path)."""
    return _sb_dir() / "response_signals"


def _turn_signal_path(
    directory: Path, cwd: str, agent_name: str, turn_id: str | None
) -> Path:
    if not turn_id:
        return directory / f"{uuid.uuid4().hex}.json"
    identity = f"{_resolve(cwd)}\x00{agent_name}\x00{turn_id}"
    return directory / f"{hashlib.sha256(identity.encode()).hexdigest()}.json"


def record_response_signal(
    cwd: str, agent_name: str, *, turn_id: str | None = None
) -> None:
    """Record a fast turn-end signal for the connector to expand into an ``agent.response``
    event (the agent finished a turn normally → idle). Called from the Stop hook — it must
    stay CHEAP (no transcript parsing here; the connector does that). Gated on the workspace
    having connected to SB before (seeded)."""
    try:
        wsid = _recordable_workspace_id(cwd)
        if wsid is None:
            # Log why so the hooks.log distinguishes "no device identity" (not enrolled)
            # from "workspace not seeded yet" (enrolled but never connected). Captures
            # THIS function's name (the record_*_signal we're in) as the script tag —
            # currentframe() is our own frame; the caller's frame is one up but that's
            # not what we want to display.
            reason = "no-device-identity" if not _device_id() else "gate-drop-workspace-not-exposed"
            import inspect
            frm = inspect.currentframe()
            fn = frm.f_code.co_name if frm else "record_*"
            # agent_name is the standard positional arg of every record_*_signal
            agent = str(frm.f_locals.get("agent_name") or "") if frm else ""
            log_hook_outcome(f"sb_notify.{fn}", reason, cwd=cwd, agent=agent)
            return
        d = response_signals_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "cwd": _resolve(cwd),
            "workspace_id": wsid,
            "workspace_label": _label(cwd),
            "agent": agent_name,
            "ts": _now_iso(),
        }
        if turn_id:
            rec["turn_id"] = turn_id
        path = _turn_signal_path(d, cwd, agent_name, turn_id)
        if turn_id:
            atomic_create_json(path, rec)
        else:
            atomic_write_json(path, rec)
    except Exception:  # noqa: BLE001 - never break the hook
        pass


def list_response_signals() -> list[dict]:
    """Pending turn-end signals, oldest first (by ``ts``); each has ``_path`` for delete."""
    d = response_signals_dir()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for p in d.glob("*.json"):
        if p.name.endswith(".json.tmp"):
            continue
        rec = _read_json(p)
        if rec:
            rec["_path"] = str(p)
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", ""))
    return out


def delete_response_signal(record: dict) -> None:
    try:
        Path(record["_path"]).unlink()
    except (OSError, KeyError):
        pass


def user_prompt_signals_dir() -> Path:
    """Queue of turn-START signals the connector expands into ``user.prompt`` events
    (the connector attaches the partial transcript off the critical path)."""
    return _sb_dir() / "user_prompt_signals"


def record_user_prompt_signal(
    cwd: str,
    agent_name: str,
    text: str,
    *,
    turn_id: str | None = None,
) -> None:
    """Record a fast turn-start signal (from the UserPromptSubmit hook) — CHEAP, no
    transcript parsing on the agent's critical path. Gated on the workspace being seeded.

    ``text`` is the user's prompt content verbatim (from the hook payload's ``prompt``
    field). The connector fills in ``overview_since_last`` + ``full`` at consume time."""
    try:
        wsid = _recordable_workspace_id(cwd)
        if wsid is None:
            # Log why so the hooks.log distinguishes "no device identity" (not enrolled)
            # from "workspace not seeded yet" (enrolled but never connected). Captures
            # THIS function's name (the record_*_signal we're in) as the script tag —
            # currentframe() is our own frame; the caller's frame is one up but that's
            # not what we want to display.
            reason = "no-device-identity" if not _device_id() else "gate-drop-workspace-not-exposed"
            import inspect
            frm = inspect.currentframe()
            fn = frm.f_code.co_name if frm else "record_*"
            # agent_name is the standard positional arg of every record_*_signal
            agent = str(frm.f_locals.get("agent_name") or "") if frm else ""
            log_hook_outcome(f"sb_notify.{fn}", reason, cwd=cwd, agent=agent)
            return
        d = user_prompt_signals_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "cwd": _resolve(cwd),
            "workspace_id": wsid,
            "workspace_label": _label(cwd),
            "agent": agent_name,
            "text": text,
            "ts": _now_iso(),
        }
        if turn_id:
            rec["turn_id"] = turn_id
        path = _turn_signal_path(d, cwd, agent_name, turn_id)
        if turn_id:
            atomic_create_json(path, rec)
        else:
            atomic_write_json(path, rec)
    except Exception:  # noqa: BLE001 - never break the hook
        pass


def list_user_prompt_signals() -> list[dict]:
    return _list_and_stamp(user_prompt_signals_dir())


def delete_user_prompt_signal(record: dict) -> None:
    try:
        Path(record["_path"]).unlink()
    except (OSError, KeyError):
        pass


# ---- ask-tool question/answered signals -----------------------------------------------
# Firsthand production points: PreToolUse[AskUserQuestion] → question_signal; the pair
# closes at PostToolUse[AskUserQuestion] → answered_signal. Filename = tool_use_id (the
# ``question_id``) so a re-fire idempotently overwrites the same signal.

def _list_and_stamp(d: Path) -> list[dict]:
    if not d.is_dir():
        return []
    out: list[dict] = []
    for p in d.glob("*.json"):
        if p.name.endswith(".json.tmp"):
            continue
        rec = _read_json(p)
        if rec:
            rec["_path"] = str(p)
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", ""))
    return out


def question_signals_dir() -> Path:
    """Queue of ask-tool question signals the connector expands into agent.question events
    (attaching the partial transcript = the "why" context)."""
    return _sb_dir() / "question_signals"


def answered_signals_dir() -> Path:
    """Queue of ask-tool answered signals the connector expands into user.answered events
    (the close of the question pair; shares question_id with the earlier agent.question)."""
    return _sb_dir() / "answered_signals"


def record_question_signal(
    cwd: str, agent_name: str, question_id: str, questions: list
) -> None:
    """Record a fast ask-tool question signal (from PreToolUse[AskUserQuestion]). CHEAP —
    no transcript parsing here; the connector generates it off the agent's critical path.
    Gated on the workspace being seeded."""
    try:
        wsid = _recordable_workspace_id(cwd)
        if wsid is None or not question_id:
            return
        d = question_signals_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "cwd": _resolve(cwd),
            "workspace_id": wsid,
            "workspace_label": _label(cwd),
            "agent": agent_name,
            "question_id": question_id,
            "questions": questions,
            "ts": _now_iso(),
        }
        path = d / f"{_safe(question_id)}.json"
        atomic_write_json(path, rec)
    except Exception:  # noqa: BLE001 - never break the hook
        pass


def list_question_signals() -> list[dict]:
    return _list_and_stamp(question_signals_dir())


def delete_question_signal(record: dict) -> None:
    try:
        Path(record["_path"]).unlink()
    except (OSError, KeyError):
        pass


def record_answered_signal(
    cwd: str,
    agent_name: str,
    question_id: str,
    answers: dict | list,
    questions: list | None = None,
    annotations: dict | None = None,
) -> None:
    """Record a fast ask-tool answered signal (from PostToolUse[AskUserQuestion]). Carries
    the shared ``question_id`` so SB pairs it to the earlier agent.question. Keeps the
    original ``questions`` inline for SB's context, and ``annotations`` (per-question
    notes/previews from the AskUserQuestion response) so any "Other"-typed text isn't
    lost if the tool routes it there. Gated on the workspace being seeded."""
    try:
        wsid = _recordable_workspace_id(cwd)
        if wsid is None or not question_id:
            return
        d = answered_signals_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "cwd": _resolve(cwd),
            "workspace_id": wsid,
            "workspace_label": _label(cwd),
            "agent": agent_name,
            "question_id": question_id,
            "answers": answers,
            "questions": questions,
            "annotations": annotations,
            "ts": _now_iso(),
        }
        path = d / f"{_safe(question_id)}.json"
        atomic_write_json(path, rec)
    except Exception:  # noqa: BLE001 - never break the hook
        pass


def list_answered_signals() -> list[dict]:
    return _list_and_stamp(answered_signals_dir())


def delete_answered_signal(record: dict) -> None:
    try:
        Path(record["_path"]).unlink()
    except (OSError, KeyError):
        pass


# ---- permission pair signals ----------------------------------------------------------
# Firsthand production points, spread across runtime hooks. Neither Claude's
# Notification nor Codex's PermissionRequest exposes tool_use_id, so both correlate
# against the earlier PreToolUse record:
#   PreToolUse[.*]          → pending_tools/<tool_use_id>.json  (candidate)
#   runtime permission hook → request candidate signal
#   confirmed user prompt    → pending_tools/<tool_use_id>.perm  (marker)
#   PostToolUse[.*]         → if .perm exists → decision signal; always cleanup candidate + marker
# Claude uses its latest session-local candidate. Codex adds native session/turn/tool
# identity and refuses an ambiguous parallel match rather than guessing a wire handle.
# Codex's hook runs before its automatic reviewer, so the connector confirms the live
# approval overlay before creating the marker or relaying the request to SecondBrain.

def pending_tools_dir() -> Path:
    return _sb_dir() / "pending_tools"


def permission_request_signals_dir() -> Path:
    return _sb_dir() / "permission_request_signals"


def permission_decision_signals_dir() -> Path:
    return _sb_dir() / "permission_decision_signals"


def record_pending_tool(
    cwd: str,
    agent_name: str,
    tool_use_id: str,
    tool_name: str,
    tool_input,
    *,
    runtime: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """Stash a tool-use candidate (from PreToolUse[.*]) — cheap and unconditional. The
    permission hook may later flag it with a ``.perm`` marker; PostToolUse cleans up
    unconditionally. Not seeded-gated at record time: we don't know at PreToolUse whether
    this session's workspace is seeded (env vars can lie for background sessions), and
    the record is a bounded working set anyway. The gate applies at the SIGNAL writes.

    ENROLMENT is checked, though, which is a different question from seeded/exposed:
    every consumer of this cache is a SecondBrain path, so with no device identity on
    this machine nothing can ever read these records and writing them only accumulates
    residue after a teardown."""
    try:
        if not tool_use_id or not cwd or not agent_name:
            return
        if not _device_id():
            return
        d = pending_tools_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "cwd": _resolve(cwd),
            "agent": agent_name,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "ts": _now_iso(),
        }
        # Codex's PermissionRequest hook omits tool_use_id, unlike PreToolUse.
        # Keep its native session/turn identity on the candidate so the later hook can
        # recover the real call id without inventing a new SecondBrain wire handle.
        if runtime:
            rec["runtime"] = runtime
        if session_id:
            rec["session_id"] = session_id
        if turn_id:
            rec["turn_id"] = turn_id
        path = d / f"{_safe(tool_use_id)}.json"
        atomic_write_json(path, rec)
    except Exception:  # noqa: BLE001 - never break the hook
        pass


def latest_pending_tool_for_session(cwd: str, agent_name: str) -> dict | None:
    """The most-recent-by-ts candidate for this session — Notification's best-effort
    correlation target. Returns the candidate dict (with ``_path``) or ``None`` if we can't
    match one (the caller then skips the event rather than fire misleading data)."""
    d = pending_tools_dir()
    if not d.is_dir():
        return None
    cwd_r = _resolve(cwd)
    best: dict | None = None
    for p in d.glob("*.json"):
        if p.name.endswith(".json.tmp"):
            continue
        rec = _read_json(p)
        if not rec:
            continue
        if rec.get("cwd") != cwd_r or rec.get("agent") != agent_name:
            continue
        if best is None or rec.get("ts", "") > best.get("ts", ""):
            rec["_path"] = str(p)
            best = rec
    return best


def pending_codex_tool_for_permission(
    cwd: str,
    agent_name: str,
    session_id: str,
    turn_id: str,
    tool_name: str,
    tool_input,
) -> dict | None:
    """Resolve a Codex ``PermissionRequest`` to its earlier ``PreToolUse`` record.

    Codex intentionally omits ``tool_use_id`` from PermissionRequest but includes it in
    PreToolUse. Match within one native session and turn, preferring an exact tool-input
    match. PermissionRequest may add a human-only ``description`` field that PreToolUse
    does not carry, so that field is ignored for correlation only; the full permission
    input still rides in the emitted event.

    If multiple calls remain indistinguishable, return ``None``. Guessing would attach
    SecondBrain's answer handle to the wrong live approval, which is worse than omitting
    one notification and leaving the user to answer it locally.
    """
    d = pending_tools_dir()
    if not d.is_dir():
        return None
    cwd_r = _resolve(cwd)
    candidates: list[dict] = []
    for path in d.glob("*.json"):
        if path.name.endswith(".json.tmp"):
            continue
        rec = _read_json(path)
        if not rec:
            continue
        if (
            rec.get("cwd") != cwd_r
            or rec.get("agent") != agent_name
            or rec.get("runtime") != "codex"
            or rec.get("session_id") != session_id
            or rec.get("turn_id") != turn_id
            or rec.get("tool_name") != tool_name
        ):
            continue
        rec["_path"] = str(path)
        candidates.append(rec)

    request_input = tool_input
    if isinstance(request_input, dict) and "description" in request_input:
        request_input = {k: v for k, v in request_input.items() if k != "description"}
    exact = [rec for rec in candidates if rec.get("tool_input") == request_input]
    if len(exact) == 1:
        return exact[0]
    if not exact and len(candidates) == 1:
        # Managed-network approval can replace the original Bash input with its
        # synthesized approval command. Session + turn + canonical tool still make a
        # sole candidate unambiguous.
        return candidates[0]
    return None


def mark_permission_requested(tool_use_id: str) -> None:
    """Atomically create the ``<id>.perm`` marker (O_EXCL) so PostToolUse can tell whether
    a permission prompt happened for this tool_use_id — the pending signal itself may
    already be consumed by the connector by then."""
    try:
        d = pending_tools_dir()
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(d / f"{_safe(tool_use_id)}.perm"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        os.close(fd)
    except FileExistsError:
        pass
    except Exception:  # noqa: BLE001
        pass


def had_permission_prompt(tool_use_id: str) -> bool:
    return (pending_tools_dir() / f"{_safe(tool_use_id)}.perm").exists()


def pending_tool_data(tool_use_id: str) -> dict | None:
    """Return the PreToolUse-recorded candidate JSON for this ``tool_use_id`` if it's
    still in flight (present between the PreToolUse and PostToolUse hook fires), else
    ``None``. Used by ``answer_question`` to verify the target AskUserQuestion is still
    pending AND to read its ``tool_input.questions`` for shape validation."""
    path = pending_tools_dir() / f"{_safe(tool_use_id)}.json"
    if not path.is_file():
        return None
    return _read_json(path)


def find_pending_askuserquestion(cwd: str, agent_name: str) -> dict | None:
    """Locate the currently-pending AskUserQuestion for ``(cwd, agent_name)`` by scanning
    ``pending_tools/`` for a stashed AskUserQuestion candidate matching this agent.

    The stashed record is written by ``sb-emit-question.py`` (PreToolUse[AskUserQuestion])
    and removed by ``sb-emit-answered.py`` (PostToolUse[AskUserQuestion]) — so its
    presence is a reliable signal that the modal is currently blocking the agent's turn.
    An agent has at most one pending AskUserQuestion at a time, so a single match
    uniquely identifies it.

    Used by ``answer_question`` when SB doesn't have (or doesn't send) a ``question_id``.

    Returns a dict shaped like :func:`pending_tool_data` (``tool_use_id`` /
    ``tool_name`` / ``tool_input`` / ``cwd`` / ``agent``) or ``None`` if none pending.
    """
    d = pending_tools_dir()
    if not d.is_dir():
        return None
    cwd_r = _resolve(cwd)
    for p in d.glob("*.json"):
        if p.name.endswith(".json.tmp"):
            continue
        rec = _read_json(p)
        if not rec:
            continue
        if (
            rec.get("cwd") == cwd_r
            and rec.get("agent") == agent_name
            and rec.get("tool_name") == "AskUserQuestion"
        ):
            return rec
    return None


def cleanup_pending_tool(tool_use_id: str) -> None:
    """PostToolUse always calls this — whether or not a permission prompt happened."""
    d = pending_tools_dir()
    for suffix in (".json", ".perm"):
        try:
            (d / f"{_safe(tool_use_id)}{suffix}").unlink()
        except (OSError, FileNotFoundError):
            pass


def delete_permission_request_signal_for_tool(tool_use_id: str) -> None:
    """Delete an unrelayed permission-request candidate by tool id.

    Codex's ``PermissionRequest`` hook runs before automatic review. If the tool reaches
    ``PostToolUse`` without the connector having confirmed a live user-facing prompt,
    the reviewer approved it automatically and no SecondBrain permission pair should
    be emitted.
    """
    try:
        (permission_request_signals_dir() / f"{_safe(tool_use_id)}.json").unlink()
    except (OSError, FileNotFoundError):
        pass


def discard_unconfirmed_codex_permissions(
    cwd: str,
    agent_name: str,
    *,
    turn_id: str | None = None,
) -> int:
    """Discard Codex approval candidates that never became a user-facing prompt.

    Automatic-review denial does not reach ``PostToolUse``. At ``Stop``, any matching
    Codex pending tool without a ``.perm`` confirmation marker was therefore resolved
    without SecondBrain or the local user answering it. Remove both the candidate and
    its unrelayed request signal without manufacturing a permission decision.
    """
    discarded = 0
    try:
        d = pending_tools_dir()
        if not d.is_dir():
            return 0
        cwd_r = _resolve(cwd)
        for path in list(d.glob("*.json")):
            if path.name.endswith(".json.tmp"):
                continue
            record = _read_json(path) or {}
            if (
                record.get("runtime") != "codex"
                or record.get("cwd") != cwd_r
                or record.get("agent") != agent_name
                or (turn_id and record.get("turn_id") != turn_id)
            ):
                continue
            tool_use_id = str(record.get("tool_use_id") or "")
            if not tool_use_id or had_permission_prompt(tool_use_id):
                continue
            delete_permission_request_signal_for_tool(tool_use_id)
            cleanup_pending_tool(tool_use_id)
            discarded += 1
    except Exception:  # noqa: BLE001 - a Stop cleanup must never break the hook
        return discarded
    return discarded


def record_permission_request_signal(
    cwd: str,
    agent_name: str,
    tool_use_id: str,
    tool_name: str,
    tool_input,
    *,
    codex_turn_id: str | None = None,
    await_user_confirmation: bool = False,
) -> None:
    """Fast signal (from a runtime permission hook) → connector expands to
    ``agent.permission_request`` (attaching the partial transcript = "why" context, off the
    agent's critical path). Gated on the workspace being seeded. Filename = tool_use_id
    (idempotent — a Notification re-fire overwrites the same signal)."""
    try:
        wsid = _recordable_workspace_id(cwd)
        if wsid is None or not tool_use_id:
            return
        d = permission_request_signals_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "cwd": _resolve(cwd),
            "workspace_id": wsid,
            "workspace_label": _label(cwd),
            "agent": agent_name,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "ts": _now_iso(),
        }
        if codex_turn_id:
            # Internal Codex correlation only. The connector uses this to wait for the
            # matching rollout row before composing the unchanged SecondBrain payload.
            rec["codex_turn_id"] = codex_turn_id
        if await_user_confirmation:
            # Internal-only lifecycle flag. It is consumed before payload composition
            # and never changes SecondBrain's agent.permission_request schema.
            rec["await_user_confirmation"] = True
        path = d / f"{_safe(tool_use_id)}.json"
        atomic_write_json(path, rec)
    except Exception:  # noqa: BLE001 - never break the hook
        pass


def list_permission_request_signals() -> list[dict]:
    return _list_and_stamp(permission_request_signals_dir())


def delete_permission_request_signal(record: dict) -> None:
    try:
        Path(record["_path"]).unlink()
    except (OSError, KeyError):
        pass


def sweep_unresolved_permissions(cwd: str, agent_name: str) -> int:
    """Close out permission prompts that were denied. Called at Stop; returns how many.

    Claude Code fires no dedicated hook when a user denies a permission prompt, and
    Codex likewise does not reach PostToolUse after a deny. For Claude, three events
    look like they should close the pair but do not:

      * ``PostToolUse`` is documented as running after a *successful* tool, and a denied
        tool never runs.
      * ``PostToolUseFailure`` sits in the ``catch`` around tool execution, and the
        denial path returns before execution is reached.
      * ``PermissionDenied`` is gated on ``decisionReason.classifier == 'auto-mode'``,
        so it covers classifier denials only, not a human (or SB) answering the modal.

    So an allow emits ``user.permission_decision`` from PostToolUse and a deny emitted
    nothing at all: the pair never closed, and SB could not tell a denied prompt from
    one still waiting on a human. Stop is the first moment we can observe it.

    Detection is by absence: ``mark_permission_requested`` writes a ``.perm`` marker,
    and PostToolUse deletes it via :func:`cleanup_pending_tool` when the tool runs. A
    marker still present at end of turn therefore means the tool never ran.

    That covers denial and also interrupt (Esc at the prompt). We report both as
    ``deny`` deliberately: the two are indistinguishable from here, the observable
    outcome is identical -- the tool did not run -- and reporting the wrong *reason*
    is much cheaper than leaving the pair open forever, which is the status quo.
    """
    closed = 0
    try:
        d = pending_tools_dir()
        if not d.is_dir():
            return 0
        cwd_r = _resolve(cwd)
        for marker in list(d.glob("*.perm")):
            record = _read_json(d / f"{marker.stem}.json") or {}
            # Only this agent's prompts: one Stop must not close another's.
            if record.get("cwd") != cwd_r or record.get("agent") != agent_name:
                continue
            tool_use_id = record.get("tool_use_id") or ""
            if not tool_use_id:
                continue
            record_permission_decision_signal(
                cwd,
                agent_name,
                tool_use_id,
                "deny",
                record.get("tool_name") or "",
                record.get("tool_input"),
                None,  # the tool never ran, so there is no response to carry
            )
            cleanup_pending_tool(tool_use_id)
            closed += 1
    except Exception:  # noqa: BLE001 - a sweep must never break the Stop hook
        return closed
    return closed


def record_permission_decision_signal(
    cwd: str,
    agent_name: str,
    tool_use_id: str,
    decision: str,
    tool_name: str,
    tool_input,
    tool_response,
) -> None:
    """Fast signal (from PostToolUse) → connector expands to ``user.permission_decision``.
    ``decision`` ∈ {``allow``, ``deny``}. No transcript (agent was blocked)."""
    try:
        wsid = _recordable_workspace_id(cwd)
        if wsid is None or not tool_use_id:
            return
        d = permission_decision_signals_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "cwd": _resolve(cwd),
            "workspace_id": wsid,
            "workspace_label": _label(cwd),
            "agent": agent_name,
            "tool_use_id": tool_use_id,
            "decision": decision,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": tool_response,
            "ts": _now_iso(),
        }
        path = d / f"{_safe(tool_use_id)}.json"
        atomic_write_json(path, rec)
    except Exception:  # noqa: BLE001
        pass


def list_permission_decision_signals() -> list[dict]:
    return _list_and_stamp(permission_decision_signals_dir())


def delete_permission_decision_signal(record: dict) -> None:
    try:
        Path(record["_path"]).unlink()
    except (OSError, KeyError):
        pass


def record_agent_created(
    cwd: str, name: str, initial_prompt: str | None = None, session_id: str = "",
) -> None:
    """Record an ``agent.created`` event at session-creation time (called from
    ``sessions.create_session`` when ``resumed_from is None`` — i.e. a fresh new agent,
    not a resume). Gated on the workspace being seeded. ``initial_prompt`` is present
    only when the agent was spawn-request-created (the requesting agent's instruction);
    absent for user-created agents (where the name is the only input).

    Event ID is ``created:<name>:<session_id>``. The name alone is not enough: SB
    dedups on ``event_id`` and NOTHING ELSE (``ingest.py`` keeps a set of bare id
    strings), so if an agent name were re-used after the previous one ended, the
    second creation would collide with the first and be dropped silently — SB would
    simply never learn the agent exists. An earlier version of this comment claimed
    SB distinguished them by timestamp; it does not, and that mistake is why the id
    was too weak. The session id is unique per creation, so a re-used name still
    produces a fresh event while a retry of the SAME creation still dedups.

    DEPENDS ON: SB deduping by ``event_id`` alone. If it ever keys on more than that,
    this id can be relaxed back to the name; if it ever dedups on less, this breaks
    and needs revisiting."""
    try:
        wsid = _recordable_workspace_id(cwd)
        if wsid is None or not name:
            return
        payload = sb_events.agent_created_event_fields(initial_prompt)
        event_id = f"created:{name}:{session_id}" if session_id else f"created:{name}"
        _write_record(
            event_id, "agent.created",
            wsid, cwd, name, payload,
        )
    except Exception:  # noqa: BLE001 - never break session creation
        pass


def record_spawn_decision(req: dict, status: str, name: str | None) -> None:
    """Record a ``user.spawn_decision`` event at resolution time (``write_resolution``).

    ``req`` is the original request dict (read from its file by the caller, which still
    exists at resolution time). A ``timeout``/non-decision status records nothing.
    """
    try:
        wsid = _recordable_workspace_id(req.get("cwd", ""))
        if wsid is None:
            return
        composed = sb_events.spawn_decision_event_fields(req, status, name)
        if composed is None:
            return  # timeout / not a user decision
        agent, payload = composed
        _write_record(
            f"spawndec:{req.get('id', '')}", "user.spawn_decision",
            wsid, req.get("cwd", ""), agent, payload,
        )
    except Exception:  # noqa: BLE001
        pass


# ---- reading / clearing (consumer side: the connector) -------------------------------


def list_pending() -> list[dict]:
    """All pending event records, oldest first (by ``ts``). Each dict has an added
    ``_path`` key for :func:`delete_pending`."""
    pd = pending_dir()
    if not pd.is_dir():
        return []
    out: list[dict] = []
    for p in pd.glob("*.json"):
        if p.name.endswith(".json.tmp"):
            continue
        rec = _read_json(p)
        if rec:
            rec["_path"] = str(p)
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", ""))
    return out


def delete_pending(record: dict) -> None:
    try:
        Path(record["_path"]).unlink()
    except (OSError, KeyError):
        pass
