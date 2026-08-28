"""Harness-agnostic interface.

Multiverse orchestrates *agent harnesses* — CLI coding agents that keep a
per-session transcript on disk and can be resumed from it. The monitor, the
inter-agent tools, and the compaction pipeline were all originally written
against Claude Code directly. This package factors out everything that is
actually harness-specific into a small adapter interface so a second harness
(``pi``) can be plugged in without forking the orchestration layer.

What a harness has to provide splits cleanly into four areas:

1. **Transcript discovery** — where sessions live on disk, how to list the
   ones belonging to a workspace, and how to pull a display name / preview
   out of one. Drives the dashboard and ``list_agents``.
2. **Process launch** — the argv for starting a fresh session, resuming an
   existing one, and loading the multiverse integration (plugin dir for
   Claude Code, ``--extension`` for pi).
3. **Ephemeral fork** — the argv for a one-shot, tool-less, non-persisting
   question against a copy of a session's context. Backs ``ask_agent``.
4. **Compaction I/O** — parsing a transcript into the harness-neutral
   ``compaction.types.Forest``, and writing a compacted forest back into a
   resumable transcript.

Everything else — the inbox, spawn-request brokering, status files, the
compaction *algorithm* itself — is already harness-neutral and lives
unchanged in ``src/`` and ``vendor/compaction/``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


def encode_cwd(cwd: str) -> str:
    """Claude Code's project-directory encoding: non-alphanumerics to ``-``.

    pi uses its own scheme (see ``PiHarness``); this helper is shared by the
    Claude adapter and by the inbox, which keys on cwd independently of any
    harness.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


@dataclass(frozen=True)
class TranscriptInfo:
    """One on-disk session transcript, as the dashboard sees it."""

    session_id: str
    path: Path
    mtime: float
    size: int
    name: Optional[str]      # user-assigned display name, if the harness has one
    preview: str             # first real user message, truncated


@dataclass(frozen=True)
class ForkPlan:
    """How to run one ephemeral side-question against a session.

    ``argv`` is the command; ``artifacts`` are paths the caller must remove
    afterwards (the fork's own transcript, a scratch session dir, …) so a
    side-question never leaves a phantom session behind. ``register_ids`` are
    session ids to hide from the dashboard for the duration of the call.
    """

    argv: list[str]
    artifacts: list[Path]
    register_ids: list[str]


class Harness(ABC):
    """Adapter for one agent CLI."""

    #: short stable key, stored in each session's ``meta.json``
    name: str = ""
    #: human-readable label for the dashboard's Harness column
    display: str = ""

    # -- process ----------------------------------------------------------

    @abstractmethod
    def binary(self) -> str:
        """Executable to invoke, honoring any per-harness override env var."""

    @abstractmethod
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
        """Full argv for an interactive session.

        ``resume`` picks up ``session_id``'s existing transcript; otherwise
        ``session_id`` (when given) is the id to create. ``prompt`` is an
        auto-submitted first user message. ``plugin_root`` is the multiverse
        plugin directory, from which the harness-specific integration
        (Claude Code plugin / pi extension) is loaded.
        """

    @abstractmethod
    def fork_plan(
        self,
        *,
        target_session_id: str,
        cwd: str,
        fork_id: str,
        question: str,
        plugin_root: Optional[str],
    ) -> ForkPlan:
        """Build the one-shot, tool-less side-question invocation."""

    # -- streaming side questions ----------------------------------------
    #
    # Optional. A harness that can emit incremental output overrides both of
    # these; the default pair means "no streaming", and callers fall back to
    # the blocking path. Keeping it opt-in means a harness without a streaming
    # spelling keeps working untouched rather than breaking on a new abstract.

    def stream_fork_argv(self, argv: list[str]) -> Optional[list[str]]:
        """Rewrite a fork argv to emit incremental output, or None if unsupported."""
        return None

    def parse_stream_line(self, line: str) -> Optional[str]:
        """Extract the text delta from one line of streamed output.

        Returns None for lines that carry no user-visible text (metadata,
        lifecycle events, unparseable noise).
        """
        return None

    def side_question_wrapper(self, question: str) -> str:
        """Framing prepended to a side question. Shared default; a harness
        may override if its system prompt needs different phrasing."""
        return SIDE_QUESTION_WRAPPER + question

    # -- transcripts ------------------------------------------------------

    @abstractmethod
    def transcripts_dir(self, cwd: str) -> Path:
        """Directory holding this workspace's transcripts."""

    @abstractmethod
    def transcript_path(self, session_id: str, cwd: str) -> Path:
        """Path of one session's transcript. May not exist yet."""

    @abstractmethod
    def list_transcripts(self, cwd: str) -> list[TranscriptInfo]:
        """All transcripts for ``cwd``, newest first."""

    @abstractmethod
    def read_meta(self, path: Path) -> dict[str, Any]:
        """``{"title": str|None, "cwd": str|None, "preview": str}`` for one
        transcript. Implementations should cache on mtime — the dashboard
        calls this for every row on every refresh tick."""

    def session_name(self, path: Path) -> Optional[str]:
        return self.read_meta(path).get("title")

    def live_tokens(self, path: Path) -> int:
        """Approximate context size of a transcript, for the dashboard.

        Called once per row per refresh tick, so an implementation must be
        cheap and mtime-cached — never a full parse of a multi-megabyte
        transcript. Returning 0 means "unknown"; the dashboard renders a dash.
        """
        return 0

    # -- compaction -------------------------------------------------------

    @abstractmethod
    def parse_forest(self, path: str) -> Any:
        """Transcript → ``compaction.types.Forest``."""

    @abstractmethod
    def render_compacted(
        self,
        forest: Any,
        budget: int,
        *,
        session_id: str,
        cwd: str,
        source_path: str,
    ) -> str:
        """Compacted forest → the full text of a resumable transcript."""

    def supports_compaction(self) -> bool:
        return True


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
