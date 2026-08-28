"""pi adapter (``@earendil-works/pi-coding-agent``).

pi is a second coding-agent CLI with a session model close enough to Claude
Code's that multiverse's orchestration transfers directly, and in three
places materially better:

* **Sessions are already trees.** Every entry carries ``id``/``parentId``, so
  branching is in-format rather than bolted on. Multiverse's forking and
  time-travel goals map onto it natively.
* **Extensions beat hooks.** A pi extension registers tools *and* subscribes
  to lifecycle events in one process, so the inter-agent tools and the status
  writer share a runtime instead of paying a process spawn per hook fire.
* **Messages can be pushed into a live agent.** ``pi.sendMessage(...,
  {triggerTurn: true})`` delivers into a running turn. Claude Code has no
  equivalent, which is why the Claude path resorts to tmux ``send-keys`` to
  wake an idle recipient; on pi that hack is unnecessary.

Session storage layout (mirrors pi's own resolution so we find exactly the
files pi writes):

    $PI_CODING_AGENT_SESSION_DIR                 (flat, if set — overrides all)
    ${PI_CODING_AGENT_DIR:-~/.pi/agent}/sessions/--<cwd, / -> ->--/
        <ISO-timestamp>_<uuid>.jsonl
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from .base import ForkPlan, Harness, TranscriptInfo

_META_CACHE: dict[str, tuple[float, dict]] = {}

#: Relative path of the pi extension that provides the multiverse tools.
PI_EXTENSION_REL = "pi-extension/index.ts"


def agent_dir() -> Path:
    """``~/.pi/agent`` unless ``PI_CODING_AGENT_DIR`` overrides it."""
    env = os.environ.get("PI_CODING_AGENT_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path(os.path.expanduser("~/.pi/agent"))


def encode_cwd(cwd: str) -> str:
    """pi's project-directory encoding: ``--`` + path (leading separator
    stripped, ``/``/``\\``/``:`` mapped to ``-``) + ``--``."""
    resolved = str(Path(cwd).expanduser())
    stripped = re.sub(r"^[/\\]", "", resolved)
    return f"--{re.sub(r'[/\\:]', '-', stripped)}--"


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
    return ""


class PiHarness(Harness):
    name = "pi"
    display = "pi"

    # -- process ----------------------------------------------------------

    def binary(self) -> str:
        return os.environ.get("MULTIVERSE_PI_BIN") or "pi"

    def _extension_args(self, plugin_root: Optional[str]) -> list[str]:
        if not plugin_root:
            return []
        ext = Path(plugin_root) / PI_EXTENSION_REL
        return ["--extension", str(ext)] if ext.exists() else []

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
        argv = [self.binary()] + self._extension_args(plugin_root)
        if name:
            argv += ["--name", name]
        if session_id:
            # --session resumes an existing id; --session-id creates the exact
            # id if it does not exist yet. Both keep multiverse in control of
            # the identifier, which is what the state dir is keyed on.
            argv += ["--session" if resume else "--session-id", session_id]
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
        """One-shot side question against a copy of the target's context.

        ``--fork`` is given the target's transcript *path* rather than its id:
        id lookup resolves against ``--session-dir``, which we repoint at a
        scratch directory so the forked transcript never lands in the
        workspace listing. The scratch dir is the only cleanup artifact, so a
        killed ask can't leave a phantom dormant agent behind — pi's analogue
        of the fork registry the Claude path needs.

        ``--no-extensions`` keeps the multiverse extension out of the fork, so
        its status writer and inbox delivery can't touch the caller's state
        (the same reasoning behind stripping ``MULTIVERSE_*`` from the Claude
        fork's environment).
        """
        scratch = Path(tempfile.mkdtemp(prefix=f"mv-ask-{fork_id[:8]}-"))
        source = self.transcript_path(target_session_id, cwd)
        argv = [
            self.binary(),
            "--fork", str(source),
            "--session-dir", str(scratch),
            "--no-extensions",
            "--no-tools",
            "--print",
            self.side_question_wrapper(question),
        ]
        return ForkPlan(argv=argv, artifacts=[scratch], register_ids=[fork_id])

    # -- transcripts ------------------------------------------------------

    def transcripts_dir(self, cwd: str) -> Path:
        env = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        if env:
            # Flat override: pi puts every session here regardless of cwd.
            return Path(os.path.expanduser(env))
        return agent_dir() / "sessions" / encode_cwd(cwd)

    def transcript_path(self, session_id: str, cwd: str) -> Path:
        """Resolve ``<timestamp>_<id>.jsonl``.

        pi prefixes each filename with the creation timestamp, so the id alone
        does not determine the path. Returns the constructed-but-absent path
        when nothing matches, which callers already treat as "no transcript".
        """
        d = self.transcripts_dir(cwd)
        if d.is_dir():
            for path in d.glob(f"*_{session_id}.jsonl"):
                return path
        return d / f"{session_id}.jsonl"

    def list_transcripts(self, cwd: str) -> list[TranscriptInfo]:
        cwd = (cwd or "").strip()
        if not cwd:
            return []
        d = self.transcripts_dir(cwd)
        if not d.is_dir():
            return []
        flat = bool(os.environ.get("PI_CODING_AGENT_SESSION_DIR"))
        target = str(Path(cwd).expanduser())
        out: list[TranscriptInfo] = []
        for path in d.glob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            meta = self.read_meta(path)
            # A flat session dir mixes workspaces; the header's cwd is the
            # only thing that scopes a row to this monitor's workspace.
            if flat and meta.get("cwd") and meta["cwd"] != target:
                continue
            sid = meta.get("session_id") or path.stem.split("_", 1)[-1]
            out.append(
                TranscriptInfo(
                    session_id=sid,
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
            return {"title": None, "cwd": None, "preview": "", "session_id": None}
        cached = _META_CACHE.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]

        title: Optional[str] = None
        cwd: Optional[str] = None
        session_id: Optional[str] = None
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
                    if typ == "session":
                        cwd = raw.get("cwd") or cwd
                        session_id = raw.get("id") or session_id
                    elif typ == "session_info":
                        # Last one wins: /name can be called repeatedly.
                        name = raw.get("name")
                        title = str(name) if name else None
                    elif typ == "message" and preview is None:
                        msg = raw.get("message") or {}
                        if msg.get("role") == "user":
                            text = _extract_text(msg.get("content")).strip()
                            if text and not text.startswith("<"):
                                text = text.replace("\n", " ")
                                if len(text) > 80:
                                    text = text[:79] + "…"
                                preview = text
        except OSError:
            pass

        result = {
            "title": title,
            "cwd": cwd,
            "session_id": session_id,
            "preview": preview or "(no user message)",
        }
        _META_CACHE[str(path)] = (mtime, result)
        return result

    def live_tokens(self, path: Path) -> int:
        """Context size from pi's own usage accounting.

        pi records ``usage`` on every assistant message, so the most recent
        one already knows exactly what the provider billed for that request's
        input — a better number than re-tokenizing the transcript, and O(1)
        after the first read. Compaction shows up in it naturally, since the
        request after a compaction carries the smaller input count.
        """
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return 0
        key = f"tokens:{path}"
        cached = _META_CACHE.get(key)
        if cached and cached[0] == mtime:
            return int(cached[1].get("tokens", 0))

        total = 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"usage"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = (row or {}).get("message")
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    n = (
                        int(usage.get("input", 0) or 0)
                        + int(usage.get("cacheRead", 0) or 0)
                        + int(usage.get("output", 0) or 0)
                    )
                    # A turn that errored (network failure, abort) is still
                    # persisted, with an all-zero usage record. Taking the
                    # last assistant message unconditionally would report 0
                    # for a session whose context is perfectly intact, so
                    # keep the most recent message that actually billed.
                    if n > 0:
                        total = n
        except OSError:
            return 0
        _META_CACHE[key] = (mtime, {"tokens": total})
        return total

    # -- compaction -------------------------------------------------------

    def parse_forest(self, path: str) -> Any:
        from compaction.pi_parser import parse_pi_session
        return parse_pi_session(path)

    def render_compacted(
        self,
        forest: Any,
        budget: int,
        *,
        session_id: str,
        cwd: str,
        source_path: str,
    ) -> str:
        from compaction.pi_render import compact_forest_to_pi_jsonl
        return compact_forest_to_pi_jsonl(
            forest, budget, session_id=session_id, cwd=cwd,
            source_path=source_path,
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def cleanup_artifacts(artifacts: list[Path]) -> None:
        for p in artifacts:
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
