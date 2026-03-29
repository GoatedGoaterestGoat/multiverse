"""Types for Claude Code session JSONL entries and fold operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class JsonlEntry:
    """A single line from a Claude Code session JSONL file."""

    line_index: int  # position in the JSONL file
    raw: dict[str, Any]  # the full parsed JSON object

    @property
    def type(self) -> str:
        return self.raw.get("type", "")

    @property
    def uuid(self) -> str:
        return self.raw.get("uuid", self.raw.get("messageId", ""))

    @property
    def parent_uuid(self) -> str | None:
        return self.raw.get("parentUuid")

    @property
    def session_id(self) -> str:
        return self.raw.get("sessionId", "")

    @property
    def timestamp(self) -> str:
        return self.raw.get("timestamp", "")

    @property
    def request_id(self) -> str | None:
        return self.raw.get("requestId")

    @property
    def is_sidechain(self) -> bool:
        return self.raw.get("isSidechain", False)

    @property
    def message(self) -> dict[str, Any] | None:
        return self.raw.get("message")

    @property
    def role(self) -> str | None:
        msg = self.message
        return msg.get("role") if msg else None

    @property
    def content(self) -> Any:
        msg = self.message
        return msg.get("content") if msg else None

    @property
    def is_conversation_message(self) -> bool:
        """True if this is a user or assistant message (not metadata)."""
        return self.type in ("user", "assistant") and self.message is not None

    @property
    def content_types(self) -> list[str]:
        """Return types of content blocks if content is a list."""
        c = self.content
        if isinstance(c, list):
            return [block.get("type", "unknown") for block in c if isinstance(block, dict)]
        return []

    @property
    def is_tool_use(self) -> bool:
        return "tool_use" in self.content_types

    @property
    def is_tool_result(self) -> bool:
        return "tool_result" in self.content_types

    @property
    def tool_use_ids(self) -> list[str]:
        """Extract tool_use IDs from this entry's content."""
        c = self.content
        if not isinstance(c, list):
            return []
        ids = []
        for block in c:
            if isinstance(block, dict):
                if block.get("type") == "tool_use" and "id" in block:
                    ids.append(block["id"])
                elif block.get("type") == "tool_result" and "tool_use_id" in block:
                    ids.append(block["tool_use_id"])
        return ids

    @property
    def tool_names(self) -> list[str]:
        """Extract tool names from tool_use blocks."""
        c = self.content
        if not isinstance(c, list):
            return []
        return [
            block["name"]
            for block in c
            if isinstance(block, dict) and block.get("type") == "tool_use" and "name" in block
        ]

    def char_count(self) -> int:
        """Rough character count of content."""
        c = self.content
        if isinstance(c, str):
            return len(c)
        if isinstance(c, list):
            total = 0
            for block in c:
                if isinstance(block, dict):
                    for key in ("text", "thinking", "content"):
                        val = block.get(key)
                        if isinstance(val, str):
                            total += len(val)
            return total
        return 0


def estimate_tokens(char_count: int) -> int:
    """Rough token estimate: ~4 chars per token."""
    return char_count // 4


@dataclass
class ConversationIndex:
    """A conversation message with its index for context_info display."""

    index: int
    entry: JsonlEntry

    def format_line(self) -> str:
        """Format as a single line for the table of contents."""
        e = self.entry
        chars = e.char_count()
        tokens = estimate_tokens(chars)
        size_str = f"({chars} chars, ~{tokens} tok)"

        if e.role == "user":
            if e.is_tool_result:
                names = e.tool_use_ids
                preview = _preview_content(e.content)
                large = " <- large" if chars > 5000 else ""
                return f"[{self.index}]  user: [tool_result] {preview} {size_str}{large}"
            else:
                preview = _preview_text(e.content, 60)
                return f"[{self.index}]  user: {preview} {size_str}"

        elif e.role == "assistant":
            ctypes = e.content_types
            if "thinking" in ctypes:
                return f"[{self.index}]  assistant: [thinking] {size_str}"
            elif "tool_use" in ctypes:
                names = e.tool_names
                tool_str = ", ".join(names)
                input_preview = _preview_tool_input(e.content)
                return f"[{self.index}]  assistant: [tool_use: {tool_str}] {input_preview}"
            elif "text" in ctypes:
                preview = _preview_text_blocks(e.content, 60)
                return f"[{self.index}]  assistant: [text] {preview} {size_str}"
            else:
                return f"[{self.index}]  assistant: [{', '.join(ctypes)}] {size_str}"

        return f"[{self.index}]  {e.type}: {size_str}"


def _preview_text(content: Any, max_len: int) -> str:
    if isinstance(content, str):
        text = content.replace("\n", " ").strip()
        if len(text) > max_len:
            return f'"{text[:max_len]}..."'
        return f'"{text}"'
    return ""


def _preview_text_blocks(content: Any, max_len: int) -> str:
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "").replace("\n", " ").strip()
            if len(text) > max_len:
                return f'"{text[:max_len]}..."'
            return f'"{text}"'
    return ""


def _preview_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            c = block.get("content", "")
            if isinstance(c, str):
                short = c.replace("\n", " ").strip()[:40]
                return f'"{short}"' if short else ""
    return ""


def _preview_tool_input(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            inp = block.get("input", {})
            if isinstance(inp, dict):
                # Show the most identifying field
                for key in ("command", "pattern", "file_path", "prompt", "description"):
                    if key in inp:
                        val = str(inp[key])[:60]
                        return val
    return ""
