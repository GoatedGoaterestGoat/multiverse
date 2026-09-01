"""Durable SecondBrain user-prompt delivery for managed pi sessions.

SecondBrain prompts must become real pi user messages, not inter-agent context and
not terminal keystrokes.  The connector therefore writes a small durable queue;
the in-process pi extension claims each prompt and hands it to
``pi.sendUserMessage`` before committing the claim.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from .atomic_files import atomic_write_json
from .paths import multiverse_dir

_STALE_CLAIM_SECONDS = 60.0
PROMPT_CONSUMER_VERSION = 1
_CAPABILITY_FILE = "pi_capabilities.json"
_CAPABILITY_KEY = "secondbrain_prompt_consumer"


def capability_path(session_dir: Path) -> Path:
    return session_dir / _CAPABILITY_FILE


def record_prompt_consumer_capability(session_dir: Path) -> None:
    """Advertise that this wrapper's loaded extension can drain pi prompts."""
    atomic_write_json(
        capability_path(session_dir),
        {_CAPABILITY_KEY: PROMPT_CONSUMER_VERSION},
    )


def clear_prompt_consumer_capability(session_dir: Path) -> None:
    """Remove the live-extension advertisement during session replacement/exit."""
    try:
        capability_path(session_dir).unlink(missing_ok=True)
    except OSError:
        pass


def has_prompt_consumer_capability(session_dir: Path) -> bool:
    """Whether the current wrapper advertises the exact consumer version we need."""
    try:
        data = json.loads(capability_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    version = data.get(_CAPABILITY_KEY) if isinstance(data, dict) else None
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version == PROMPT_CONSUMER_VERSION
    )


def _encode_cwd(cwd: str) -> str:
    try:
        cwd = str(Path(cwd).expanduser().resolve())
    except (OSError, RuntimeError):
        pass
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def prompt_dir(cwd: str, agent_name: str) -> Path:
    return multiverse_dir() / "secondbrain" / "pi_prompts" / _encode_cwd(cwd) / agent_name


def write_prompt(cwd: str, agent_name: str, text: str) -> Path:
    prompt_id = str(uuid.uuid4())
    path = prompt_dir(cwd, agent_name) / f"{prompt_id}.json"
    atomic_write_json(
        path,
        {"prompt_id": prompt_id, "text": text, "ts": time.time()},
    )
    return path


def claim_prompts(cwd: str, agent_name: str) -> list[dict]:
    directory = prompt_dir(cwd, agent_name)
    if not directory.is_dir():
        return []
    _readopt_stale_claims(directory)
    claimant = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    claimed: list[dict] = []
    for path in directory.glob("*.json"):
        claim_path = path.with_name(path.name + f".claim-{claimant}")
        try:
            path.rename(claim_path)
        except OSError:
            continue
        try:
            data = json.loads(claim_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            try:
                claim_path.unlink()
            except OSError:
                pass
            continue
        data["__claim_path"] = str(claim_path)
        claimed.append(data)
    claimed.sort(key=lambda item: item.get("ts", 0))
    return claimed


def commit_prompts(prompts: list[dict]) -> int:
    committed = 0
    for prompt in prompts:
        raw = prompt.get("__claim_path") if isinstance(prompt, dict) else None
        if not raw:
            continue
        try:
            Path(raw).unlink()
        except OSError:
            continue
        committed += 1
    return committed


def release_prompts(cwd: str, agent_name: str, basenames: list[str]) -> int:
    directory = prompt_dir(cwd, agent_name)
    if not directory.is_dir():
        return 0
    released = 0
    for basename in basenames:
        if (
            not basename
            or Path(basename).name != basename
            or ".json.claim-" not in basename
        ):
            continue
        claim_path = directory / basename
        original = claim_path.with_name(claim_path.name.split(".claim-")[0])
        try:
            claim_path.rename(original)
        except OSError:
            continue
        released += 1
    return released


def _readopt_stale_claims(directory: Path) -> None:
    now = time.time()
    for claim_path in directory.glob("*.json.claim-*"):
        try:
            if now - claim_path.stat().st_mtime < _STALE_CLAIM_SECONDS:
                continue
        except OSError:
            continue
        original = claim_path.with_name(claim_path.name.split(".claim-")[0])
        try:
            claim_path.rename(original)
        except OSError:
            pass
