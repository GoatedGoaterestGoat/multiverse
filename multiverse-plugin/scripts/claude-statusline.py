#!/usr/bin/env python3
"""Claude statusline capacity recorder and launcher shim.

    claude-statusline.py launch CLAUDE_BIN [claude args...]
        Exec CLAUDE_BIN with a per-session --settings file whose statusLine
        wraps the user's own statusline command in ``render`` below. Falls
        back to exec'ing the original argv when instrumentation cannot be
        configured safely.

    claude-statusline.py render [--relay-b64 COMMAND]
        Configured as the statusLine command. Records the payload's exact
        context_window_size, then relays the unchanged bytes to the user's
        original command so their statusline output is untouched.

All logic lives in ``src.claude_statusline``. This file stays importable by
any Python 3 so that when that module cannot be imported at all (a
mismatched interpreter, a half-updated checkout) ``launch`` still execs the
original argv and ``render`` still relays the user's command: a broken
recorder must cost the user neither their session nor their statusline.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path


def _fallback(argv: list[str]) -> int:
    """Dependency-free behaviour when ``src.claude_statusline`` is unusable."""
    if len(argv) >= 2 and argv[0] == "launch":
        try:
            os.execvpe(argv[1], argv[1:], os.environ)
        except OSError as exc:
            sys.stderr.write(f"[claude-statusline] cannot exec {argv[1]}: {exc}\n")
            return 127
    if argv and argv[0] == "render":
        raw = sys.stdin.buffer.read()
        command = None
        for i, arg in enumerate(argv):
            if arg == "--relay-b64" and i + 1 < len(argv):
                try:
                    command = base64.b64decode(argv[i + 1]).decode("utf-8")
                except (ValueError, UnicodeError):
                    command = None
        if not command:
            return 0
        try:
            return subprocess.run(["/bin/sh", "-c", command], input=raw, check=False).returncode
        except OSError:
            return 1
    return 2


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.claude_statusline import main  # noqa: E402
except Exception:  # pragma: no cover - exercised through the subprocess tests
    main = None  # type: ignore[assignment]

if __name__ == "__main__":
    argv = sys.argv[1:]
    if main is None:
        sys.exit(_fallback(argv))
    try:
        sys.exit(main(argv, script_path=Path(__file__).resolve()))
    except Exception:
        sys.exit(_fallback(argv))
