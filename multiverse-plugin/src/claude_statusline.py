"""Exact Claude context capacity, captured from the native statusline.

Claude Code computes a session's real context window (``[1m]`` suffix, the
1M beta, ``CLAUDE_CODE_MAX_CONTEXT_TOKENS``, model capabilities, account
clamps) inside the binary and exposes the result in exactly one place: the
``context_window.context_window_size`` field of the JSON it pipes to the
configured ``statusLine`` command. Transcripts record only the bare model id
and hook payloads carry no capacity at all, so nothing on disk holds the
number a session actually runs under. Guessing it from a model name would
miscolor real sessions, so this module captures the exact value instead.

Two entry points, both reached through ``scripts/claude-statusline.py``:

``launch CLAUDE_BIN <argv...>``
    Prepares a per-session ``--settings`` file whose ``statusLine`` runs the
    recorder below, wrapped around the user's own effective statusline
    command, then ``execvpe``s Claude with the rewritten argv. The user's own
    settings files are never modified; every other statusLine field they set
    (padding, refresh options, custom flags) is carried over unchanged. When
    instrumentation cannot be configured safely (a managed policy already
    owns the statusline, a non-command statusline, unparsable explicit
    ``--settings``, an unwritable state dir), Claude is exec'd with the
    ORIGINAL argv — the feature fails open to "capacity unknown".

``render [--relay-b64 <original command>]``
    Reads the statusline payload once, records the capacity under
    ``<multiverse_dir>/claude_statusline/<workspace slug>/<session id>.json``
    and relays the unchanged bytes to the user's original command so their
    statusline stdout, stderr and exit status are exactly what they were.

The record is keyed the way ``full_jsonl.live_jsonl_path`` keys transcripts
(Claude's own project slug, taken from ``transcript_path``), so
``claude_usage.context_window_cached(uuid, cwd)`` finds it for live and
dormant rows alike.  ``render`` never raises: a recording problem must not
take the user's statusline down with it.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn, Sequence

from .atomic_files import atomic_write_bytes
from .full_jsonl import cwd_slug
from .paths import multiverse_dir

RECORD_DIRNAME = "claude_statusline"
SETTINGS_FILENAME = "claude-settings.json"
_SETTING_SOURCES = ("user", "project", "local")
# One filename-safe path component: Claude's session ids are UUIDs, but a
# ``--session-id`` is user-supplied, so anything that could escape the
# record directory (separators, ``.``/``..``, NULs) is rejected outright.
_COMPONENT = re.compile(r"^(?!\.\.?$)[A-Za-z0-9._-]{1,200}$")
_RELAY_SHELL = "/bin/sh"


# ---------------------------------------------------------------------------
# Record store
# ---------------------------------------------------------------------------

def record_dir() -> Path:
    return multiverse_dir() / RECORD_DIRNAME


def record_path(claude_uuid: str, cwd: str) -> Path:
    """Where the recorder files ``cwd``'s session ``claude_uuid``.

    Mirrors ``full_jsonl.live_jsonl_path``: the workspace is keyed by Claude's
    project slug, so the reader and the recorder agree even though one has
    the workspace path and the other only the transcript path.
    """
    return record_dir() / cwd_slug(cwd) / f"{claude_uuid}.json"


def _safe_component(value: object) -> str | None:
    # fullmatch: ``$`` alone would still accept a trailing newline.
    if isinstance(value, str) and _COMPONENT.fullmatch(value):
        return value
    return None


def is_session_id(value: object) -> bool:
    """True when ``value`` can name a record file without leaving its dir."""
    return _safe_component(value) is not None


def _workspace_slug(payload: dict) -> str | None:
    """Claude's project slug for the payload's session.

    ``transcript_path`` is fixed for the session's lifetime and lives in
    ``<projects>/<slug>/``, so its parent name is authoritative even after
    the session ``cd``s elsewhere. Without one, the session's project dir
    (``workspace.project_dir``) and the workspace the monitor launched us in
    (``MULTIVERSE_SESSION_CWD``) are the next-best identities; the payload's
    own ``cwd`` follows the session around and is last.
    """
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        slug = _safe_component(Path(transcript).parent.name)
        if slug is not None:
            return slug
    workspace = payload.get("workspace")
    project_dir = workspace.get("project_dir") if isinstance(workspace, dict) else None
    for cwd in (project_dir, os.environ.get("MULTIVERSE_SESSION_CWD"), payload.get("cwd")):
        if isinstance(cwd, str) and cwd:
            return _safe_component(cwd_slug(cwd))
    return None


def _capacity(payload: dict) -> int | None:
    window = payload.get("context_window")
    if not isinstance(window, dict):
        return None
    size = window.get("context_window_size")
    # bool is an int in Python, but is not a token capacity.
    if type(size) is int and size > 0:
        return size
    return None


def record(raw: bytes, *, now: float | None = None) -> Path | None:
    """Persist the capacity from one statusline payload; the path, or None.

    Stores only what the dashboard needs — session id, workspace identity,
    capacity, model id, timestamp — not the payload (which carries cost and
    rate-limit data no reader here wants). A payload without a positive
    ``context_window_size`` is not recorded, so a previously measured value
    is never replaced by "unknown".
    """
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    session_id = _safe_component(payload.get("session_id"))
    slug = _workspace_slug(payload)
    capacity = _capacity(payload)
    if session_id is None or slug is None or capacity is None:
        return None
    model = payload.get("model")
    model_id = model.get("id") if isinstance(model, dict) else None
    entry = {
        "recorded_at": time.time() if now is None else now,
        "session_id": session_id,
        "workspace_slug": slug,
        "transcript_path": payload.get("transcript_path"),
        "cwd": payload.get("cwd"),
        "model_id": model_id if isinstance(model_id, str) else None,
        "context_window_size": capacity,
    }
    path = record_dir() / slug / f"{session_id}.json"
    atomic_write_bytes(
        path, json.dumps(entry, separators=(",", ":")).encode() + b"\n", mode=0o600,
    )
    return path


def read_record(claude_uuid: str, cwd: str) -> dict | None:
    """The stored entry for a session, or None when absent or unusable."""
    return parse_record(record_path(claude_uuid, cwd).read_bytes(), claude_uuid)


def parse_record(raw: bytes, claude_uuid: str) -> dict | None:
    """Validate one stored entry; None unless it is this session's."""
    try:
        entry = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(entry, dict) or entry.get("session_id") != claude_uuid:
        return None
    size = entry.get("context_window_size")
    if type(size) is not int or size <= 0:
        return None
    return entry


# ---------------------------------------------------------------------------
# Relay
# ---------------------------------------------------------------------------

def relay(raw: bytes, command: str | None, *, cwd: str | None = None) -> int:
    """Run the user's statusline command on the same bytes; its exit status.

    stdout and stderr are inherited, so the text Claude renders is the
    user's, untouched. No command means no statusline was configured:
    print nothing and succeed, which is what Claude saw before.
    """
    if not command:
        return 0
    try:
        return subprocess.run(
            [_RELAY_SHELL, "-c", command], input=raw, cwd=cwd, check=False,
        ).returncode
    except OSError:
        return 1


def render_command(
    script_path: Path, original_command: str | None, *, python: str | None = None
) -> str:
    """The statusLine command that records, then relays to ``original_command``.

    The original is carried inside the command as base64 so it survives the
    shell that runs this line with no quoting layer to get wrong; every path
    is quoted so spaces in the plugin root or interpreter work.
    """
    parts = [python or sys.executable, str(script_path), "render"]
    if original_command:
        encoded = base64.b64encode(original_command.encode("utf-8")).decode("ascii")
        parts += ["--relay-b64", encoded]
    return " ".join(shlex.quote(part) for part in parts)


def _decode_relay(encoded: str | None) -> str | None:
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8") or None
    except (ValueError, UnicodeError):
        return None


# ---------------------------------------------------------------------------
# Effective settings
# ---------------------------------------------------------------------------

def _flag_value(args: Sequence[str], flag: str) -> str | None:
    """The last ``flag X`` / ``flag=X`` before a literal ``--``, or None.

    Everything after ``--`` is positional to Claude (a prompt can be spelled
    ``--settings=anything``), so it is never read as an option here.
    """
    value = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break
        if arg == flag and i + 1 < len(args):
            value = args[i + 1]
            i += 1
        elif arg.startswith(flag + "="):
            value = arg[len(flag) + 1:]
        i += 1
    return value


def _strip_flag(args: Sequence[str], flag: str) -> list[str]:
    kept: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            kept.extend(args[i:])
            break
        if arg == flag:
            i += 2
            continue
        if arg.startswith(flag + "="):
            i += 1
            continue
        kept.append(arg)
        i += 1
    return kept


def _load_settings_file(path: Path) -> dict | None:
    """A settings object, or None when the file is absent or not an object.

    Raises for a present-but-unparsable file, so a caller deciding whether
    instrumentation is safe can tell "no file" from "broken file".
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    value = json.loads(text)
    return value if isinstance(value, dict) else None


def _flag_settings(value: str | None, cwd: str) -> dict | None:
    """The explicit ``--settings`` object: inline JSON, else a file path."""
    if value is None:
        return None
    stripped = value.strip()
    if stripped.startswith("{"):
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError("--settings JSON is not an object")
        return parsed
    path = Path(value)
    if not path.is_absolute():
        path = Path(cwd) / path
    loaded = _load_settings_file(path)
    if loaded is None:
        raise ValueError(f"--settings file is missing or not an object: {value}")
    return loaded


def _sources(args: Sequence[str]) -> set[str]:
    value = _flag_value(args, "--setting-sources")
    if value is None:
        return set(_SETTING_SOURCES)
    return {part.strip() for part in value.split(",") if part.strip()}


def _config_dir(config_dir: str | None) -> Path:
    explicit = config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.claude"))


def managed_settings_path() -> Path:
    """Where this platform's enterprise policy file lives."""
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if sys.platform.startswith("win"):
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ClaudeCode" / "managed-settings.json"
    return Path("/etc/claude-code/managed-settings.json")


def _git_toplevel(cwd: str) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root) if root else None


def _local_settings_dir(cwd: str) -> Path:
    """Where Claude reads ``.claude/settings.local.json`` from.

    Project settings come from the cwd itself. Local settings come from the
    git worktree root when launching from a subdirectory of one — unless
    that root is the home directory, which Claude refuses to treat as a
    project. Mirrors the binary's localSettings resolution; an unreadable
    or missing git answer falls back to the cwd.
    """
    root = _git_toplevel(cwd)
    if root is None:
        return Path(cwd)
    try:
        resolved_root = root.resolve()
        if resolved_root == Path(cwd).resolve():
            return Path(cwd)
        if resolved_root == Path.home().resolve():
            return Path(cwd)
    except OSError:
        return Path(cwd)
    return root


def statusline_layers(
    cwd: str, claude_args: Sequence[str] = (), *, config_dir: str | None = None,
) -> dict[str, Any]:
    """Each source's ``statusLine`` value, keyed by source, lowest first.

    Only sources Claude will consult are read: ``--setting-sources`` prunes
    user/project/local; an explicit ``--settings`` and a managed policy file
    always apply. Raises when a consulted file is unparsable — the caller
    treats that as "cannot instrument safely".
    """
    sources = _sources(claude_args)
    layers: dict[str, Any] = {}
    files: list[tuple[str, Path]] = []
    if "user" in sources:
        files.append(("user", _config_dir(config_dir) / "settings.json"))
    if "project" in sources:
        files.append(("project", Path(cwd) / ".claude" / "settings.json"))
    if "local" in sources:
        files.append(("local", _local_settings_dir(cwd) / ".claude" / "settings.local.json"))
    for name, path in files:
        loaded = _load_settings_file(path)
        if loaded is not None and "statusLine" in loaded:
            layers[name] = loaded["statusLine"]
    flag = _flag_settings(_flag_value(claude_args, "--settings"), cwd)
    if flag is not None and "statusLine" in flag:
        layers["flag"] = flag["statusLine"]
    managed = _load_settings_file(managed_settings_path())
    if managed is not None and "statusLine" in managed:
        layers["managed"] = managed["statusLine"]
    return layers


_LAYER_ORDER = ("user", "project", "local", "flag", "managed")


def _deep_merge(base: Any, override: Any) -> Any:
    """Claude's settings merge: objects merge key by key, anything else wins."""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return override


def _merged_statusline(layers: dict[str, Any], names: Sequence[str]) -> Any:
    merged: Any = None
    found = False
    for name in names:
        if name in layers:
            merged = layers[name] if not found else _deep_merge(merged, layers[name])
            found = True
    return merged


def effective_statusline(
    cwd: str, claude_args: Sequence[str] = (), *, config_dir: str | None = None,
) -> Any:
    """The ``statusLine`` value Claude would use for this launch, or None.

    Layers deep-merge lowest to highest (user, project, local, explicit
    ``--settings``, managed policy), so a user-level command survives a
    project file that only sets padding.
    """
    layers = statusline_layers(cwd, claude_args, config_dir=config_dir)
    return _merged_statusline(layers, _LAYER_ORDER)


def instrumented_settings(
    cwd: str,
    claude_args: Sequence[str] = (),
    *,
    script_path: Path,
    config_dir: str | None = None,
    python: str | None = None,
) -> dict | None:
    """The ``--settings`` object that wraps the user's statusline, or None.

    None means "leave the launch alone": a managed policy sets the
    statusline command or type (a CLI flag cannot override it, so wrapping
    would be a lie), or the effective statusline is something other than a
    command, or the command is not a string. The explicit ``--settings``
    content is kept as the base so nothing else the user passed is lost;
    only ``statusLine`` changes, and within it only ``type`` and
    ``command`` — every other field of the deep-merged user/project/local
    layers rides along unchanged.
    """
    layers = statusline_layers(cwd, claude_args, config_dir=config_dir)
    managed = layers.get("managed")
    if managed is not None and (
        not isinstance(managed, dict) or "command" in managed or "type" in managed
    ):
        return None
    original = _merged_statusline(layers, ("user", "project", "local", "flag"))
    if original is None:
        original = {}
    if not isinstance(original, dict):
        return None
    if original.get("type", "command") != "command":
        return None
    command = original.get("command")
    if command is not None and not isinstance(command, str):
        return None
    override = dict(original)
    override["type"] = "command"
    override["command"] = render_command(script_path, command, python=python)
    base = _flag_settings(_flag_value(claude_args, "--settings"), cwd) or {}
    merged = dict(base)
    merged["statusLine"] = override
    return merged


def prepare_launch(
    claude_args: Sequence[str],
    cwd: str,
    *,
    script_path: Path,
    settings_file: Path,
    config_dir: str | None = None,
    python: str | None = None,
) -> list[str]:
    """``claude_args`` rewritten to load the instrumented settings file.

    Writes the merged settings to ``settings_file`` and returns the argv with
    exactly one ``--settings <settings_file>`` (a user-supplied ``--settings``
    is folded into the file, so it is dropped from the argv). Any failure
    returns the original argv unchanged: capacity stays unknown, the session
    launches as it always did.
    """
    original = list(claude_args)
    try:
        merged = instrumented_settings(
            cwd, original, script_path=script_path, config_dir=config_dir, python=python,
        )
        if merged is None:
            return original
        atomic_write_bytes(
            settings_file,
            json.dumps(merged, indent=2).encode("utf-8") + b"\n",
            mode=0o600,
        )
    except Exception:
        return original
    # Prepend rather than append: a trailing positional prompt may start
    # with anything, and a ``--`` in the user's args would demote a trailing
    # option to a positional.
    return ["--settings", str(settings_file), *_strip_flag(original, "--settings")]


def settings_file_path() -> Path:
    """Per-session location for the instrumented settings file.

    Beside the session's status.json when launched by the monitor; a unique
    file under the record dir otherwise (no state dir to be tidy in).
    """
    status = os.environ.get("MULTIVERSE_STATUS")
    if status:
        return Path(status).parent / SETTINGS_FILENAME
    return record_dir() / "settings" / f"{os.getpid()}-{secrets.token_hex(4)}.json"


# ---------------------------------------------------------------------------
# Script entry points
# ---------------------------------------------------------------------------

def launch(claude_bin: str, claude_args: Sequence[str], *, script_path: Path) -> NoReturn:
    """Exec Claude with instrumented argv, or the original argv on any trouble."""
    args = list(claude_args)
    try:
        args = prepare_launch(
            args, os.getcwd(), script_path=script_path, settings_file=settings_file_path(),
        )
    except Exception:
        args = list(claude_args)
    try:
        os.execvpe(claude_bin, [claude_bin, *args], os.environ)
    except OSError as exc:
        sys.stderr.write(f"[claude-statusline] cannot exec {claude_bin}: {exc}\n")
        sys.exit(127)


def render(argv: Sequence[str], stdin: Any = None) -> int:
    """Record the payload on stdin, then relay it to the original command."""
    raw = (stdin or sys.stdin.buffer).read()
    try:
        record(raw)
    except Exception:
        pass
    return relay(raw, _decode_relay(_flag_value(argv, "--relay-b64")))


def main(argv: Sequence[str], *, script_path: Path) -> int:
    if argv and argv[0] == "render":
        return render(argv[1:])
    if len(argv) >= 2 and argv[0] == "launch":
        launch(argv[1], argv[2:], script_path=script_path)
    sys.stderr.write(
        "usage: claude-statusline.py launch CLAUDE_BIN [claude args...]\n"
        "       claude-statusline.py render [--relay-b64 COMMAND]\n"
    )
    return 2
