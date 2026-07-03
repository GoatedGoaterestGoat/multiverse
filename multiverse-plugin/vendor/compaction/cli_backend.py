"""Claude CLI backend: invoke `claude -p` to make merge decisions.

Bypasses the Anthropic Python SDK and the need for an ANTHROPIC_API_KEY
by shelling out to the user's installed Claude Code CLI, which uses the
existing Claude subscription credentials (keychain OAuth).

The CLI is invoked with `--print --tools "" --no-session-persistence
--disable-slash-commands --setting-sources user --output-format json`,
which is enough isolation for our use case while still using the
keychain auth (`--bare` would force ANTHROPIC_API_KEY auth and is
therefore avoided). The model's response is asked to be a single JSON
object which we parse out of the CLI's `{"result": ...}` envelope.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

from .render import render_frontier_for_constructor
from .types import Frontier

SYSTEM_PROMPT = """\
You are constructing a summary tree of an agent trace, bottom-up.

You will be shown the current frontier (an ordered list of entries that
together cover one user-assistant turn). Your job is to pick exactly ONE
contiguous span [start, end) of frontier entries to collapse into a
single summary node.

Point of view (IMPORTANT):
  The trace you are summarizing is YOUR OWN past work as the agent.
  In the entries you see, "I"/"me"/"my" refers to you (the agent), and
  "you" refers to the user. Write summaries in the FIRST PERSON from
  the agent's perspective: say "I read the file", "I tried X then Y",
  NOT "the assistant read the file" or "Claude tried...". When the
  trace contains a user message, refer to that speaker as "the user"
  or "you" (paraphrasing what the user said TO you).

Decision rule:
  Pick the span such that the resulting frontier remains the most
  coherent possible summary of the entire trace at its new compression
  level.

Concretely:
  - Prefer spans that form a natural unit: a tool exchange and its
    immediate follow-up; a retry sequence; one attempt at one sub-task.
  - Prefer spans where the content is redundant or low-density.
  - AVOID making the frontier lopsided: do not keep collapsing one
    region while the rest is still raw. Distribute merges across the
    trace so every part is summarized at roughly the same level of
    detail (with denser content allowed to compress harder).
  - The summary text should read as a natural continuation of the trace
    at its compression level: factual, present-tense, FIRST-PERSON, no
    preamble. Preserve file paths, identifiers, exit codes, errors, and
    decisions verbatim where possible.

Output format (STRICT):
  Output exactly ONE JSON object on a single line, and NOTHING else.
  No preamble, no markdown fences, no trailing commentary.

  {"start": <int>, "end": <int>, "summary": "<text>"}

Constraints:
  - 0 <= start < end <= frontier_size
  - end - start >= 2 (a merge must collapse at least two adjacent entries)
"""

USER_PROMPT_TEMPLATE = """\
{frontier}

Respond with one JSON object: {{"start": <int>, "end": <int>, "summary": <str>}}
"""


@dataclass
class CLIBackendConfig:
    model: str = "sonnet"
    timeout_seconds: int = 60
    max_budget_usd: float | None = None
    extra_args: tuple[str, ...] = ()


def request_merge_via_cli(
    frontier: Frontier,
    config: CLIBackendConfig | None = None,
) -> tuple[int, int, str]:
    """Ask the Claude CLI to pick the next merge for this frontier.

    Returns (start, end, summary). Raises RuntimeError on parse / CLI failure.
    """
    cfg = config or CLIBackendConfig()

    rendered = render_frontier_for_constructor(frontier)
    user_prompt = USER_PROMPT_TEMPLATE.format(frontier=rendered)

    cmd = [
        os.environ.get("MULTIVERSE_CLAUDE_BIN") or "claude",
        "-p",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--setting-sources", "user",
        "--tools", "",
        "--max-turns", "1",
        "--output-format", "json",
        "--model", cfg.model,
        "--system-prompt", SYSTEM_PROMPT,
    ]
    if cfg.max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(cfg.max_budget_usd)]
    cmd += list(cfg.extra_args)

    proc = subprocess.run(
        cmd,
        input=user_prompt,
        capture_output=True,
        text=True,
        timeout=cfg.timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exit {proc.returncode}\n"
            f"stderr:\n{proc.stderr}\n"
            f"stdout:\n{proc.stdout}"
        )

    return _parse_response(proc.stdout)


def _parse_response(stdout: str) -> tuple[int, int, str]:
    """Pull the merge decision out of the CLI's JSON envelope."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"could not parse CLI envelope JSON: {e}\nstdout: {stdout[:500]}")

    text = envelope.get("result", "") if isinstance(envelope, dict) else ""
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"empty `result` in CLI envelope: {envelope}")

    obj = _extract_first_json_object(text)
    if obj is None:
        raise RuntimeError(f"no JSON object found in model output:\n{text[:500]}")

    try:
        start = int(obj["start"])
        end = int(obj["end"])
        summary = str(obj["summary"])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"merge JSON missing fields: {e}\nobject: {obj}")

    return start, end, summary


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_first_json_object(text: str) -> dict | None:
    """Find the first top-level JSON object in `text` and parse it.

    Tolerates: optional ```json fences, leading/trailing prose, multiple
    JSON objects (returns the first that parses).
    """
    # Try fenced first
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Fall back to brace-balanced extraction starting at the first '{'
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
        start = text.find("{", start + 1)
    return None
