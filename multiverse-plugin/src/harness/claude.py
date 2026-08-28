"""Claude Code adapter.

Wraps the behavior multiverse has always had. Nothing here is new logic —
the transcript reader, the fork argv, and the compaction entry points are
lifted verbatim from ``src/sessions.py``, ``src/ask_agents.py``, and
``src/compact_runner.py`` so that routing Claude Code through the adapter
is a no-op at runtime.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from ..paths import claude_projects_dir
from .base import ForkPlan, Harness, TranscriptInfo, encode_cwd

# Suffix build_dashboard_rows writes onto ghost-row display names. Stripped on
# read so a title corrupted by a previous ghost-row resume still resolves.
_FOLD_SUFFIX_RE = re.compile(r" \(folded [^)]+\)$")

# mtime-keyed cache of parsed transcript metadata; the dashboard reads every
# row every tick, so re-parsing unchanged files would dominate refresh cost.
_META_CACHE: dict[str, tuple[float, dict]] = {}


def strip_fold_suffix(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    while True:
        new = _FOLD_SUFFIX_RE.sub("", name)
        if new == name:
            return name
        name = new


def _extract_user_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
    return ""


class ClaudeHarness(Harness):
    name = "claude"
    display = "claude"

    # -- process ----------------------------------------------------------

    def binary(self) -> str:
        return os.environ.get("MULTIVERSE_CLAUDE_BIN") or "claude"

    def launch_argv(
        self,
        *,
        session_id: Optional[str],
        resume: bool,
        name: Optional[str],
        plugin_root: Optional[str],
        extra_args: list[str],
        prompt: Optional[str],
    ) -> list[str]:
        argv = [self.binary()]
        if plugin_root:
            argv += ["--plugin-dir", plugin_root]
        if name:
            argv += ["--name", name]
        if resume and session_id:
            argv += ["--resume", session_id]
        elif session_id:
            argv += ["--session-id", session_id]
        argv += extra_args
        if prompt:
            argv.append(prompt)
        return argv

    def fork_plan(
        self,
        *,
        target_session_id: str,
        cwd: str,
        fork_id: str,
        question: str,
        plugin_root: Optional[str],
    ) -> ForkPlan:
        argv = [self.binary()]
        if plugin_root:
            argv += ["--plugin-dir", plugin_root]
        argv += [
            "--resume", target_session_id,
            "--fork-session",
            "--session-id", fork_id,
            "--tools", "",
            "--max-turns", "1",
            "-p", self.side_question_wrapper(question),
        ]
        return ForkPlan(
            argv=argv,
            artifacts=[self.transcript_path(fork_id, cwd)],
            register_ids=[fork_id],
        )

    # -- streaming side questions ----------------------------------------

    def stream_fork_argv(self, argv: list[str]) -> Optional[list[str]]:
        """Claude Code streams only under ``stream-json``.

        Plain ``-p`` buffers the whole answer and flushes at the end (measured:
        every line of a 12-line answer arrived in the same millisecond), so the
        format switch is what actually buys incremental output, not the way we
        read the pipe. ``--verbose`` is required alongside it, and
        ``--include-partial-messages`` is what turns full-message events into
        token-level deltas.
        """
        return argv + [
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]

    def parse_stream_line(self, line: str) -> Optional[str]:
        """Pull text out of one NDJSON event; ignore everything else."""
        line = line.strip()
        if not line:
            return None
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(ev, dict) or ev.get("type") != "stream_event":
            return None
        inner = ev.get("event")
        if not isinstance(inner, dict):
            return None
        if inner.get("type") != "content_block_delta":
            return None
        delta = inner.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return None
        text = delta.get("text")
        return text if isinstance(text, str) and text else None

    # -- transcripts ------------------------------------------------------

    def transcripts_dir(self, cwd: str) -> Path:
        return claude_projects_dir() / encode_cwd(cwd)

    def transcript_path(self, session_id: str, cwd: str) -> Path:
        return self.transcripts_dir(cwd) / f"{session_id}.jsonl"

    def list_transcripts(self, cwd: str) -> list[TranscriptInfo]:
        cwd = (cwd or "").strip()
        if not cwd:
            return []
        d = self.transcripts_dir(cwd)
        if not d.is_dir():
            return []
        out: list[TranscriptInfo] = []
        for path in d.glob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            meta = self.read_meta(path)
            out.append(
                TranscriptInfo(
                    session_id=path.stem,
                    path=path,
                    mtime=st.st_mtime,
                    size=st.st_size,
                    name=meta.get("title"),
                    preview=meta.get("preview") or "(no user message)",
                )
            )
        out.sort(key=lambda t: t.mtime, reverse=True)
        return out

    def read_meta(self, path: Path) -> dict[str, Any]:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {"title": None, "cwd": None, "preview": ""}
        cached = _META_CACHE.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]

        title: Optional[str] = None
        cwd: Optional[str] = None
        preview: Optional[str] = None
        try:
            with open(path, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    typ = raw.get("type")
                    if typ == "custom-title":
                        ct = raw.get("customTitle")
                        if ct:
                            title = strip_fold_suffix(str(ct))
                    if cwd is None and raw.get("cwd"):
                        cwd = str(raw["cwd"])
                    if preview is None and typ == "user":
                        msg = raw.get("message") or {}
                        if msg.get("role") == "user":
                            text = _extract_user_text(msg.get("content")).strip()
                            if text and not text.startswith("<"):
                                text = text.replace("\n", " ")
                                if len(text) > 80:
                                    text = text[:79] + "…"
                                preview = text
        except OSError:
            pass

        result = {"title": title, "cwd": cwd, "preview": preview or "(no user message)"}
        _META_CACHE[str(path)] = (mtime, result)
        return result

    # -- compaction -------------------------------------------------------

    def parse_forest(self, path: str) -> Any:
        from compaction.parser import parse_session
        return parse_session(path)

    def render_compacted(
        self,
        forest: Any,
        budget: int,
        *,
        session_id: str,
        cwd: str,
        source_path: str,
    ) -> str:
        from compaction.priv_session import compact_forest_to_session_jsonl
        return compact_forest_to_session_jsonl(
            forest, budget, session_id=session_id, cwd=cwd
        )
