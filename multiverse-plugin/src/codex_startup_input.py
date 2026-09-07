"""Name a fresh Codex TUI thread before delivering its initial prompt."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from . import codex_control
from .atomic_files import atomic_write_text
from .sb_actions import (
    _run_tmux,
    capture_pane,
    capture_focused_codex_composer_draft,
    paste_text_via_tmux,
)


_POLL_INTERVAL_S = 0.025
_COMPOSER_TIMEOUT_S = 30.0
_SUBMIT_ACK_TIMEOUT_S = 0.75
_SUBMIT_ATTEMPTS = 3
_NAME_ACK_TIMEOUT_S = 8.0
_OPAQUE_PASTE_RE = re.compile(r"^\[Pasted Content (\d+) chars\]$")
_THREAD_ID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_OWNERSHIP_ERROR = "Codex startup session is no longer locally owned and interactive"


def _is_local(tmux_name: str) -> bool:
    return codex_control.local_session(tmux_name, startup=True) is not None


def _wait_for_empty_composer(tmux_name: str) -> tuple[bool, str]:
    deadline = time.monotonic() + _COMPOSER_TIMEOUT_S
    last_error = "Codex input composer did not become ready"
    while time.monotonic() < deadline:
        if not _is_local(tmux_name):
            return False, _OWNERSHIP_ERROR
        ok, draft, error = capture_focused_codex_composer_draft(tmux_name)
        if ok and not draft:
            return True, ""
        if ok:
            return False, "Codex startup composer unexpectedly contains a draft"
        if error:
            last_error = error
        time.sleep(_POLL_INTERVAL_S)
    return False, last_error


def _loaded_prompt_state(
    tmux_name: str,
    expected: str,
    *,
    allow_opaque: bool,
) -> tuple[str, str]:
    """Return ``exact``, ``opaque``, ``empty``, ``other``, or ``unavailable``."""
    if not _is_local(tmux_name):
        return "unavailable", _OWNERSHIP_ERROR
    ok, draft, error = capture_focused_codex_composer_draft(tmux_name)
    if ok:
        if draft == expected:
            return "exact", ""
        if not draft:
            return "empty", ""
        return "other", draft
    if allow_opaque:
        match = _OPAQUE_PASTE_RE.fullmatch(draft.strip())
        if match and int(match.group(1)) == len(expected):
            return "opaque", ""
    return "unavailable", error


def _wait_for_loaded_prompt(
    tmux_name: str,
    text: str,
    *,
    allow_opaque: bool,
) -> tuple[bool, str]:
    deadline = time.monotonic() + _COMPOSER_TIMEOUT_S
    last_error = "Codex did not render the pasted startup input"
    while time.monotonic() < deadline:
        if not _is_local(tmux_name):
            return False, _OWNERSHIP_ERROR
        state, detail = _loaded_prompt_state(
            tmux_name, text, allow_opaque=allow_opaque
        )
        if state in ("exact", "opaque"):
            return True, ""
        if state in ("empty", "other"):
            return False, "Codex startup input changed unexpectedly after paste"
        if detail:
            last_error = detail
        time.sleep(_POLL_INTERVAL_S)
    return False, last_error


def _submit_loaded_prompt(
    tmux_name: str,
    text: str,
    *,
    allow_opaque: bool,
) -> tuple[bool, str]:
    error = "Codex did not acknowledge startup input submission"
    for _ in range(_SUBMIT_ATTEMPTS):
        if not _is_local(tmux_name):
            return False, _OWNERSHIP_ERROR
        state, _ = _loaded_prompt_state(tmux_name, text, allow_opaque=allow_opaque)
        if state not in ("exact", "opaque"):
            return False, "Codex startup input changed before submission"
        if not _is_local(tmux_name):
            return False, _OWNERSHIP_ERROR
        ok, error = _run_tmux(["send-keys", "-t", tmux_name, "Enter"])
        if not ok:
            return False, error
        deadline = time.monotonic() + _SUBMIT_ACK_TIMEOUT_S
        while time.monotonic() < deadline:
            if not _is_local(tmux_name):
                return False, _OWNERSHIP_ERROR
            state, detail = _loaded_prompt_state(
                tmux_name, text, allow_opaque=allow_opaque
            )
            if state == "empty":
                return True, ""
            if state == "other":
                return False, "Codex startup input changed instead of being submitted"
            # The composer can briefly lose focus while a slash command is handled.
            if state == "unavailable" and detail:
                error = detail
            time.sleep(_POLL_INTERVAL_S)
    return False, error or "Codex did not acknowledge startup input submission"


def _send_startup_input(
    tmux_name: str,
    text: str,
    *,
    allow_opaque: bool = False,
    composer_ready: bool = False,
) -> tuple[bool, str]:
    if not composer_ready:
        ok, error = _wait_for_empty_composer(tmux_name)
        if not ok:
            return False, error
    if not _is_local(tmux_name):
        return False, _OWNERSHIP_ERROR
    ok, draft, error = capture_focused_codex_composer_draft(tmux_name)
    if not ok or draft:
        return False, error or "Codex startup composer changed before paste"
    if not _is_local(tmux_name):
        return False, _OWNERSHIP_ERROR
    ok, error = paste_text_via_tmux(tmux_name, text)
    if not ok:
        return False, error
    ok, error = _wait_for_loaded_prompt(
        tmux_name, text, allow_opaque=allow_opaque
    )
    if not ok:
        return False, error
    return _submit_loaded_prompt(tmux_name, text, allow_opaque=allow_opaque)


def _read_native_id(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return value or None


def _record_visible_native_id(tmux_name: str, path: Path) -> bool:
    """Seed the native ID from a fresh TUI footer before the first turn hook."""
    if not _is_local(tmux_name):
        return False
    if _read_native_id(path):
        return True
    ok, pane = capture_pane(tmux_name)
    if not ok:
        return False
    matches = _THREAD_ID_RE.findall(pane)
    if not matches:
        return False
    if not _is_local(tmux_name):
        return False
    try:
        atomic_write_text(path, matches[-1] + "\n", mode=0o600)
    except OSError:
        return False
    return True


def _indexed_thread_name(index_path: Path, thread_id: str) -> str | None:
    name: str | None = None
    try:
        with index_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("id") != thread_id:
                    continue
                value = record.get("thread_name")
                name = value if isinstance(value, str) and value else None
    except OSError:
        return None
    return name


def _wait_for_native_name(
    native_id_path: Path,
    index_path: Path,
    expected: str,
) -> bool:
    deadline = time.monotonic() + _NAME_ACK_TIMEOUT_S
    while time.monotonic() < deadline:
        thread_id = _read_native_id(native_id_path)
        # session_index.jsonl is shared by all hosts/workspaces. A newly
        # appended matching NAME is not evidence that this TUI owns that ID.
        # Only the local footer or runtime hook may establish the native ID.
        if thread_id and _indexed_thread_name(index_path, thread_id) == expected:
            return True
        time.sleep(_POLL_INTERVAL_S)
    return False


def deliver_startup_input(
    tmux_name: str,
    name: str,
    prompt_path: Path | None,
    native_id_path: Path,
    codex_home: Path,
) -> list[str]:
    """Rename through the live TUI, then submit the saved initial prompt."""
    if not _is_local(tmux_name):
        return [_OWNERSHIP_ERROR]
    errors: list[str] = []
    index_path = codex_home / "session_index.jsonl"
    ready, error = _wait_for_empty_composer(tmux_name)
    if ready:
        _record_visible_native_id(tmux_name, native_id_path)
        ok, error = _send_startup_input(
            tmux_name,
            f"/rename {name}",
            composer_ready=True,
        )
    else:
        ok = False
    if not ok:
        errors.append(f"native rename failed: {error}")
    elif not _wait_for_native_name(
        native_id_path,
        index_path,
        name,
    ):
        errors.append("native rename was not acknowledged")

    prompt = ""
    if prompt_path is not None:
        try:
            prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"initial prompt could not be read: {exc}")

    if prompt:
        if not _is_local(tmux_name):
            return errors + [_OWNERSHIP_ERROR]
        ok, error = _send_startup_input(
            tmux_name,
            prompt,
            allow_opaque=True,
        )
        if ok and _is_local(tmux_name):
            try:
                prompt_path.unlink()
            except OSError:
                pass
        else:
            errors.append(f"initial prompt delivery failed: {error}")
    return errors


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tmux-name", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--native-id-path", required=True, type=Path)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--error-file", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    errors = deliver_startup_input(
        args.tmux_name,
        args.name,
        args.prompt_file,
        args.native_id_path,
        args.codex_home,
    )
    if errors:
        atomic_write_text(args.error_file, "\n".join(errors) + "\n", mode=0o600)
        return 1
    try:
        args.error_file.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
