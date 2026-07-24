"""Temporary-question fan-out across Multiverse agents.

Each target gets a one-off, tool-less response that does not persist in its
main conversation.

Claude targets use ``claude -p --resume <uuid> --fork-session --session-id
<fresh-uuid>``. Codex targets use the app-server protocol to ``thread/fork``
the existing thread with ``ephemeral: true``, then run exactly one
``turn/start``. Both paths leave the origin session untouched.

Cache behavior: for live sessions, the subprocess rebuilds
``systemPrompt``/``userContext``/``systemContext`` from scratch. If the build
is deterministic (no ``--agent``/``--system-prompt`` extras, same plugin
config), the rebuilt bytes match the live process's last request → prompt
cache hit on the big prefix. For dormant sessions the cache is cold anyway,
so rebuilding is equivalent to /btw's fallback path.

Tool denial: Claude uses ``--tools ""`` plus ``--max-turns 1``. Codex disables
tool-bearing features and integrations, selects a permission profile that
denies the filesystem and network, and treats any action item in the event
stream as a failed ask. The strict side-question prompt backs up both paths.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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
    # Kept as the third field for compatibility with older positional callers.
    claude_uuid: Optional[str] = None
    runtime: str = "claude"
    session_id: Optional[str] = None

    @property
    def native_session_id(self) -> Optional[str]:
        return self.session_id or self.claude_uuid


@dataclass
class AskResult:
    name: str
    response: Optional[str]
    error: Optional[str]
    fork_uuid: str
    elapsed: float


_CODEX_RUNTIME = "codex"
_CODEX_PERMISSION_PROFILE = "multiverse-side-question"
_CODEX_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "code_mode_host",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "shell_tool",
    "tool_suggest",
    "unified_exec",
)
_CODEX_NON_ACTION_ITEMS = {
    "userMessage",
    "hookPrompt",
    "agentMessage",
    "plan",
    "reasoning",
    "contextCompaction",
}
_CODEX_SIDE_QUESTION_INSTRUCTIONS = """You are an ephemeral side-question fork of an existing Codex conversation.
Answer the user's single question directly from the conversation context you already have.
Do not call tools, inspect files, search, execute commands, delegate, or take actions.
Do not promise future work. If the context does not contain the answer, say so.
Return one final answer and stop."""


class _CodexProtocolError(RuntimeError):
    pass


class _CodexToolAttempt(RuntimeError):
    pass


class _JsonLineProcess:
    """Small blocking JSON-line client around one Codex app-server process."""

    def __init__(self, cmd: list[str], cwd: str, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        if self.proc.stdin is None or self.proc.stdout is None:
            raise _CodexProtocolError("Codex app-server did not expose stdio")
        self._messages: queue.Queue[Optional[str]] = queue.Queue()
        self._stderr: list[str] = []
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self._messages.put(line)
        finally:
            self._messages.put(None)

    def _read_stderr(self) -> None:
        if self.proc.stderr is None:
            return
        for line in self.proc.stderr:
            if sum(map(len, self._stderr)) < 4000:
                self._stderr.append(line)

    def send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise _CodexProtocolError(self.failure_detail() or str(exc)) from exc

    def receive(self, deadline: float) -> dict:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                line = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError from exc
            if line is None:
                raise _CodexProtocolError(
                    self.failure_detail() or "Codex app-server exited unexpectedly"
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                return message

    def failure_detail(self) -> str:
        return "".join(self._stderr).strip()[-400:]

    def close(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(self.proc.pid, signal.SIGTERM)
                else:
                    self.proc.terminate()
                self.proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name == "posix":
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    else:
                        self.proc.kill()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        self._stdout_thread.join(timeout=0.2)
        self._stderr_thread.join(timeout=0.2)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _env_without_session_vars() -> dict[str, str]:
    """Return ``os.environ`` minus session-scoped ``MULTIVERSE_*`` vars.

    Why this matters: when ``ask_agent`` is called by a running multiverse
    agent, the MCP server (a subprocess of that agent's runtime) inherits
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


def _response_result(message: dict, request_id: int) -> dict:
    if message.get("id") != request_id:
        raise _CodexProtocolError(f"unexpected Codex response id: {message.get('id')}")
    error = message.get("error")
    if error:
        if isinstance(error, dict):
            detail = error.get("message") or json.dumps(error, ensure_ascii=False)
        else:
            detail = str(error)
        raise _CodexProtocolError(detail[:400])
    result = message.get("result")
    if not isinstance(result, dict):
        raise _CodexProtocolError("Codex app-server returned no result")
    return result


def _wait_for_response(
    client: _JsonLineProcess, request_id: int, deadline: float
) -> dict:
    while True:
        message = client.receive(deadline)
        if message.get("id") == request_id:
            return _response_result(message, request_id)


def _codex_app_server_command() -> list[str]:
    cmd = [
        os.environ.get("MULTIVERSE_CODEX_BIN") or "codex",
        "app-server",
        "--listen",
        "stdio://",
        "--config",
        'web_search="disabled"',
        "--config",
        "mcp_servers={}",
        "--config",
        "apps._default.enabled=false",
    ]
    for feature in _CODEX_DISABLED_FEATURES:
        cmd += ["--disable", feature]
    return cmd


def _codex_fork_params(target: AskTarget) -> dict:
    session_id = target.native_session_id
    if not session_id:
        raise _CodexProtocolError("target has no tracked Codex thread id")
    disabled_features = {name: False for name in _CODEX_DISABLED_FEATURES}
    return {
        "threadId": session_id,
        "cwd": target.cwd,
        "ephemeral": True,
        "excludeTurns": True,
        "approvalPolicy": "never",
        "permissions": _CODEX_PERMISSION_PROFILE,
        "baseInstructions": _CODEX_SIDE_QUESTION_INSTRUCTIONS,
        "developerInstructions": _CODEX_SIDE_QUESTION_INSTRUCTIONS,
        "runtimeWorkspaceRoots": [],
        "config": {
            "web_search": "disabled",
            "features": disabled_features,
            "mcp_servers": {},
            "apps": {"_default": {"enabled": False}},
            "permissions": {
                _CODEX_PERMISSION_PROFILE: {
                    "filesystem": {":root": "deny"},
                    "network": {"enabled": False},
                }
            },
        },
    }


def _record_codex_item(item: object, agent_messages: dict[str, dict]) -> None:
    if not isinstance(item, dict):
        return
    item_type = item.get("type")
    if item_type not in _CODEX_NON_ACTION_ITEMS:
        raise _CodexToolAttempt(
            f"Codex side question attempted disallowed action '{item_type or 'unknown'}'"
        )
    if item_type == "agentMessage":
        item_id = str(item.get("id") or len(agent_messages))
        agent_messages[item_id] = item


def _final_codex_message(agent_messages: dict[str, dict]) -> Optional[str]:
    messages = list(agent_messages.values())
    finals = [m for m in messages if m.get("phase") == "final_answer"]
    selected = finals[-1] if finals else (messages[-1] if messages else None)
    if selected is None:
        return None
    text = selected.get("text")
    return text.strip() if isinstance(text, str) and text.strip() else None


def _ask_one_codex(
    target: AskTarget,
    question: str,
    timeout: float,
) -> AskResult:
    start = time.monotonic()
    deadline = start + timeout
    fork_uuid = ""
    response: Optional[str] = None
    err: Optional[str] = None
    client: Optional[_JsonLineProcess] = None
    try:
        client = _JsonLineProcess(
            _codex_app_server_command(), target.cwd, _env_without_session_vars()
        )
        client.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "multiverse", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        _wait_for_response(client, 1, deadline)
        client.send({"method": "initialized"})
        client.send(
            {"id": 2, "method": "thread/fork", "params": _codex_fork_params(target)}
        )
        fork_result = _wait_for_response(client, 2, deadline)
        thread = fork_result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise _CodexProtocolError("Codex thread/fork returned no thread id")
        if thread.get("ephemeral") is not True:
            raise _CodexProtocolError("Codex thread/fork was not ephemeral")
        fork_uuid = thread["id"]

        client.send(
            {
                "id": 3,
                "method": "turn/start",
                "params": {
                    "threadId": fork_uuid,
                    "input": [
                        {
                            "type": "text",
                            "text": SIDE_QUESTION_WRAPPER + question,
                        }
                    ],
                    "approvalPolicy": "never",
                    "runtimeWorkspaceRoots": [],
                },
            }
        )

        turn_started = False
        completed_turn: Optional[dict] = None
        agent_messages: dict[str, dict] = {}
        while True:
            message = client.receive(deadline)
            if message.get("id") == 3:
                _response_result(message, 3)
                turn_started = True
            else:
                method = message.get("method")
                params = message.get("params")
                if not isinstance(params, dict) or params.get("threadId") != fork_uuid:
                    continue
                if method in ("item/started", "item/completed"):
                    _record_codex_item(params.get("item"), agent_messages)
                elif method == "turn/completed":
                    turn = params.get("turn")
                    if not isinstance(turn, dict):
                        raise _CodexProtocolError(
                            "Codex returned an invalid completed turn"
                        )
                    completed_turn = turn

            if not turn_started or completed_turn is None:
                continue
            for item in completed_turn.get("items") or []:
                _record_codex_item(item, agent_messages)
            if completed_turn.get("status") != "completed":
                turn_error = completed_turn.get("error")
                if isinstance(turn_error, dict):
                    detail = turn_error.get("message") or json.dumps(turn_error)
                else:
                    detail = str(turn_error or completed_turn.get("status") or "failed")
                raise _CodexProtocolError(f"Codex turn failed: {detail}"[:400])
            response = _final_codex_message(agent_messages)
            if response is None:
                err = "empty response"
            break
    except TimeoutError:
        err = f"timed out after {timeout:.0f}s"
    except FileNotFoundError:
        err = "codex CLI not found"
    except (_CodexProtocolError, _CodexToolAttempt) as exc:
        err = str(exc)[:400]
    except Exception as exc:  # noqa: BLE001 - surface transport failures
        err = f"subprocess error: {exc}"[:400]
    finally:
        if client is not None:
            client.close()

    return AskResult(
        name=target.name,
        response=response,
        error=err,
        fork_uuid=fork_uuid,
        elapsed=time.monotonic() - start,
    )


def _ask_one_claude(
    target: AskTarget,
    question: str,
    plugin_root: Optional[str],
    timeout: float,
    register_fork: Optional[Callable[[str, bool], None]],
) -> AskResult:
    fork_uuid = str(_uuid.uuid4())
    wrapped = SIDE_QUESTION_WRAPPER + question
    jsonl = CLAUDE_PROJECTS_DIR / _encode_cwd(target.cwd) / f"{fork_uuid}.jsonl"

    cmd = [os.environ.get("MULTIVERSE_CLAUDE_BIN") or "claude"]
    if plugin_root:
        cmd += ["--plugin-dir", plugin_root]
    # Ask forks resume the full origin session, so a large session can
    # overflow the default (200k) context window and fail with "Prompt is
    # too long". MULTIVERSE_ASK_MODEL (set in bin/multiverse.local) pins a
    # model — e.g. a 1M-context id — so the fork fits. Unset => no --model,
    # i.e. the session's own default model (unchanged behaviour).
    ask_model = os.environ.get("MULTIVERSE_ASK_MODEL")
    if ask_model:
        cmd += ["--model", ask_model]
    cmd += [
        "--resume",
        target.native_session_id,
        "--fork-session",
        "--session-id",
        fork_uuid,
        "--tools",
        "",
        "--max-turns",
        "1",
        "-p",
        wrapped,
    ]

    if register_fork:
        register_fork(fork_uuid, True)
    # Register in the on-disk fork registry BEFORE spawning. This is what
    # keeps the fork hidden from the dashboard / list_agents — and, unlike
    # the monitor's in-memory set, it survives a kill mid-ask (when the
    # `finally` below never runs). gc() reaps the orphan + this entry later.
    forks.mark_active(fork_uuid, jsonl)

    start = time.monotonic()
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
            # On failure the CLI often prints the real reason to stdout
            # (e.g. "Prompt is too long") with empty stderr — fall back to
            # stdout so the caller sees the cause, not a bare "claude
            # exited N".
            err = (r.stderr.strip() or r.stdout.strip() or f"claude exited {r.returncode}")[:400]
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
        elapsed=time.monotonic() - start,
    )


def _ask_one(
    target: AskTarget,
    question: str,
    plugin_root: Optional[str],
    timeout: float,
    register_fork: Optional[Callable[[str, bool], None]],
) -> AskResult:
    if target.runtime == _CODEX_RUNTIME:
        return _ask_one_codex(target, question, timeout)
    return _ask_one_claude(target, question, plugin_root, timeout, register_fork)


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
            ex.submit(_ask_one, t, question, plugin_root, timeout, register_fork): i
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
