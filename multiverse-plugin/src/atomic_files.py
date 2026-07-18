"""Small cross-process file primitives for Multiverse state."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def _open_unique_sibling(path: Path, mode: int) -> tuple[int, Path]:
    """Create a claimant-unique temporary file beside ``path``."""
    for _ in range(100):
        temp = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            continue
        return fd, temp
    raise FileExistsError(f"could not allocate a temporary file beside {path}")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o666) -> None:
    """Replace ``path`` without exposing partial content or shared temp names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = _open_unique_sibling(path, mode)
    try:
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def atomic_write_text(
    path: Path,
    value: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o666,
) -> None:
    atomic_write_bytes(path, value.encode(encoding), mode=mode)


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    mode: int = 0o666,
    indent: int | None = None,
) -> None:
    atomic_write_text(path, json.dumps(value, indent=indent), mode=mode)


def atomic_create_bytes(path: Path, data: bytes, *, mode: int = 0o666) -> bool:
    """Publish a complete file only if ``path`` does not already exist.

    The temporary file is fully written before a same-directory hard link makes
    it visible at ``path``. This preserves first-writer-wins semantics without
    the empty/partial window of ``O_EXCL`` followed by a write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = _open_unique_sibling(path, mode)
    try:
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def atomic_create_json(
    path: Path,
    value: Any,
    *,
    mode: int = 0o666,
    indent: int | None = None,
) -> bool:
    return atomic_create_bytes(
        path,
        json.dumps(value, indent=indent).encode(),
        mode=mode,
    )


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold an advisory cross-process lock associated with ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append_text_locked(
    path: Path,
    value: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o666,
) -> None:
    """Append one complete record while excluding cooperating writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            _write_all(fd, value.encode(encoding))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
