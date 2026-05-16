"""Temporary-question fan-out across multiverse agents.

Simulates claude-code's ``/btw`` side-question: each target gets a one-off,
tool-less response that does not persist in its main conversation.

Implementation: spawn ``claude -p --resume <uuid> --fork-session
--session-id <fresh-uuid>`` per target. The ``--fork-session`` creates an
isolated JSONL so the origin session is untouched; we delete the fork JSONL
after the subprocess exits.

Cache behavior: for live sessions, the subprocess rebuilds
``systemPrompt``/``userContext``/``systemContext`` from scratch. If the build
is deterministic (no ``--agent``/``--system-prompt`` extras, same plugin
config), the rebuilt bytes match the live process's last request → prompt
cache hit on the big prefix. For dormant sessions the cache is cold anyway,
so rebuilding is equivalent to /btw's fallback path.

Tool denial: ``--tools ""`` disables all tools (the documented disable-all
sentinel), and ``--max-turns 1`` early-exits after one assistant turn so a
non-compliant model can't loop. The strict side-question prompt (same copy
as ``sideQuestion.ts``) backs both up. ``--max-turns`` is a hidden flag —
present in claude-code's CLI but suppressed from ``--help`` — and works
with ``--print``.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import forks
from .paths import claude_projects_dir


CLAUDE_PROJECTS_DIR = claude_projects_dir()


SIDE_QUESTION_WRAPPER = """<system-reminder>This is a side question from the user. You must answer this question directly in a single response.

IMPORTANT CONTEXT:
- You are a separate, lightweight agent spawned to answer this one question
- The main agent is NOT interrupted - it continues working independently in the background
- You share the conversation context but are a completely separate instance
- Do NOT reference being interrupted or what you were "previously doing" - that framing is incorrect

CRITICAL CONSTRAINTS:
- You have NO tools available - you cannot read files, run commands, search, or take any actions
- This is a one-off response - there will be no follow-up turns
- You can ONLY provide information based on what you already know from the conversation context
- NEVER say things like "Let me try...", "I'll now...", "Let me check...", or promise to take any action
- If you don't know the answer, say so - do not offer to look it up or investigate

Simply answer the question with the information you have.</system-reminder>

"""


@dataclass
class AskTarget:
    name: str
    cwd: str
    claude_uuid: str


@dataclass
class AskResult:
    name: str
    response: Optional[str]
    error: Optional[str]
    fork_uuid: str
    elapsed: float


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _env_without_session_vars() -> dict[str, str]:
    """Return ``os.environ`` minus session-scoped ``MULTIVERSE_*`` vars.

    Why this matters: when ``ask_agent`` is called by a running multiverse
    agent, the MCP server (a subprocess of that agent's claude) inherits
    the agent's ``MULTIVERSE_STATUS`` / ``MULTIVERSE_SESSION_*`` env vars.
    If we launch the fork subprocess without stripping those, the fork's
    hooks (``record-claude-uuid.sh``, ``set-status.sh``, ``deliver-inbox.py``)
    will see the CALLER's session path and **write the fork's session_id
    into the caller's state dir** — corrupting the caller's ``claude_uuid``
    so dedup against dormant listings breaks and the caller appears twice
    in the dashboard (once live-wrapping-a-wrong-uuid, once as a phantom
    dormant row for the caller's real uuid).

    The fork is ephemeral, tool-less, single-turn — it has no business
    touching any multiverse state — so we strip ALL ``MULTIVERSE_*`` from
    its env. The hooks all have ``[ -z "${MULTIVERSE_STATUS:-}" ] && exit 0``
    guards, so stripping makes them no-op cleanly.

    ``MULTIVERSE_PLUGIN_ROOT`` is read by the caller BEFORE calling this,
    so the ``--plugin-dir`` CLI flag is still correctly populated.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("MULTIVERSE_")}


def _ask_one(
    target: AskTarget,
    question: str,
    plugin_root: Optional[str],
    timeout: float,
    register_fork: Optional[Callable[[str, bool], None]],
) -> AskResult:
    import time

    fork_uuid = str(_uuid.uuid4())
    wrapped = SIDE_QUESTION_WRAPPER + question
    jsonl = CLAUDE_PROJECTS_DIR / _encode_cwd(target.cwd) / f"{fork_uuid}.jsonl"

    cmd = ["claude"]
    if plugin_root:
        cmd += ["--plugin-dir", plugin_root]
    cmd += [
        "--resume", target.claude_uuid,
        "--fork-session",
        "--session-id", fork_uuid,
        "--tools", "",
        "--max-turns", "1",
        "-p", wrapped,
    ]

    if register_fork:
        register_fork(fork_uuid, True)
    # Register in the on-disk fork registry BEFORE spawning. This is what
    # keeps the fork hidden from the dashboard / list_agents — and, unlike
    # the monitor's in-memory set, it survives a kill mid-ask (when the
    # `finally` below never runs). gc() reaps the orphan + this entry later.
    forks.mark_active(fork_uuid, jsonl)

    start = time.time()
    err: Optional[str] = None
    response: Optional[str] = None
    try:
        r = subprocess.run(
            cmd,
            cwd=target.cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env_without_session_vars(),
        )
        if r.returncode != 0:
            err = (r.stderr.strip() or f"claude exited {r.returncode}")[:400]
        else:
            response = r.stdout.strip() or None
            if not response:
                err = "empty response"
    except subprocess.TimeoutExpired:
        err = f"timed out after {timeout:.0f}s"
    except FileNotFoundError:
        err = "claude CLI not found"
    except Exception as e:  # noqa: BLE001 — surface anything else as error text
        err = f"subprocess error: {e}"
    finally:
        try:
            jsonl.unlink(missing_ok=True)
        except OSError:
            pass
        forks.mark_done(fork_uuid)
        if register_fork:
            register_fork(fork_uuid, False)

    return AskResult(
        name=target.name,
        response=response,
        error=err,
        fork_uuid=fork_uuid,
        elapsed=time.time() - start,
    )


def ask_all(
    question: str,
    targets: list[AskTarget],
    timeout: float = 60.0,
    max_parallel: int = 5,
    register_fork: Optional[Callable[[str, bool], None]] = None,
) -> list[AskResult]:
    """Ask ``question`` of every target in parallel. Results returned in input order."""
    if not targets:
        return []
    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT")
    results: list[Optional[AskResult]] = [None] * len(targets)
    with ThreadPoolExecutor(max_workers=max_parallel) as ex:
        futures = {
            ex.submit(
                _ask_one, t, question, plugin_root, timeout, register_fork
            ): i
            for i, t in enumerate(targets)
        }
        for f in as_completed(futures):
            i = futures[f]
            results[i] = f.result()
    return [r for r in results if r is not None]


def ask_one(
    question: str,
    target: AskTarget,
    timeout: float = 60.0,
    register_fork: Optional[Callable[[str, bool], None]] = None,
) -> AskResult:
    """Ask one target and return the single result. Used by the MCP ask_agent tool."""
    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT")
    return _ask_one(target, question, plugin_root, timeout, register_fork)
