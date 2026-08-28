"""Temporary-question fan-out across multiverse agents.

Each target gets a one-off, tool-less response that does not persist in its
main conversation — claude-code's ``/btw`` side-question, generalized.

The mechanism is the same on every harness and the harness adapter supplies
the specifics (see ``src/harness/``): fork the target's session into a
throwaway copy, deny all tools, run one turn in print mode, read the answer
off stdout, delete the copy. Claude Code does this with ``--fork-session
--session-id <fresh> --tools "" --max-turns 1``; pi with ``--fork <path>
--session-dir <scratch> --no-tools --print``. Either way the target's own
process is never involved, so an ask works whether it is idle, mid-turn, or
dormant.

Cache behavior: for a live target the fork rebuilds the system prompt from
scratch. When that build is deterministic the rebuilt bytes match the live
process's last request, so the big prefix is a prompt-cache hit. For dormant
targets the cache is cold either way.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import forks
from .harness import get_harness
from .harness.base import SIDE_QUESTION_WRAPPER  # re-exported for callers


@dataclass
class AskTarget:
    name: str
    cwd: str
    claude_uuid: str
    #: which harness owns this session. Defaults to ``claude`` so existing
    #: callers that predate multi-harness support keep working unchanged.
    harness: str = "claude"


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

    harness = get_harness(target.harness)
    fork_uuid = str(_uuid.uuid4())
    plan = harness.fork_plan(
        target_session_id=target.claude_uuid,
        cwd=target.cwd,
        fork_id=fork_uuid,
        question=question,
        plugin_root=plugin_root,
    )

    for sid in plan.register_ids:
        if register_fork:
            register_fork(sid, True)
        # Register in the on-disk fork registry BEFORE spawning. This is what
        # keeps the fork hidden from the dashboard / list_agents — and, unlike
        # the monitor's in-memory set, it survives a kill mid-ask (when the
        # `finally` below never runs). gc() reaps the orphan + entry later.
        forks.mark_active(sid, plan.artifacts[0] if plan.artifacts else Path("."))

    start = time.time()
    err: Optional[str] = None
    response: Optional[str] = None
    try:
        r = subprocess.run(
            plan.argv,
            cwd=target.cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env_without_session_vars(),
        )
        if r.returncode != 0:
            err = (r.stderr.strip() or f"{plan.argv[0]} exited {r.returncode}")[:400]
        else:
            response = r.stdout.strip() or None
            if not response:
                err = "empty response"
    except subprocess.TimeoutExpired:
        err = f"timed out after {timeout:.0f}s"
    except FileNotFoundError:
        err = f"{plan.argv[0]} CLI not found"
    except Exception as e:  # noqa: BLE001 — surface anything else as error text
        err = f"subprocess error: {e}"
    finally:
        _cleanup(plan.artifacts)
        for sid in plan.register_ids:
            forks.mark_done(sid)
            if register_fork:
                register_fork(sid, False)

    return AskResult(
        name=target.name,
        response=response,
        error=err,
        fork_uuid=fork_uuid,
        elapsed=time.time() - start,
    )


def _ask_one_streaming(
    target: AskTarget,
    question: str,
    plugin_root: Optional[str],
    timeout: float,
    idle_timeout: float,
    register_fork: Optional[Callable[[str, bool], None]],
    on_delta: Callable[[str], None],
    cancel: Optional[threading.Event],
) -> AskResult:
    """Like ``_ask_one`` but emits text through ``on_delta`` as it arrives.

    Falls back to the blocking path when the harness reports no streaming
    spelling, so an adapter that has not implemented it keeps working — the
    caller just gets one delta at the end instead of many.

    The fork scaffolding (register → run → cleanup → mark_done) is duplicated
    from ``_ask_one`` rather than factored out, deliberately: ``_ask_one`` is on
    the monitor's fan-out path and the MCP tool's path, and a refactor there to
    serve one new caller trades a working code path for a tidier one.
    """
    import time

    harness = get_harness(target.harness)
    fork_uuid = str(_uuid.uuid4())
    plan = harness.fork_plan(
        target_session_id=target.claude_uuid,
        cwd=target.cwd,
        fork_id=fork_uuid,
        question=question,
        plugin_root=plugin_root,
    )
    if cancel is not None and cancel.is_set():
        return AskResult(target.name, None, "aborted", fork_uuid, 0.0)

    stream_argv = harness.stream_fork_argv(plan.argv)
    if stream_argv is None:
        # No streaming to measure gaps in, so the two budgets collapse back
        # into one wall-clock cap for the blocking call.
        result = _ask_one(
            target, question, plugin_root, timeout + idle_timeout, register_fork
        )
        if result.response:
            on_delta(result.response)
        return result

    for sid in plan.register_ids:
        if register_fork:
            register_fork(sid, True)
        forks.mark_active(sid, plan.artifacts[0] if plan.artifacts else Path("."))

    start = time.time()
    err: Optional[str] = None
    parts: list[str] = []
    proc: Optional[subprocess.Popen] = None
    timed_out = threading.Event()
    stalled = threading.Event()
    aborted = threading.Event()
    #: Mutable box so the watchdog thread can see the read loop's progress.
    last_delta: list[Optional[float]] = [None]
    try:
        proc = subprocess.Popen(
            stream_argv,
            cwd=target.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_env_without_session_vars(),
        )

        # A streaming read has no subprocess.run-style timeout, and a blocking
        # read on the pipe cannot notice an abort, so both the deadline and the
        # caller's cancel are enforced the same way: kill the child and let the
        # read loop end on EOF. Polled rather than event-driven because it has
        # to watch two conditions at once.
        def _watch() -> None:
            # Two budgets, not one. `timeout` asks "is anything happening at
            # all" and covers prefill, which scales with the target's
            # transcript: measured 4.6s warm to 15.0s cold on one 9.7MB
            # session. `idle_timeout` asks "has it stalled" and only applies
            # once text is flowing. A single wall-clock cap gets this wrong in
            # both directions — it kills a healthy long answer, and it has to
            # be set so high to avoid that it stops catching real hangs.
            while not finished.wait(0.05):
                if cancel is not None and cancel.is_set():
                    aborted.set()
                    break
                now = time.time()
                last = last_delta[0]
                if last is None:
                    if now - start >= timeout:
                        timed_out.set()
                        break
                elif now - last >= idle_timeout:
                    stalled.set()
                    break
            else:
                return
            try:
                proc.kill()
            except OSError:
                pass

        finished = threading.Event()
        watchdog = threading.Thread(target=_watch, daemon=True)
        watchdog.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                text = harness.parse_stream_line(line)
                if text:
                    last_delta[0] = time.time()
                    parts.append(text)
                    on_delta(text)
            proc.wait()
        finally:
            finished.set()

        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        if aborted.is_set():
            # Partial text is still an answer — the user chose to stop reading,
            # so what already arrived is kept and only a silent abort is an error.
            err = None if parts else "aborted"
        elif timed_out.is_set():
            err = (
                f"no response in {timeout:.0f}s — the target's transcript may "
                f"be large enough that rebuilding it exceeds the budget "
                f"(raise MULTIVERSE_ASK_TIMEOUT)"
            )
        elif stalled.is_set():
            err = None if parts else f"stream stalled for {idle_timeout:.0f}s"
        elif proc.returncode != 0 and not parts:
            err = (stderr.strip() or f"{stream_argv[0]} exited {proc.returncode}")[:400]
        elif not parts:
            err = "empty response"
    except FileNotFoundError:
        err = f"{stream_argv[0]} CLI not found"
    except Exception as e:  # noqa: BLE001 — surface anything else as error text
        err = f"subprocess error: {e}"
    finally:
        _cleanup(plan.artifacts)
        for sid in plan.register_ids:
            forks.mark_done(sid)
            if register_fork:
                register_fork(sid, False)

    response = "".join(parts).strip() or None
    return AskResult(
        name=target.name,
        response=response,
        error=err if not response else None,
        fork_uuid=fork_uuid,
        elapsed=time.time() - start,
    )


def ask_one_streaming(
    question: str,
    target: AskTarget,
    timeout: float = 90.0,
    idle_timeout: float = 30.0,
    register_fork: Optional[Callable[[str, bool], None]] = None,
    on_delta: Optional[Callable[[str], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> AskResult:
    """Ask one target, streaming text to ``on_delta`` as it is generated.

    Used by the quick-ask popup, where time-to-first-word is what the wait
    feels like. ``ask_all`` deliberately does not use this: several answers
    interleaving into one pane would be unreadable.

    ``timeout`` bounds time-to-first-token only; ``idle_timeout`` bounds the
    gap between tokens after that. A long answer is therefore never cut off for
    being long — only for going quiet. Setting ``cancel`` kills the fork and
    returns whatever text had already arrived. The fork is cleaned up on this
    path exactly as on the others.
    """
    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT")
    return _ask_one_streaming(
        target, question, plugin_root, timeout, idle_timeout, register_fork,
        on_delta or (lambda _t: None), cancel,
    )


def _cleanup(artifacts: list[Path]) -> None:
    """Remove a fork's leftovers. Harnesses differ in what they leave behind:
    Claude Code writes one transcript file we name up front; pi is pointed at
    a scratch session dir, so the whole directory goes."""
    import shutil

    for path in artifacts:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


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
