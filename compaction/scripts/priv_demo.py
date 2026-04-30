"""Privileged-constructor demo via CLI (subscription auth).

Constructs a synthetic Claude Code session containing only one turn from the
source JSONL — preserving thinking blocks (and their signatures) verbatim —
then runs `claude --resume` against it with the merge instruction as the
next user prompt. The model receives the original conversation as native
messages history, so thinking signatures validate server-side and the
constructor reads the actual chain-of-thought when picking a merge.

This demo runs ONE merge call (the first merge for the chosen turn) and
prints the result, so we can verify the auth/signature path works before
investing in a multi-step constructor.

Usage:
    PYTHONPATH=src python3 scripts/priv_demo.py SESSION.jsonl TURN_IDX [--model NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid as uuidlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from compaction.constructor import (
    PRIV_USER_INSTRUCTION_TEMPLATE,
    SYSTEM_PROMPT,
    _render_priv_frontier_index,
)
from compaction.parser import parse_session
from compaction.preprocess import PreprocessConfig, preprocess_forest


def cwd_to_slug(cwd: str) -> str:
    """Claude Code's project-slug rule: replace `/` with `-`."""
    return cwd.replace("/", "-")


def build_synthetic_jsonl(source_path: str, turn_idx: int, new_session_id: str,
                           new_cwd: str) -> str:
    """Extract turn N's rows from the source JSONL into a fresh session
    JSONL. Patches sessionId, cwd, and parentUuid chain so Claude Code can
    load it as a normal resumable session."""
    forest = parse_session(source_path)
    turn = forest[turn_idx]

    # Collect source UUIDs that belong to this turn (prompt + all leaves).
    target_uuids: set[str] = set()
    if turn.user_prompt:
        target_uuids.update(turn.user_prompt.source_uuids)
    for leaf in turn.leaves:
        target_uuids.update(leaf.source_uuids)

    # Walk the source JSONL and extract matching rows in original order.
    rows: list[dict] = []
    with open(source_path) as f:
        for line in f:
            d = json.loads(line)
            if d.get("uuid") in target_uuids:
                rows.append(d)

    # Rewire the chain: first row's parentUuid -> null, subsequent rows
    # point to predecessor. Update sessionId and cwd on every row.
    prev_uuid: str | None = None
    for d in rows:
        d["sessionId"] = new_session_id
        d["cwd"] = new_cwd
        d["parentUuid"] = prev_uuid
        prev_uuid = d["uuid"]

    # Prepend a minimal metadata row so /resume picks up a friendly title.
    out_lines = [
        json.dumps({
            "type": "custom-title",
            "customTitle": f"priv-demo-turn{turn_idx}",
            "sessionId": new_session_id,
        }),
    ]
    out_lines.extend(json.dumps(d) for d in rows)
    return "\n".join(out_lines) + "\n"


def run_priv_merge(source: str, turn_idx: int, model: str = "sonnet") -> dict:
    """Run one privileged-mode merge call. Returns a dict with start, end,
    summary, and metadata."""
    forest = preprocess_forest(parse_session(source), PreprocessConfig())
    turn = forest[turn_idx]
    frontier = list(turn.leaves)

    # Synthetic session in the user's projects dir for the current cwd.
    cwd = os.getcwd()
    slug = cwd_to_slug(cwd)
    session_id = str(uuidlib.uuid4())
    projects_dir = Path.home() / ".claude" / "projects" / slug
    projects_dir.mkdir(parents=True, exist_ok=True)
    session_path = projects_dir / f"{session_id}.jsonl"

    print(f"[priv_demo] writing synthetic session to: {session_path}",
          file=sys.stderr)
    session_path.write_text(build_synthetic_jsonl(source, turn_idx, session_id, cwd))

    instruction = PRIV_USER_INSTRUCTION_TEMPLATE.format(
        n_entries=len(frontier),
        frontier_index=_render_priv_frontier_index(frontier),
    )
    instruction += (
        "\n\nOutput format (STRICT): a JSON object on a single line:\n"
        '{"start": <int>, "end": <int>, "summary": "<text>"}'
    )

    cmd = [
        "claude",
        "-p", instruction,
        "--resume", session_id,
        "--no-session-persistence",
        "--disable-slash-commands",
        "--setting-sources", "user",
        "--tools", "",
        "--max-turns", "1",
        "--output-format", "json",
        "--model", model,
        "--system-prompt", SYSTEM_PROMPT,
    ]

    print(f"[priv_demo] running: claude -p ... --resume {session_id} ...",
          file=sys.stderr)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    finally:
        # Clean up the synthetic session.
        try:
            session_path.unlink()
        except OSError:
            pass

    if proc.returncode != 0:
        return {
            "ok": False,
            "returncode": proc.returncode,
            "stderr": proc.stderr,
            "stdout": proc.stdout[:2000],
        }

    # Parse the CLI's outer envelope.
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "error": f"could not parse CLI envelope: {e}",
            "stdout": proc.stdout[:2000],
        }

    text = envelope.get("result", "") if isinstance(envelope, dict) else ""
    obj = _extract_first_json_object(text)
    if obj is None:
        return {
            "ok": False,
            "error": "no JSON object in model output",
            "result_text": text[:2000],
            "envelope_keys": list(envelope.keys()) if isinstance(envelope, dict) else None,
            "usage": envelope.get("usage") if isinstance(envelope, dict) else None,
        }

    return {
        "ok": True,
        "start": int(obj["start"]),
        "end": int(obj["end"]),
        "summary": str(obj["summary"]),
        "usage": envelope.get("usage"),
    }


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_first_json_object(text: str) -> dict | None:
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
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
                        candidate = text[start: i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
        start = text.find("{", start + 1)
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("session", help="path to source JSONL")
    p.add_argument("turn_idx", type=int, help="turn index to merge in")
    p.add_argument("--model", default="sonnet")
    args = p.parse_args()

    result = run_priv_merge(args.session, args.turn_idx, model=args.model)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
