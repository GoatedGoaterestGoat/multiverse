"""Multiverse MCP server.

Hosts the agent-facing tools for the multiverse plugin:
  - list_agents                   — discover other agents in workspace
  - set_description               — set your own concise responsibility label
  - message_agent                 — send a persistent message to another
                                    agent through its durable inbox;
                                    recipient may use tools and reply by
                                    calling message_agent back
  - ask_agent                     — quick ephemeral question to another
                                    agent (forked side-question, no tools,
                                    blocking with timeout)
  - spawn_agent                   — start a brand-new subagent in the
                                    workspace with the calling agent's
                                    instruction as its first user message
                                    (user confirms+names via a monitor
                                    modal)
  - check_inbox / monitor_inbox   — read and wait on the calling agent's
                                    inbox
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import time
from functools import wraps
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

# Add parent to path for imports when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sessions import (
    RUNTIME_CLAUDE,
    RUNTIME_CODEX,
    RUNTIME_PI,
    all_session_names_in_cwd,
    create_session,
    find_agent_by_name,
    foreign_host_still_reported_live,
    is_foreign_host,
    kill_session,
    list_named_agents_in_cwd,
    normalize_runtime,
    retired_agent_message,
    retiring_agent_message,
    write_status_path,
)
from src.inbox import (
    claim_pending_messages,
    clear_wake_sent,
    commit_claimed_messages,
    mark_wake_sent,
    pending_inbox_count,
    render_messages_as_tool_result,
    wake_delivery_lock,
    wake_is_outstanding,
    write_message_to_inbox,
)
from src.interagent_log import (
    log_inbox_delivered,
    log_message_sent,
    log_notification_routed,
)
from src.compact_state import is_locked
from src.paths import encode_cwd
from src import wake_relay
from src import groups
from src import codex_control
from src.ask_agents import AskTarget, ask_one
from src.names import pick_random_name
from src.agent_descriptions import write_description
from src.reminders import append_reminders
from src.spawn_requests import (
    cleanup_request,
    new_request_id,
    read_resolution,
    write_outcome,
    write_spawn_request,
)
from src.sb_actions import (
    capture_pane,
    claude_input_box,
    capture_focused_codex_composer_draft,
    clear_composer_text_via_tmux,
    codex_composer_text,
    parse_input_box_text,
    paste_text_via_tmux,
    send_prompt_via_tmux,
)

_CALLER_IDENTITY: ContextVar[tuple[str | None, str | None] | None] = ContextVar(
    "multiverse_caller_identity", default=None,
)


class InteragentMCP(FastMCP):
    async def call_tool(self, name, arguments):
        # Resolve once, before long-running calls or concurrent agents can
        # move the Claude transcript heuristic. ContextVar also isolates
        # simultaneous calls, including synchronous functions run in threads.
        caller = _resolve_self_identity()
        token = _CALLER_IDENTITY.set(caller)
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            # Includes argument-validation errors, which occur before the
            # registered function's normal result wrapper can run.
            raise ToolError(append_reminders(str(exc), *caller)) from exc
        finally:
            _CALLER_IDENTITY.reset(token)


mcp = InteragentMCP("multiverse")


def interagent_tool(fn):
    """Register the MCP result wrapper while preserving internal Python calls.

    SecondBrain also calls these functions directly; its responses must not
    receive agent reminders. Pi's mvctl transport applies the same helper.
    wraps preserves the original signature/docstring and thus the MCP schema.
    Capture identity before a potentially long async wait, but check reminder
    conditions afterwards so a successful set_description stops the reminder.
    """
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            name, cwd = _resolve_self_identity()
            result = await fn(*args, **kwargs)
            return append_reminders(result, name, cwd)
    else:
        @wraps(fn)
        def wrapped(*args, **kwargs):
            name, cwd = _resolve_self_identity()
            result = fn(*args, **kwargs)
            return append_reminders(result, name, cwd)
    mcp.tool()(wrapped)
    return fn

_DEFAULT_ASK_TIMEOUT = 60
_DEFAULT_MONITOR_INBOX_TIMEOUT = 300
_MONITOR_INBOX_POLL_INTERVAL = 1.0
# Poll interval while a spawn request waits (indefinitely) for the user to
# accept/reject in the monitor.
_SPAWN_POLL_INTERVAL = 0.25
# Emit a progress notification every N seconds while spawn_agent waits for
# the user. Keeps the tool call visibly alive on the client (any
# progress-reset timeout gets pushed forward) and gives UX feedback.
_SPAWN_PROGRESS_INTERVAL = 10.0

# Status written to this session's status.json while monitor_inbox is parked,
# so the dashboard and list_agents show the agent as waiting-for-messages
# rather than busy. Mirrors set-status.sh's JSON shape.
_LISTENING_STATUS = "listening"

#: Minimum mtime gap between the freshest transcript and the runner-up before
#: ``_resolve_self_identity`` will trust the freshest one as its own.
_IDENTITY_RACE_MARGIN_S = 0.25
# How recently a transcript must have been appended to for its session to
# count as "currently writing" for identity purposes.
_IDENTITY_WINDOW_S = 10.0


def _env_session_wrote_recently(
    env_name: str, env_cwd: str, projects: Path, now: float
) -> bool:
    """True if the session the env vars name has ITSELF appended to its
    transcript inside the identity window.

    This is the plausibility test that gates transcript discovery. The
    bg-pty-host failure is precisely "the env-named session is not the one
    writing" — a frozen daemon's owner is elsewhere and silent. So if that
    session is demonstrably writing right now, the env vars are fine and
    must be left alone.
    """
    mv_root = Path(
        os.environ.get("MULTIVERSE_ROOT") or os.path.expanduser("~/.multiverse")
    )
    sid_env = os.environ.get("MULTIVERSE_SESSION_ID")
    try:
        sess_dirs = list((mv_root / "sessions").iterdir())
    except OSError:
        return False
    for sess_dir in sess_dirs:
        if not sess_dir.is_dir():
            continue
        # MULTIVERSE_SESSION_ID pins the dir directly when present; fall back
        # to matching name+cwd for wrappers that predate it.
        if sid_env:
            if sess_dir.name != sid_env:
                continue
        else:
            try:
                meta = json.loads((sess_dir / "meta.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("name") != env_name or meta.get("cwd") != env_cwd:
                continue
        try:
            uuid = (sess_dir / "claude_uuid").read_text().strip()
        except OSError:
            continue
        if not uuid:
            continue
        try:
            mt = (projects / encode_cwd(env_cwd) / f"{uuid}.jsonl").stat().st_mtime
        except OSError:
            continue
        if now - mt <= _IDENTITY_WINDOW_S:
            return True
    return False


def _resolve_self_identity() -> tuple[str | None, str | None]:
    """Return ``(name, cwd)`` of the multiverse session this tool call
    is actually being made from — bypassing MULTIVERSE_SESSION_NAME /
    MULTIVERSE_SESSION_CWD env vars, which Claude Code's bg-pty-host
    silently pollutes.

    Background. When a claude session runs in ``sessionKind: "bg"``
    (which happens after Claude Code's autocompact fires — the summarized
    session continues as a background agent), all its tool calls are
    dispatched through a shared bg-pty-host daemon. That daemon holds
    frozen env vars from whichever session first spawned it. So reading
    MULTIVERSE_SESSION_NAME here returns THE DAEMON OWNER's name, not
    the caller's — every outgoing message_agent then stamps ``from:``
    with the wrong identity. Symmetric problem for MULTIVERSE_SESSION_CWD.

    The reliable ground truth is the JSONL file claude is currently
    appending to: at the moment this tool call fires, claude has JUST
    written a tool_use row into ``~/.claude/projects/<slug>/<uuid>.jsonl``.
    That row carries ``sessionId``, which we cross-reference against
    ``~/.multiverse/sessions/*/claude_uuid`` to find the multiverse
    session dir, whose ``meta.json`` has the correct ``name`` and ``cwd``.

    Falls back to env vars if any step of the lookup fails — the env
    values are still correct for the vast majority of non-bg sessions.
    """
    caller = _CALLER_IDENTITY.get()
    if caller is not None:
        return caller
    env_name = os.environ.get("MULTIVERSE_SESSION_NAME")
    env_cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    # The shared bg-pty-host issue is specific to Claude. Every other managed
    # runtime starts its own MCP server with wrapper-owned identity variables,
    # so Claude transcript discovery could only misidentify a non-Claude
    # caller — it would hand back whichever Claude agent happened to write a
    # jsonl row in the last 10s. Unset means Claude, matching the default used
    # everywhere else in this module.
    if os.environ.get("MULTIVERSE_RUNTIME", RUNTIME_CLAUDE) != RUNTIME_CLAUDE:
        return env_name, env_cwd
    # Nothing to repair unless we are inside a managed session. The bg-pty-host
    # failure is a session whose identity vars are present but FROZEN from
    # another session — never one with no identity at all. An unwrapped MCP
    # server has no claim on whichever agent happens to be writing right now,
    # and inventing one for it is the same wrong-identity hazard this guard
    # exists to avoid.
    if not env_name or not env_cwd:
        return env_name, env_cwd
    try:
        cfg = Path(
            os.environ.get("CLAUDE_CONFIG_DIR")
            or os.path.expanduser("~/.claude")
        )
        projects = cfg / "projects"
        if not projects.is_dir():
            return env_name, env_cwd
        now = time.time()
        # Trust the env vars whenever the session they name is itself
        # writing. Discovery below picks the globally freshest transcript
        # and ASSUMES it belongs to this caller, verifying nothing about the
        # winner — so any other agent working harder than the caller wins it.
        # Observed: toolsmith (SecondBrain) resolved to Aris (Multiverse)
        # whenever Aris was `working`, and correctly to itself the moment
        # Aris went `idle`. Outgoing messages were then stamped `From agent:
        # Aris`, and messaging Aris failed with "cannot message yourself".
        #
        # The 0.25s race margin cannot catch that: it only fires when the top
        # two writes are near-simultaneous, whereas a continuously-working
        # agent beats an idle-but-correct caller by far more than 0.25s.
        #
        # So invert the trust. Discovery is a repair for one specific fault —
        # a frozen bg-pty-host daemon, whose signature is that the env-named
        # session is NOT writing — and it only runs once we have evidence of
        # that fault.
        if _env_session_wrote_recently(env_name, env_cwd, projects, now):
            return env_name, env_cwd
        # Freshest JSONL across all project dirs. tool_use is written
        # to disk before dispatch to the MCP server, so at call time
        # OUR jsonl should have mtime within a second or two of now.
        recent: list[tuple[float, Path]] = []
        for pd in projects.iterdir():
            if not pd.is_dir():
                continue
            try:
                jsonls = list(pd.glob("*.jsonl"))
            except OSError:
                continue
            for j in jsonls:
                try:
                    mt = j.stat().st_mtime
                except OSError:
                    continue
                # Generous window: it comfortably includes the
                # write-then-dispatch latency and any small clock skew.
                if now - mt > _IDENTITY_WINDOW_S:
                    continue
                recent.append((mt, j))
        if not recent:
            return env_name, env_cwd
        recent.sort(key=lambda item: item[0], reverse=True)
        best = recent[0]
        # Refuse to guess when two agents raced. Our own tool_use row is
        # written immediately before this call dispatches, so the freshest
        # jsonl is normally ours by a clear margin — but in a multiverse
        # workspace several Claude agents are live at once, and if another
        # one wrote inside the same instant "freshest wins" is a coin flip
        # that hands this caller a DIFFERENT agent's name and cwd. Wrong
        # identity is far worse than no correction: it stamps outgoing
        # messages with someone else's name and drains their inbox. When
        # the top two are too close to separate, fall back to the env vars,
        # which are correct for every session except the bg-pty-host case
        # this lookup exists to repair.
        if len(recent) > 1 and best[0] - recent[1][0] < _IDENTITY_RACE_MARGIN_S:
            return env_name, env_cwd
        # Read the first row's sessionId (sessionId is stable per file,
        # so any row works; first is cheapest).
        session_id: str | None = None
        try:
            with best[1].open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = row.get("sessionId") or row.get("session_id")
                    if sid:
                        session_id = sid
                        break
        except OSError:
            return env_name, env_cwd
        if session_id is None:
            return env_name, env_cwd
        # Match against multiverse sessions.
        mv_root = Path(
            os.environ.get("MULTIVERSE_ROOT")
            or os.path.expanduser("~/.multiverse")
        )
        for sess_dir in (mv_root / "sessions").iterdir():
            if not sess_dir.is_dir():
                continue
            u_file = sess_dir / "claude_uuid"
            if not u_file.is_file():
                continue
            try:
                if u_file.read_text().strip() != session_id:
                    continue
                meta = json.loads((sess_dir / "meta.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue
            return (
                meta.get("name") or env_name,
                meta.get("cwd") or env_cwd,
            )
        # sessionId not in multiverse (session not multiverse-managed).
        return env_name, env_cwd
    except Exception:  # noqa: BLE001 — env fallback must never crash tool calls
        return env_name, env_cwd


def _write_session_status(status: str) -> None:
    """Write a status value to this session's status.json (best-effort).

    The MCP server inherits MULTIVERSE_STATUS from the managed Claude or
    Codex process. No-op outside the wrapper (env var unset). Matches the
    ``{"status": ..., "ts": ...}`` shape lifecycle hooks write, so hook-driven
    and tool-driven status updates are interchangeable.
    """
    status_path = os.environ.get("MULTIVERSE_STATUS")
    if not status_path:
        return
    try:
        write_status_path(Path(status_path), status, int(time.time()))
    except OSError:
        pass


# Prompt typed into an idle/working agent's tmux pane to wake it for a
# push-notified message. Claude may also receive the message through its
# UserPromptSubmit inbox hook. Codex uses this prompt to call check_inbox;
# the message remains durable until that tool atomically claims it.
_WAKE_PROMPT = (
    "Please check your inbox — one or more new inter-agent messages have arrived. "
    "Use the multiverse check_inbox tool to read and clear them, then respond or act "
    "as appropriate."
)
_CODEX_WAKE_COMPOSER_POLL_INTERVAL_S = 0.025
_CODEX_WAKE_SUBMIT_ACK_TIMEOUT_S = 0.5
_CODEX_WAKE_SUBMIT_ATTEMPTS = 3
_CODEX_WAKE_DRAFT_TIMEOUT_S = 3.0


def _codex_wake_prompt_matches(rendered: str) -> bool:
    """Whether a rendered composer contains exactly our wake prompt.

    Ratatui renders a long single-line Codex draft over explicit rows rather
    than terminal soft-wrap rows.  Tmux therefore returns newlines at those
    visual boundaries even with ``capture-pane -J``.  The wake prompt itself
    has no logical newlines, so replacing only rendered row breaks with spaces
    reconstructs it without weakening validation for any other payload.
    """
    return rendered == _WAKE_PROMPT or " ".join(
        line.strip() for line in rendered.splitlines()
    ) == _WAKE_PROMPT


def _codex_wake_prompt_state(tmux_name: str) -> bool | None:
    """Whether Codex's visible composer contains exactly the wake prompt.

    Browser terminals can occasionally leak delayed terminal-identification replies
    into application input. Fail closed rather than submitting a composer that
    contains one of those replies, a pre-existing draft, or any other unexpected text.
    ``None`` means the pane could not be captured; the caller must not treat a transient
    capture failure as either validation or submission acknowledgement.
    """
    if codex_control.local_session(tmux_name) is None:
        return None
    ok, pane = capture_pane(
        tmux_name,
        join_wrapped=True,
        include_styles=True,
    )
    if not ok:
        return None
    rendered = codex_composer_text(pane)
    return rendered is not None and _codex_wake_prompt_matches(rendered)


def _codex_wake_prompt_ready(tmux_name: str) -> bool:
    return _codex_wake_prompt_state(tmux_name) is True


def _wait_for_codex_wake_prompt_to_clear(tmux_name: str) -> bool:
    """Whether Codex acknowledged submission by clearing the wake composer."""
    deadline = time.monotonic() + _CODEX_WAKE_SUBMIT_ACK_TIMEOUT_S
    while True:
        if codex_control.local_session(tmux_name) is None:
            return False
        ok, draft, _ = capture_focused_codex_composer_draft(tmux_name)
        if ok and not draft:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_CODEX_WAKE_COMPOSER_POLL_INTERVAL_S, remaining))


def _submit_codex_wake_prompt(tmux_name: str) -> bool:
    """Submit a validated Codex wake prompt, with acknowledgement.

    Codex assigns Enter to submit: it starts a turn while idle and steers the active
    turn at its next tool boundary while working. Tab is the distinct follow-up queue
    action and must not be used here because it waits for the current turn to finish.
    tmux accepting a key does not prove that the TUI handled it, so retry only while
    the exact validated prompt remains in the composer.
    """
    for _ in range(_CODEX_WAKE_SUBMIT_ATTEMPTS):
        # A user may type (or ownership may change) between retries. Never
        # submit a composer merely because our previous paste was validated.
        if not _codex_wake_prompt_ready(tmux_name):
            return False
        if codex_control.local_session(tmux_name) is None:
            return False
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_name, "Enter"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        if _wait_for_codex_wake_prompt_to_clear(tmux_name):
            return True
    return False


def _wait_for_codex_composer_text(tmux_name: str, expected: str) -> bool:
    """Wait for a focused Codex composer to contain exactly ``expected``."""
    deadline = time.monotonic() + _CODEX_WAKE_DRAFT_TIMEOUT_S
    while True:
        if codex_control.local_session(tmux_name) is None:
            return False
        ok, draft, _ = capture_focused_codex_composer_draft(tmux_name)
        if ok and (
            draft == expected
            or (expected == _WAKE_PROMPT and _codex_wake_prompt_matches(draft))
        ):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_CODEX_WAKE_COMPOSER_POLL_INTERVAL_S, remaining))


def _replace_codex_composer_text(
    tmux_name: str,
    current: str,
    replacement: str,
) -> bool:
    """Atomically-as-possible replace a known focused Codex draft."""
    if codex_control.local_session(tmux_name) is None:
        return False
    ok, actual, _ = capture_focused_codex_composer_draft(tmux_name)
    if not ok or actual != current:
        return False
    if current:
        if codex_control.local_session(tmux_name) is None:
            return False
        ok, _ = clear_composer_text_via_tmux(tmux_name, current)
        if not ok or not _wait_for_codex_composer_text(tmux_name, ""):
            return False
    if replacement:
        if codex_control.local_session(tmux_name) is None:
            return False
        ok, actual, _ = capture_focused_codex_composer_draft(tmux_name)
        if not ok or actual:
            return False
        if codex_control.local_session(tmux_name) is None:
            return False
        ok, _ = paste_text_via_tmux(tmux_name, replacement)
        if not ok or not _wait_for_codex_composer_text(tmux_name, replacement):
            return False
    return True


def _restore_codex_composer_text(tmux_name: str, saved: str) -> bool:
    """Best-effort rollback/restore after a Codex wake attempt."""
    deadline = time.monotonic() + _CODEX_WAKE_DRAFT_TIMEOUT_S
    while True:
        if codex_control.local_session(tmux_name) is None:
            return False
        ok, current, _ = capture_focused_codex_composer_draft(tmux_name)
        if ok:
            if current == saved:
                return True
            if current and not _codex_wake_prompt_matches(current):
                # This is newly typed user content, not ours to erase. The
                # displaced draft remains in its recovery file for a retry.
                return False
            if _replace_codex_composer_text(tmux_name, current, saved):
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_CODEX_WAKE_COMPOSER_POLL_INTERVAL_S, remaining))


def _wake_codex_via_tmux(tmux_name: str) -> bool:
    """Wake Codex without losing a detached session's partial draft."""
    session = codex_control.local_session(tmux_name)
    if session is None:
        return False
    try:
        pending = codex_control.pending_draft(session)
        if pending is not None:
            if not _restore_codex_composer_text(tmux_name, pending["text"]):
                return False
            if codex_control.local_session(tmux_name) is None:
                return False
            codex_control.clear_draft(session)
            # If the earlier wake was accepted, do not submit it a second time
            # just because restoration had to wait for the composer to clear.
            return pending["submitted"]
    except (OSError, ValueError):
        return False
    ok, saved, _ = capture_focused_codex_composer_draft(tmux_name)
    if not ok:
        return False
    try:
        if codex_control.local_session(tmux_name) is None:
            return False
        if saved and not codex_control.save_draft(session, saved):
            return False
        submitted = False
        if (not saved or _replace_codex_composer_text(tmux_name, saved, "")):
            if _replace_codex_composer_text(tmux_name, "", _WAKE_PROMPT):
                submitted = _submit_codex_wake_prompt(tmux_name)
                if submitted and saved and codex_control.local_session(tmux_name):
                    codex_control.mark_draft_submitted(session)
        restored = _restore_codex_composer_text(tmux_name, saved)
        if restored and saved and codex_control.local_session(tmux_name):
            codex_control.clear_draft(session)
        return submitted and restored
    except (OSError, ValueError, subprocess.SubprocessError):
        # Keep the durable draft if a terminal operation failed mid-sequence.
        return False


# How long to wait for Claude's TUI to render after a submit before deciding
# the wake prompt never landed. Polled, so a healthy submit confirms in ~one
# render cycle and only a genuine failure pays the full wait.
_WAKE_CONFIRM_TIMEOUT_S = 3.0
_WAKE_CONFIRM_POLL_S = 0.1


def _claude_wake_confirmed(tmux_name: str, expected_draft: str) -> bool:
    """Whether Claude actually accepted the wake prompt we just typed.

    ``send_prompt_via_tmux`` reports success once its tmux commands succeed,
    which is NOT the same as Claude having submitted the prompt — a dropped
    Enter leaves our wake text sitting in the composer, unsent. Codex confirms
    this itself (it waits for the prompt to leave the composer); Claude does
    not, so we check here.

    This gate exists because marking a wake as outstanding is a promise to stop
    pinging. Get it wrong and delivery to that agent stops permanently: the
    empty-inbox self-heal can't rescue it, since nothing is being delivered and
    so the inbox never empties.

    After a successful send the composer holds exactly what we restored — the
    user's draft, or nothing. Anything else (notably our own wake text) means
    the submit didn't land, so we report unconfirmed and let the next message
    ping again. Erring toward a redundant prompt is the safe direction.
    """
    deadline = time.time() + _WAKE_CONFIRM_TIMEOUT_S
    while True:
        ok, pane = capture_pane(tmux_name, join_wrapped=True, include_styles=True)
        if ok:
            visible, draft = claude_input_box(pane)
            if visible and draft == expected_draft:
                return True
        if time.time() >= deadline:
            return False
        time.sleep(_WAKE_CONFIRM_POLL_S)


def _wake_via_tmux(tmux_name: str, *, runtime: str = RUNTIME_CLAUDE) -> bool:
    """Type and submit the wake prompt in a session's tmux pane.

    Claude uses literal ``send-keys`` for the text. Codex uses tmux's explicit
    bracketed-paste path, requires the composer to be focused, saves and clears any
    partial draft, then uses Enter to submit while idle or steer at the next tool
    boundary while working. Codex submission and draft restoration are both verified.
    Returns False on any tmux error, unavailable composer, or unexpected Codex
    composer content.
    """
    try:
        if runtime == RUNTIME_CODEX:
            return _wake_codex_via_tmux(tmux_name)
        else:
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_name, "-l", _WAKE_PROMPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=True,
            )
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_name, "Enter"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _auto_resume(target, cwd: str) -> None:
    """Spawn a multiverse session that resumes a dormant/ended target.

    Headless mirror of the monitor's resume flow: reuse the target's name,
    reaping an ended-but-still-recorded multiverse session first so
    create_session's name-collision check doesn't fire. SessionStart +
    bin/multiverse-session's inbox-prime then deliver the pending message
    without any user action.
    """
    session_id = target.session_id
    if session_id is None:
        raise RuntimeError(
            f"target '{target.name}' has no tracked {target.runtime} session ID — "
            "cannot resume programmatically"
        )
    if target.session is not None and target.status == "ended":
        if foreign_host_still_reported_live(target.session):
            raise RuntimeError(
                f"'{target.name}' was created on host "
                f"'{target.session.host}' and its status there still says "
                "it's active — refusing to auto-resume as a new session "
                "here to avoid creating a duplicate that would share its "
                "inbox"
            )
        kill_session(target.session)
    if target.runtime == RUNTIME_CLAUDE:
        runtime_args = ["--resume", session_id]
    elif target.runtime == RUNTIME_CODEX:
        runtime_args = ["resume", session_id]
    else:
        runtime_args = ["--session", session_id]
    create_session(
        cwd=cwd,
        claude_args=runtime_args,
        resumed_from=session_id,
        name=target.name,
        runtime=target.runtime,
    )


def _queued_next_step(
    runtime: str, *, dormant: bool = False, host: str | None = None
) -> str:
    """Describe how an inbox-only message will reach the target.

    ``host`` names the machine the recipient is running on when that is not
    this one. A cross-host message is still delivered — the inbox is a file on
    the shared mount and the recipient's own hooks collect it over there — so
    the caller is told where it landed rather than being told it failed.
    """
    if runtime == RUNTIME_CODEX:
        if dormant:
            return (
                "It will remain queued until the user resumes that session; "
                "the Codex wrapper will then prompt it to call check_inbox."
            )
        if host:
            return (
                f"It will remain queued until Codex calls check_inbox on host '{host}'. "
                "A dashboard running on that host can relay the wake request; "
                "no terminal ping was sent from this host."
            )
        return "It will remain queued until Codex calls check_inbox."
    if runtime == RUNTIME_PI:
        if dormant:
            return "The pi extension will deliver it when the session resumes."
        if host:
            return (
                f"Its pi extension on {host} will deliver it from the durable "
                "inbox."
            )
        return "The pi extension will deliver it directly from the durable inbox."
    if dormant:
        return "It will be delivered when the user next resumes that session."
    if host:
        # Deliberately does not promise the nudge: the relay only fires if a
        # dashboard is running on that host, and the honest floor is what the
        # recipient's own hooks guarantee.
        return (
            f"Its own hooks on {host} will deliver it on its next turn, and a "
            "dashboard running there will prompt it to check sooner."
        )
    return "It will be delivered on the recipient's next turn."


_DEFAULT_MAX_AGENTS = 15

_STATUS_PRIORITY = {
    "working": 0,
    "waiting": 0,
    "listening": 1,
    "starting": 1,
    "idle": 1,
    "ending": 1,
    "retiring": 1,
    "unknown": 2,
    "ended": 3,
    "retired": 3,
    "dormant": 4,
}


@interagent_tool
def set_description(
    description: Annotated[str, Field(
        description="Very short, concise summary of responsibilities, completed tasks, "
        "ongoing implementations, or expertise: at most 60 characters, one line. "
        "Help others see what to delegate to you. Empty string clears it."
    )],
) -> str:
    """Set or update YOUR OWN very short, concise agent description.

    Summarize responsibilities, completed tasks, ongoing implementations, or
    expertise so other agents and the user can tell at first glance what tasks
    can be allocated to you. Use a brief label, not a detailed report or paragraph.
    Hard limit: 60 Unicode characters, including spaces and punctuation.
    One line only; no newlines or control characters. Over-limit calls fail
    with an explicit error and leave your existing description unchanged.

    Other agents see it in list_agents, and the user sees it on the dashboard.
    Call this at your own discretion when ready, and again whenever your
    work or responsibilities change to keep it accurate. New agents have no description.
    You can only change your own description, never another agent's. Pass an
    empty string to clear it and restore the missing-description reminder.
    """
    name, cwd = _resolve_self_identity()
    if not name or not cwd:
        return (
            "Error: set_description requires a named multiverse-managed session "
            "(MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME unset)."
        )
    try:
        value = write_description(cwd, name, description)
    except ValueError as exc:
        return f"Error: {exc}. Your existing description was not changed."
    except (OSError, RuntimeError) as exc:
        return f"Error: could not save your description: {exc}"
    if value is None:
        return "Your agent description was cleared."
    return f"Your agent description is now: {value}"


@interagent_tool
def list_agents(
    max_agents: int = _DEFAULT_MAX_AGENTS,
    working_directory: str = "",
) -> str:
    """List all named agents in your multiverse workspace.

    These are the agents you can talk to via inter-agent tools. By default
    the list is scoped to your own workspace's working directory. Pass
    ``working_directory`` to instead list agents in a different workspace —
    useful for discovering agents you can ``ask_agent`` across workspaces.

    Status values:
      - idle / working / waiting / starting — currently running
      - listening — parked in monitor_inbox, waiting on an inter-agent
                   message (running, but not on a user turn)
      - compacting — a compaction holds this session; it is not
                   interactive and cannot be attached, resumed or killed
                   until that finishes
      - ending   — runtime teardown is in progress (not interactive)
      - retiring — handoff is pending (not running or interactive)
      - ended    — was running, now stopped (still resumable)
      - retired  — permanently stopped after handing off to a successor
      - dormant  — never wrapped by multiverse this run, but the underlying
                   transcript exists and is named
      - unknown  — status file unreadable or not yet written

    Agents the user has filed into a monitor folder show it in square
    brackets (nested folders as ``parent/child``). Agents you spawn are
    filed alongside you automatically, so a shared folder is a good hint
    that you are working the same problem.

    Your own entry is marked with ``← this is you`` (only when listing your
    own workspace). The list is sorted with you first, then live agents,
    then ended/retired/dormant. Claude and Codex agents share this registry, but the
    output intentionally does not expose an agent's runtime.

    The Description column shows each agent's short, self-maintained
    work/responsibility summary, or "None" when unset. Use set_description at
    your discretion to keep your own description current.

    Args:
        max_agents: Maximum entries to show before truncating with "and N
            more". Default 15 keeps the output compact. Pass a larger value
            (e.g. 100) if you need the full roster — your own entry is
            always kept visible regardless of this cap.
        working_directory: Optional absolute path to another workspace's
            working directory. When omitted, lists agents in your own
            workspace. When provided, lists agents in that workspace
            instead; the ``← this is you`` marker is suppressed.
    """
    self_name, self_cwd = _resolve_self_identity()
    self_name = self_name or ""

    if working_directory:
        try:
            cwd = str(Path(working_directory).expanduser().resolve())
        except (OSError, RuntimeError) as e:
            return f"Error: invalid working_directory '{working_directory}': {e}"
    else:
        if not self_cwd:
            return (
                "Error: MULTIVERSE_SESSION_CWD is not set — this tool requires "
                "running inside a multiverse-managed session."
            )
        cwd = self_cwd

    self_cwd_abs = ""
    if self_cwd:
        try:
            self_cwd_abs = str(Path(self_cwd).expanduser().resolve())
        except (OSError, RuntimeError):
            self_cwd_abs = self_cwd
    in_own_workspace = bool(self_cwd_abs) and cwd == self_cwd_abs

    # Suppress the "← this is you" marker when listing another workspace.
    self_name_for_marker = self_name if in_own_workspace else ""

    try:
        agents = list_named_agents_in_cwd(cwd)
    except Exception as e:
        return f"Error listing agents: {e}"

    if not agents:
        if in_own_workspace or not working_directory:
            return "(no named agents in this workspace)"
        return f"(no named agents in workspace '{cwd}')"

    # Sort: you first, then by liveness, then by name (stable display).
    agents.sort(
        key=lambda a: (
            a.name != self_name_for_marker,
            _STATUS_PRIORITY.get(a.status, 9),
            a.name,
        )
    )

    total = len(agents)
    live_count = sum(
        1 for a in agents if a.status not in ("ended", "retiring", "retired", "dormant")
        and (a.session is None or a.session.status not in ("retiring", "retired"))
    )
    cap = max(1, int(max_agents))

    # Truncate, but keep "you" in the visible slice no matter what
    # (only relevant when listing your own workspace).
    if total > cap:
        visible = agents[:cap]
        if self_name_for_marker and not any(
            a.name == self_name_for_marker for a in visible
        ):
            you = next(
                (a for a in agents if a.name == self_name_for_marker), None
            )
            if you is not None:
                visible = [you] + visible[: cap - 1]
        hidden = total - len(visible)
    else:
        visible = agents
        hidden = 0

    if in_own_workspace or not working_directory:
        summary = f"{total} agent(s) in workspace ({live_count} live)"
    else:
        summary = (
            f"{total} agent(s) in workspace '{cwd}' ({live_count} live)"
        )

    # Folder membership from the monitor's grouping, shown so an agent can
    # see who it is teamed with — a spawner and its subagents share one.
    try:
        group_state = groups.load(cwd)
    except OSError:
        group_state = groups.GroupState()

    name_width = max(len("Name"), *(len(a.name) for a in visible))
    status_width = max(len("Status"), *(len(a.status) for a in visible))
    lines = [summary, "", f"  {'Name'.ljust(name_width)}  {'Status'.ljust(status_width)}  Description"]
    for a in visible:
        marker = "  ← this is you" if a.name == self_name_for_marker else ""
        gid = group_state.group_of(a.name)
        folder = "/".join(group_state.path_names(gid)) if gid else ""
        folder_cell = f"  [{folder}]" if folder else ""
        status = a.status.ljust(status_width)
        description = a.description or "None"
        lines.append(
            f"- {a.name.ljust(name_width)}  {status}  {description}{folder_cell}{marker}"
        )
    if hidden:
        lines.append(
            f"  … and {hidden} more not shown "
            f"(call again with max_agents={total} to see all)"
        )
    return "\n".join(lines)


@interagent_tool
def message_agent(
    target_name: str,
    message: str,
    working_directory: str = "",
) -> str:
    """Send a durable message to another named agent.

    Use this for instructions, in-depth questions, or any communication
    where the recipient should be able to take action — read files, run
    commands, use tools, then reply at their own pace.

    By default the target is looked up in your own workspace. Pass
    ``working_directory`` (an absolute path) to message an agent in a
    DIFFERENT workspace — use ``list_agents(working_directory=...)`` to
    discover targets there. A cross-workspace message records your own
    workspace with it, so the delivered message tells the recipient to
    reply with ``working_directory=<your workspace>``.

    The message is always dropped into the recipient's inbox and persists
    in their conversation history once delivered. To reply to a message you
    received, call ``message_agent`` back at the original sender (passing
    ``working_directory`` if the message says the sender is in another
    workspace) — there is no separate "respond" tool. For a quick fact you
    don't want to persist in the target's session, use ``ask_agent``.

    DELIVERY is always active: the recipient is woken now, no user needed.
    Behavior depends on the recipient's state:
      - dormant/ended → the agent is auto-resumed (started for you) and
        processes the message on startup.
      - idle or working (Claude), with its input box on screen → it's
        pinged via its terminal to check its inbox immediately, whether or
        not a user is attached. Anything the user had typed but not sent is
        saved and restored around the ping.
      - idle or working (Claude), with its input box NOT on screen → it's
        sitting on a permission prompt or a question modal, so nothing is
        typed and delivery stays queued.
      - idle or working (Codex), with its input box focused → it's pinged
        via its terminal whether or not a user is attached. Anything the
        user had typed but not sent is saved and restored around the ping.
      - idle or working (Codex), with its input box NOT focused → nothing
        is typed and delivery stays queued.
      - listening (parked in ``monitor_inbox``) → it's already watching its
        inbox and picks the message up within a second; no wake needed.
      - starting → remains queued; no terminal input is sent while the
        runtime TUI is not ready for a normal prompt.
      - waiting (blocked on a permission/input prompt) → NOT woken; a
        terminal ping can't reach a blocked pane. The message queues and
        is delivered once the user resolves the prompt and the turn
        continues.
      - compacting → the agent is locked (its terminal is killed and even
        the user can't interact with it during compaction), so it is NOT
        woken; the message queues and is delivered once compaction finishes
        and the session resumes.
      - ending → runtime teardown is already in progress, so it is NOT woken
        or auto-resumed; the message stays queued for the next resume.
      - retired → rejected without writing to the inbox; the result names the
        successor that should be messaged instead.
      - retiring → rejected without writing to the inbox; try again later.

    Except for retiring/retired recipients, the message is queued in the inbox
    before delivery is attempted, so states that can't be woken still receive
    it through their normal path. The tool result tells you exactly what happened.

    At most one outstanding prompt: if the recipient was already asked to
    check its inbox and hasn't done so yet, no second prompt is sent. Your
    message is still queued and the pending prompt collects it along with
    everything else — reading an inbox drains all of it at once. So sending
    several messages in a row to a busy agent costs it one interruption, not
    one per message, and nothing is lost.

    Args:
        target_name: Name of the agent to message. Use ``list_agents`` to
            see who's available.
        message: The message text. Can be multi-line, can include
            instructions, code, anything.
        working_directory: Optional absolute path to another workspace. When
            omitted, the target is looked up in your own workspace. When
            provided, the target (and any auto-resume / inbox delivery)
            happens in that workspace instead.
    """
    self_name, self_cwd = _resolve_self_identity()
    if not self_cwd or not self_name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME unset)."
        )

    # Resolve the workspace the target lives in — our own by default.
    if working_directory:
        try:
            cwd = str(Path(working_directory).expanduser().resolve())
        except (OSError, RuntimeError) as e:
            return f"Error: invalid working_directory '{working_directory}': {e}"
    else:
        cwd = self_cwd

    try:
        self_cwd_abs = str(Path(self_cwd).expanduser().resolve())
    except (OSError, RuntimeError):
        self_cwd_abs = self_cwd
    in_own_workspace = cwd == self_cwd_abs

    if in_own_workspace and target_name == self_name:
        return "Error: cannot message yourself."

    target = find_agent_by_name(cwd, target_name)
    if target is None:
        if in_own_workspace:
            return (
                f"Error: no agent named '{target_name}' in this workspace. "
                "Call list_agents to see who's available."
            )
        return (
            f"Error: no agent named '{target_name}' in workspace '{cwd}'. "
            f"Call list_agents(working_directory='{cwd}') to see who's "
            "available."
        )

    if target.status == "retiring" or (
        target.session is not None and target.session.status == "retiring"
    ):
        return retiring_agent_message()

    # Retirement is permanent and differs from ended/dormant: the native
    # transcript remains available to ask_agent and compaction, but no new
    # durable input may enter it. Check the underlying Session too because a
    # compaction lock temporarily overlays the dashboard row status.
    if target.status == "retired" or (
        target.session is not None and target.session.status == "retired"
    ):
        return retired_agent_message(
            target_name,
            target.session.successor if target.session is not None else None,
        )

    # Called for its side effect, not its answer: wake_is_outstanding() retires
    # a stale marker (one with an empty inbox behind it — e.g. the agent was
    # killed after being pinged). That check only works BEFORE our own write
    # adds a message, which is why it happens here. The decision itself is made
    # later, re-checked under the per-recipient lock.
    wake_is_outstanding(cwd, target_name)

    # Always write the inbox file before any auto-resume. Claude's SessionStart
    # hook and Codex's wrapper-side startup prompt must both observe the queued
    # message. For cross-workspace sends, stamp the sender workspace so replies
    # can route back correctly.
    inbox_file = write_message_to_inbox(
        sender_name=self_name,
        recipient_name=target_name,
        recipient_cwd=cwd,
        message=message,
        sender_cwd="" if in_own_workspace else self_cwd_abs,
    )
    caller_runtime = os.environ.get("MULTIVERSE_RUNTIME", RUNTIME_CLAUDE)
    if caller_runtime not in (RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_PI):
        caller_runtime = RUNTIME_CLAUDE

    log_message_sent(
        sender=self_name,
        recipient=target_name,
        cwd=cwd,
        # Retired as a caller-facing argument — delivery is always active
        # now. Kept in the event schema so historical logs stay comparable.
        push_notification=True,
        message_id=inbox_file.stem,
        message=message,
        sender_runtime=caller_runtime,
        recipient_runtime=target.runtime,
    )

    def log_route(route: str, success: Optional[bool] = None) -> None:
        log_notification_routed(
            sender=self_name,
            recipient=target_name,
            cwd=cwd,
            route=route,
            success=success,
            sender_runtime=caller_runtime,
            recipient_runtime=target.runtime,
        )

    is_dormant = target.session is None or target.status == "ended"

    reply_hint = (
        " If you're expecting a reply, finish any other work first, then "
        "call monitor_inbox to wait for it (it blocks), or check_inbox later."
    )

    def ending_queue_result() -> str:
        log_route("passive_fallback_ending")
        return (
            f"Message queued for '{target_name}' — it's currently ending, so "
            "no terminal ping or auto-resume was attempted. "
            + _queued_next_step(target.runtime, dormant=True)
            + reply_hint
        )

    # A compacting agent is LOCKED: its tmux pane is killed for the duration
    # of the compaction and even the user can't attach/interact. Never wake
    # it (no auto-resume, no terminal ping) — leave the message queued, to
    # be picked up when compaction finishes and the session resumes. Checked
    # against both the durable status and compaction lock. The explicit status
    # check closes the short windows before lock discovery or after a stale
    # lock has been cleaned up.
    if target.status == "compacting" or (
        target.claude_uuid and is_locked(target.claude_uuid)
    ):
        log_route("passive_fallback_compacting")
        return (
            f"Message queued for '{target_name}' — it's currently compacting "
            "(locked), so the message will be delivered once compaction "
            "finishes and the session resumes." + reply_hint
        )

    if target.status == "ending" or (
        target.session is not None
        and target.session.ending_marker_path.is_file()
    ):
        return ending_queue_result()

    # Actively wake the recipient.
    if is_dormant:
        try:
            _auto_resume(target, cwd)
        except Exception as e:  # noqa: BLE001 — surface as tool error text
            log_route("auto_resume", success=False)
            if target.runtime == RUNTIME_CLAUDE:
                return (
                    f"Message queued for '{target_name}', but auto-resume failed "
                    f"({e}). It will be delivered when the session is next "
                    "resumed." + reply_hint
                )
            return (
                f"Message queued for '{target_name}', but auto-resume failed "
                f"({e}). "
                + _queued_next_step(target.runtime, dormant=True)
                + reply_hint
            )
        log_route("auto_resume", success=True)
        if target.runtime == RUNTIME_CODEX:
            return (
                f"Woke '{target_name}' (it was dormant) and prompted Codex "
                "to call check_inbox on startup." + reply_hint
            )
        return (
            f"Woke '{target_name}' (it was dormant) and delivered your "
            "message — it will process it on startup." + reply_hint
        )

    status = target.status

    if status == "listening":
        log_route("already_listening")
        if target.runtime == RUNTIME_CLAUDE:
            return (
                f"Delivered to '{target_name}' — it was already listening on its "
                "inbox and will pick the message up within a second." + reply_hint
            )
        return (
            f"Message queued for '{target_name}' — it was already listening "
            "on its inbox and will detect it within a second." + reply_hint
        )

    # A recipient on another host cannot be typed into from here: tmux servers
    # are per-machine, so send-keys would go nowhere (or, worse, into a local
    # session that happened to reuse the name). The message is already in the
    # shared inbox and the recipient's own hooks will collect it; what cannot
    # cross is the nudge that makes an idle agent look now rather than on its
    # next turn. File that as a request for the dashboard on its host.
    #
    # Placed ahead of the starting/waiting branch on purpose. Those states are
    # transient, and a message arriving during one would otherwise never get a
    # request filed at all — the agent becomes idle later with nothing pending
    # to nudge it. The relay holds the request until that host can act, and
    # declines to type until the session is actually in a state to receive it.
    # ``listening`` is handled above and needs nothing: an agent polling its
    # inbox reads files, which already crosses hosts.
    if is_foreign_host(target.session):
        relayed = wake_relay.request_wake(target.session, requested_by=self_name)
        log_route("wake_relay_requested" if relayed else "wake_relay_pending")
        host = target.session.host or "another host"
        return (
            f"Message queued for '{target_name}' — it is running on host "
            f"{host}, which this host cannot type into. "
            + _queued_next_step(target.runtime, host=host)
            + reply_hint
        )

    if status in ("starting", "waiting"):
        log_route(f"passive_fallback_{status}")
        if target.runtime == RUNTIME_CLAUDE:
            if status == "waiting":
                return (
                    f"Message queued for '{target_name}' — it's currently blocked "
                    "on a user permission/input prompt, so it can't be pushed. "
                    "The message will be delivered the moment the user resolves "
                    "the prompt and the turn continues." + reply_hint
                )
            return (
                f"Message queued for '{target_name}' (status: {status}). It will "
                "be delivered on the recipient's next turn." + reply_hint
            )
        if status == "starting":
            reason = "it is still starting"
        else:
            reason = "it is waiting for a user permission/input decision"
        return (
            f"Message queued for '{target_name}' — {reason}, so no terminal "
            "ping was sent. "
            + _queued_next_step(target.runtime)
            + reply_hint
        )

    if status in ("idle", "working"):
        if target.runtime == RUNTIME_PI:
            # pi's extension owns live delivery through its in-process
            # sendMessage API. Typing into its tmux pane would be both less
            # reliable and liable to corrupt a user's draft. The extension
            # polls while idle and drains once a working turn settles.
            log_route("pi_extension_delivery")
            timing = "within a couple of seconds" if status == "idle" else "when its current turn settles"
            return (
                f"Message queued for '{target_name}'. Its pi extension will "
                f"deliver it {timing}." + reply_hint
            )

        # Serialized per recipient across sender processes: the check below,
        # the ping, and the resulting mark must be one indivisible step, or two
        # concurrent senders both read "nothing outstanding" and both ping.
        with wake_delivery_lock(cwd, target_name):
            # The dashboard can claim teardown after our roster snapshot but
            # before we acquire the recipient delivery lock. Re-check the
            # durable marker before sending any keystrokes.
            if target.session.ending_marker_path.is_file():
                return ending_queue_result()

            # Authoritative re-check, inside the lock. The earlier call ran
            # before our own inbox write so it could still see an empty inbox
            # and retire a stale marker; this one decides.
            if wake_is_outstanding(cwd, target_name):
                # A wake prompt is already sitting with this agent, accepted
                # but not yet acted on. Typing another buys nothing: reading an
                # inbox drains ALL of it, so the outstanding prompt collects
                # this message too and a second would burn a turn arriving at
                # an empty inbox.
                log_route("skipped_wake_outstanding")
                return (
                    f"Message queued for '{target_name}'. It was already asked to "
                    "check its inbox and hasn't done so yet, so no second prompt "
                    "was sent — the pending one will pick this message up too."
                    + reply_hint
                )

            tmux_name = target.session.tmux_name

            if target.runtime == RUNTIME_CLAUDE:
                # Claude delivery is composer-gated rather than attachment-gated.
                # Whether a user is attached is irrelevant — what matters is whether
                # the composer is on screen to receive keystrokes:
                #   composer visible  → deliver, preserving any typed-but-unsent
                #                       draft (save → clear → send+submit → restore).
                #   composer missing  → a permission prompt / AskUserQuestion modal
                #                       is up (or the pane is scrolled away).
                #                       Keystrokes would answer the modal, so leave
                #                       the message queued.
                ok, saved, err = send_prompt_via_tmux(
                    tmux_name,
                    _WAKE_PROMPT,
                    runtime=RUNTIME_CLAUDE,
                    require_composer=True,
                )
                if not ok and "composer is not visible" in err:
                    log_route("passive_fallback_no_composer")
                    return (
                        f"Message queued for '{target_name}' — its input box isn't "
                        "on screen (it's on a permission prompt or a question "
                        "modal), so no keystrokes were sent. The message will be "
                        "delivered on the recipient's next turn." + reply_hint
                    )
                log_route("wake_tmux", success=ok)
                if not ok:
                    return (
                        f"Message queued for '{target_name}', but the terminal ping "
                        f"failed ({err}) — it will be delivered on the recipient's "
                        "next turn." + reply_hint
                    )
                # Mark only once the submit is CONFIRMED, not merely attempted:
                # the marker suppresses later pings, so a wake still sitting in
                # the composer would stop delivery to this agent for good.
                # Marking inside the lock is also what lets a loser re-check
                # after our outcome is known, so a failure here doesn't strand
                # their message.
                if _claude_wake_confirmed(tmux_name, saved):
                    mark_wake_sent(cwd, target_name)
                preserved = (
                    " Its typed-but-unsent input was saved and restored."
                    if saved
                    else ""
                )
                return (
                    f"Delivered to '{target_name}' and pinged its terminal to check "
                    f"its inbox now (it was {status})." + preserved + reply_hint
                )

            woke = _wake_via_tmux(tmux_name, runtime=target.runtime)
            log_route("wake_tmux", success=woke)
            if woke:
                # Success only — see the Claude branch above for why.
                mark_wake_sent(cwd, target_name)
                return (
                    f"Message queued for '{target_name}' and its terminal was "
                    f"pinged to call check_inbox now (it was {status})." + reply_hint
                )
            return (
                f"Message queued for '{target_name}', but the terminal ping "
                "failed. " + _queued_next_step(target.runtime) + reply_hint
            )

    # Unknown or future live states are never safe targets for terminal input.
    log_route("passive_fallback_status")
    if target.runtime == RUNTIME_CLAUDE:
        return (
            f"Message queued for '{target_name}' (status: {status}). It will be "
            "delivered on the recipient's next turn." + reply_hint
        )
    return (
        f"Message queued for '{target_name}' (status: {status}); no terminal "
        "ping was sent. "
        + _queued_next_step(target.runtime)
        + reply_hint
    )


@interagent_tool
def check_inbox() -> str:
    """Read pending inbox messages now, without waiting for a hook to fire.

    Codex sessions use this as their explicit inbox-delivery path; pi also
    exposes it through its extension. Call it
    when a push notification or resume prompt asks you to check your inbox,
    after ``monitor_inbox`` reports a message, or whenever you suspect a
    reply arrived during a long-running turn. Claude sessions can use the
    same tool mid-turn in addition to their automatic hook delivery.

    Returns either a system-reminder block (identical to the hook-delivered
    format) containing every pending message, OR the literal string
    ``"You have no pending inbox messages."`` when the inbox is empty.

    Calling this **clears your inbox**. Each pending file is atomically
    claimed before it is returned, so racing tools or processes cannot
    deliver the same message twice. Don't call it without intending to act
    on the messages.
    """
    name, cwd = _resolve_self_identity()
    if not cwd or not name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME unset)."
        )

    # Acting on the wake prompt is what clears it — checking and finding an
    # empty inbox still counts, so senders may prompt us again. Done before
    # the early return below for exactly that case.
    clear_wake_sent(cwd, name)

    # Claims (reads + STAGES) — safe if a duplicate-session process is
    # also polling this same inbox; see claim_pending_messages. The
    # staged files survive until commit_claimed_messages below, so a
    # throw in between doesn't destroy undelivered messages.
    messages = claim_pending_messages(cwd, name)
    if not messages:
        return "You have no pending inbox messages."

    log_inbox_delivered(
        recipient=name,
        cwd=cwd,
        hook_event="check_inbox_tool",
        messages=messages,
        recipient_runtime=os.environ.get("MULTIVERSE_RUNTIME", RUNTIME_CLAUDE),
    )
    rendered = render_messages_as_tool_result(messages)

    # Render succeeded and `rendered` is what the caller receives — the
    # last point we can observe from here. Committing before this put
    # log_inbox_delivered and the renderer inside the destroy window.
    commit_claimed_messages(messages)
    return rendered


@interagent_tool
async def monitor_inbox(
    timeout_seconds: int = _DEFAULT_MONITOR_INBOX_TIMEOUT,
) -> str:
    """Block until an inbox message arrives, then signal you to read it.

    Use this when you've finished your turn but are expecting replies — e.g.
    you just sent ``message_agent`` to one or more agents and want to wait
    for their responses instead of ending your turn blind. This tool parks
    until the first message lands in your inbox (or the timeout elapses),
    so you don't have to busy-loop on ``check_inbox``.

    This tool does NOT consume or return the message contents — it only
    waits and tells you something arrived. After it returns with a new
    message, call ``check_inbox`` to atomically read and clear your inbox.
    This behavior is identical in managed Claude and Codex sessions.

    If you already have pending messages when you call this, it returns
    immediately telling you to check — it never makes you wait when there's
    already something to read.

    The user can interrupt this wait at any time (Esc) to prompt you — the
    call ends and your turn resumes with their input.

    Args:
        timeout_seconds: Max seconds to wait for this call. Default 300
            (5 minutes), not a cap: choose a larger value when desired,
            e.g. 86400 for one day. On timeout, returns a "no new messages"
            notice — you can call again to keep waiting, or move on.
    """
    name, cwd = _resolve_self_identity()
    if not cwd or not name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME unset)."
        )

    # Deliberately does NOT clear the outstanding-wake marker: this tool only
    # DETECTS, it doesn't consume. Clearing here would open a window where the
    # agent has been told a message exists but hasn't read it yet, and a new
    # sender would type a redundant prompt at it. A parked agent reads as
    # `listening`, which is already excluded from pinging, and the marker is
    # retired for real once check_inbox drains the inbox.

    if pending_inbox_count(cwd, name) > 0:
        return (
            "You already have pending inbox messages. Call check_inbox to "
            "read them."
        )

    # Show "listening" while parked so the user (and other agents via
    # list_agents) can tell this agent is waiting for messages, not busy.
    # The finally restores "working" — the turn continues after we return
    # (the agent will call check_inbox / act), and on a user interrupt the
    # Stop hook follows up with "idle".
    _write_session_status(_LISTENING_STATUS)
    try:
        # Async sleep (not time.sleep) so a user interrupt — which the MCP
        # layer surfaces as request cancellation — propagates CancelledError
        # through the await and stops this loop immediately, instead of
        # leaking a worker thread that keeps polling until the timeout.
        timeout = max(1, int(timeout_seconds))
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(_MONITOR_INBOX_POLL_INTERVAL)
            if pending_inbox_count(cwd, name) > 0:
                return (
                    "You have a new inbox message. Call check_inbox to read it."
                )

        return (
            f"No new inbox messages arrived within {timeout}s. Call "
            "monitor_inbox again to keep waiting, or proceed without a reply."
        )
    finally:
        _write_session_status("working")


@interagent_tool
def ask_agent(
    target_name: str,
    question: str,
    timeout_seconds: int = _DEFAULT_ASK_TIMEOUT,
    working_directory: str = "",
) -> str:
    """Ask a single named agent a quick question and wait for the answer.

    Use this for ephemeral, fact-gathering questions: "what are you working
    on?", "did you implement feature X?", "do you remember the API key
    location?". The target answers from their existing context only — they
    have NO tools available, can't read files or run commands. If the
    answer requires investigation, send a ``message_agent`` instead.

    By default the target is looked up in your own workspace. Pass
    ``working_directory`` to ask an agent in a different workspace —
    cross-workspace questions are allowed (read-only, ephemeral). Use
    ``list_agents(working_directory=...)`` to discover targets there.

    Mechanism: spawns a forked, one-shot subprocess against the target's
    native Claude, Codex, or pi session (same approach as the monitor's ``a``
    keybinding, but for a single agent). The target's main session is NOT
    involved — no notification, no entry in their conversation history.
    The question is purely sidechannel.

    This call BLOCKS until the target responds or the timeout elapses.

    Args:
        target_name: Name of the agent to ask. Use ``list_agents``.
        question: The question text. Keep it focused — the target can only
            answer from what they already know.
        timeout_seconds: Max seconds to wait. Default 60.
        working_directory: Optional absolute path to another workspace's
            working directory. When omitted, looks up the target in your
            own workspace. When provided, looks up the target in that
            workspace instead.
    """
    self_name, self_cwd = _resolve_self_identity()
    if not self_cwd or not self_name:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session."
        )

    if working_directory:
        try:
            cwd = str(Path(working_directory).expanduser().resolve())
        except (OSError, RuntimeError) as e:
            return f"Error: invalid working_directory '{working_directory}': {e}"
    else:
        cwd = self_cwd

    try:
        self_cwd_abs = str(Path(self_cwd).expanduser().resolve())
    except (OSError, RuntimeError):
        self_cwd_abs = self_cwd
    in_own_workspace = cwd == self_cwd_abs

    if in_own_workspace and target_name == self_name:
        return "Error: cannot ask yourself. Use your own context directly."

    target = find_agent_by_name(cwd, target_name)
    if target is None:
        if in_own_workspace:
            return (
                f"Error: no agent named '{target_name}' in this workspace. "
                "Call list_agents to see who's available."
            )
        return (
            f"Error: no agent named '{target_name}' in workspace '{cwd}'. "
            f"Call list_agents(working_directory='{cwd}') to see who's available."
        )
    if target.session_id is None and target.claude_uuid is None:
        return (
            f"Error: '{target_name}' has no tracked {target.runtime} session id yet "
            "(may have just started). Try again in a moment."
        )

    timeout = max(1, int(timeout_seconds))
    result = ask_one(
        question=question,
        target=AskTarget(
            name=target.name,
            cwd=target.cwd,
            claude_uuid=target.claude_uuid,
            runtime=target.runtime,
            session_id=target.session_id,
        ),
        timeout=float(timeout),
    )

    if result.error:
        if "timed out" in result.error:
            return f"'{target_name}' did not respond within {timeout}s."
        return f"Error asking '{target_name}': {result.error}"
    return result.response or f"'{target_name}' returned an empty response."


@interagent_tool
async def spawn_agent(
    instruction: str,
    name: str = "",
    ctx: Context | None = None,
) -> str:
    """Spawn a brand-new subagent in your workspace with a starting task.

    The ``instruction`` you pass becomes the subagent's first user message —
    the subagent starts a fresh session and acts on it immediately.

    The subagent has NO knowledge of your prior conversation. If context
    from your conversation is relevant to the task, include a targeted
    summary directly in the instruction. Be deliberate — the subagent only
    sees what you put here.

    User flow: a widget opens in the multiverse monitor showing your
    instruction, a suggested name, and a Claude/Codex/pi runtime selector. The
    user can accept, edit the name, choose a runtime, or reject. This
    tool blocks until they respond — there is NO timeout, so the request
    waits however long the user needs. (You can interrupt the wait with Esc
    to give up; that cancels the request and no subagent is created.) If
    rejected, no subagent is created.

    A rejection may come with a message from the user, quoted back to you in
    the tool result.

    Args:
        instruction: The first user message for the new agent. Can be
            multi-line, can include task context, code, a summary of your
            own prior context — anything the subagent should see to start
            its work.
        name: Optional preferred name for the new agent. The user sees this
            pre-filled in the naming widget and can accept or edit it. If
            empty or already taken in this workspace, a random name is
            suggested instead.
    """
    self_name, cwd = _resolve_self_identity()
    caller_sid = os.environ.get("MULTIVERSE_SESSION_ID")
    if not cwd or not self_name or not caller_sid:
        return (
            "Error: this tool requires running inside a multiverse-managed "
            "session (MULTIVERSE_SESSION_CWD / MULTIVERSE_SESSION_NAME / "
            "MULTIVERSE_SESSION_ID unset)."
        )
    try:
        caller_runtime = normalize_runtime(
            os.environ.get("MULTIVERSE_RUNTIME", RUNTIME_CLAUDE)
        )
    except ValueError as e:
        return f"Error: invalid caller runtime: {e}"

    if not instruction or not instruction.strip():
        return "Error: instruction cannot be empty."

    existing = all_session_names_in_cwd(cwd)
    requested = (name or "").strip()
    if requested and requested not in existing:
        default_name = requested
    else:
        default_name = pick_random_name(existing) or ""
    if not default_name:
        return (
            "Error: could not pick a default name — the workspace name "
            "pool may be exhausted. Kill some ended sessions and retry."
        )

    req_id = new_request_id()
    caller_tmux = f"multiverse-{caller_sid}"

    try:
        write_spawn_request(
            req_id=req_id,
            from_name=self_name,
            cwd=cwd,
            instruction=instruction,
            default_name=default_name,
            caller_tmux_session=caller_tmux,
            caller_runtime=caller_runtime,
        )
    except OSError as e:
        return f"Error: failed to write spawn request: {e}"

    # Nudge any user attached to the caller's pane. Best-effort — if the
    # tmux session doesn't exist or tmux isn't on PATH, skip silently.
    try:
        subprocess.run(
            [
                "tmux", "display-message", "-t", caller_tmux,
                f"'{self_name}' wants to spawn a subagent — "
                "detach (Ctrl-b d) to approve in the monitor.",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        # Wait indefinitely for the user to accept/reject in the monitor —
        # no timeout. Async sleep so a user interrupt (Esc) surfaces as
        # cancellation: CancelledError propagates through the `finally`,
        # which cleans up the request file, and the monitor dismisses any
        # open modal once that file is gone.
        #
        # Send an MCP progress notification every _SPAWN_PROGRESS_INTERVAL
        # seconds. The MCP protocol's canonical mechanism for long-running
        # tools: it advances any progress-reset timeout on the client and
        # gives the user visible feedback ("Waiting for approval — 30s
        # elapsed") instead of a mystery-hang. Failures to report are
        # swallowed — an old/limited client shouldn't take the tool down.
        wait_start = time.time()
        next_progress_at = wait_start + _SPAWN_PROGRESS_INTERVAL
        while True:
            res = read_resolution(req_id)
            if res is not None:
                break
            await asyncio.sleep(_SPAWN_POLL_INTERVAL)
            now = time.time()
            if ctx is not None and now >= next_progress_at:
                elapsed = int(now - wait_start)
                try:
                    await ctx.report_progress(
                        progress=elapsed,
                        message=(
                            f"Waiting for user to accept/reject in the "
                            f"multiverse monitor ({elapsed}s elapsed)"
                        ),
                    )
                except Exception:  # noqa: BLE001
                    pass
                next_progress_at = now + _SPAWN_PROGRESS_INTERVAL

        status = res.get("status")
        if status != "accepted":
            reason = (res.get("reason") or "").strip()
            # "cancelled" is the marker for a bare Esc / external resolve — no
            # person typed anything, so it gets no quoted message clause.
            message = (
                f' with the message "{reason}"'
                if reason and reason != "cancelled"
                else ""
            )
            return (
                f"Spawn request rejected by the user{message}\n"
                "No subagent was created."
            )

        chosen = (res.get("name") or "").strip()
        if not chosen:
            write_outcome(
                req_id,
                cwd=cwd,
                created=False,
                error="resolution contained an empty name",
            )
            return "Error: resolution contained an empty name — no subagent created."
        try:
            child_runtime = normalize_runtime(
                res.get("runtime") or caller_runtime
            )
        except ValueError as e:
            write_outcome(
                req_id,
                cwd=cwd,
                created=False,
                name=chosen,
                error=f"invalid runtime: {e}",
            )
            return f"Error: resolution contained an invalid runtime: {e}"

        try:
            create_session(
                cwd=cwd,
                claude_args=[],
                name=chosen,
                runtime=child_runtime,
                # Pass the instruction off-band via a state-dir file rather
                # than as a CLI arg. This avoids: shell-escape bloat for
                # apostrophe-heavy / long instructions, the multi-stage
                # tmux→sh→multiverse-session re-parse, and any ARG_MAX
                # surprises. multiverse-session reads the file and hands
                # the content to the selected runtime as a positional argv
                # entry.
                initial_prompt=instruction,
            )
        except (subprocess.CalledProcessError, ValueError, RuntimeError) as e:
            # The monitor is holding this approval open waiting for the
            # agent to appear; tell it that it never will, so it doesn't
            # sit on a folder for a session that was never created.
            write_outcome(
                req_id,
                cwd=cwd,
                created=False,
                name=chosen,
                error=f"failed to create session: {e}",
            )
            return f"Error: failed to create session: {e}"

        write_outcome(req_id, cwd=cwd, created=True, name=chosen)

        # File the new agent in the monitor's folder tree next to whoever
        # spawned it, so a delegation lineage shows up as a folder instead
        # of scattering across a flat list. Purely cosmetic bookkeeping —
        # the session is already running, so a failure here must not turn
        # a successful spawn into an error.
        folder_note = ""
        try:
            gid = groups.auto_group_spawn(cwd, self_name, chosen)
            group = groups.load(cwd).get(gid)
            if group is not None:
                folder_note = f" in folder '{group.name}'"
        except (OSError, ValueError):
            pass

        return f"Spawned agent with name '{chosen}'{folder_note}."
    finally:
        cleanup_request(req_id)


if __name__ == "__main__":
    mcp.run()
