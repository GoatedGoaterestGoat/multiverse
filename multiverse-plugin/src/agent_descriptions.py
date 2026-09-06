"""Short, self-maintained agent descriptions, shared by every runtime.

Descriptions belong to a workspace/name, like inboxes and folder membership,
not to an ephemeral wrapper ID. They survive teardown, resume and compaction.
Fresh creation explicitly clears any stale description for a reused name.
Separate atomic files avoid read/modify/write races between different agents.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

from .atomic_files import atomic_write_json
from .paths import multiverse_dir


MAX_DESCRIPTION_CHARS = 60


def description_path(cwd: str, name: str, *, root: Path | None = None) -> Path:
    # Hash both components: punctuation in paths must not alias workspaces,
    # and an agent name must never be interpreted as a relative file path.
    workspace = str(Path(cwd).expanduser().resolve())
    workspace_key = hashlib.sha256(workspace.encode()).hexdigest()
    name_key = hashlib.sha256(name.encode()).hexdigest()
    base = root if root is not None else multiverse_dir()
    return base / "agent_descriptions" / workspace_key / f"{name_key}.json"


def validate_description(description: str) -> str | None:
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"description is {len(description)} characters; the maximum is "
            f"{MAX_DESCRIPTION_CHARS}. Keep it very short and concise"
        )
    if any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029"
           for c in description):
        raise ValueError("description must be a single line without control characters")
    return description.strip() or None


def read_description(cwd: str, name: str, *, root: Path | None = None) -> str | None:
    try:
        data = json.loads(description_path(cwd, name, root=root).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("description") is None:
            return None
        return validate_description(data["description"])
    except (OSError, ValueError, RuntimeError):
        return None


def write_description(
    cwd: str, name: str, description: str, *, root: Path | None = None,
) -> str | None:
    """Validate before writing; invalid updates leave the previous value intact."""
    value = validate_description(description)
    atomic_write_json(description_path(cwd, name, root=root), {"description": value})
    return value
