"""Guards the launch-failure diagnostics in ``bin/multiverse-session``.

The runtime's exit status used to be discarded with ``|| true``. A launch that
failed instantly — a ``--model`` the build rejects, a missing binary — was then
indistinguishable from a clean exit: the script wrote ``status=ended`` and the
tmux pane was torn down before anyone could read the error. That is exactly how
a bare ``opus[1m]`` alias against a packaged build presented: "starting", then
"ended", with no message anywhere.

These tests pin the three properties that make such a failure diagnosable, and
the one that must not regress while doing so:

1. the runtime's stderr is captured to ``launch-stderr.log`` in the session dir,
2. a non-zero exit is recorded in ``last-exit``,
3. ``status.json`` is still written afterwards — i.e. a non-zero exit must not
   abort the script under ``set -e``, which is what the original ``|| true``
   was there to guarantee and what the monitor's reaping depends on,
4. a *successful* launch leaves no ``last-exit`` marker.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SESSION_SCRIPT = PLUGIN_ROOT / "bin" / "multiverse-session"


def _run_with_fake_runtime(script_body: str) -> tuple[int, Path, str]:
    """Run multiverse-session with a stub runtime on PATH.

    Returns (exit code, session dir, combined output). The stub stands in for
    claude/tclaude so the test never launches a real model or needs a network.
    """
    tmp = Path(tempfile.mkdtemp())
    bindir = tmp / "bin"
    bindir.mkdir()
    fake = bindir / "fake-runtime"
    fake.write_text("#!/usr/bin/env bash\n" + script_body)
    fake.chmod(0o755)

    session_dir = tmp / "sessions" / "abcd1234"
    session_dir.mkdir(parents=True)
    status = session_dir / "status.json"

    env = {k: v for k, v in os.environ.items() if not k.startswith("MULTIVERSE_")}
    env.update(
        {
            "PATH": f"{bindir}:{env.get('PATH', '')}",
            "MULTIVERSE_CLAUDE_BIN": "fake-runtime",
            "MULTIVERSE_STATUS": str(status),
            "MULTIVERSE_SESSION_ID": "abcd1234",
            # Keep the statusline launcher out of the path under test: it would
            # wrap the stub in a python process and change whose stderr we see.
            "MULTIVERSE_PLUGIN_ROOT": str(tmp / "no-plugin-root"),
        }
    )

    proc = subprocess.run(
        ["bash", str(SESSION_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp),
    )
    return proc.returncode, session_dir, proc.stdout + proc.stderr


class LaunchFailureDiagnosticsTests(unittest.TestCase):
    def test_failed_launch_captures_stderr_and_exit_and_still_ends(self) -> None:
        rc, session_dir, _ = _run_with_fake_runtime(
            'echo "model \\"opus[1m]\\" is not available." >&2\nexit 1\n'
        )

        log = session_dir / "launch-stderr.log"
        self.assertTrue(log.is_file(), "expected launch-stderr.log to be written")
        self.assertIn("is not available", log.read_text())

        last_exit = session_dir / "last-exit"
        self.assertTrue(last_exit.is_file(), "expected last-exit to be written")
        self.assertEqual("1", last_exit.read_text().strip())

        # The load-bearing part: a non-zero runtime exit must not abort the
        # script before the ended-status write the monitor reaps on.
        status = session_dir / "status.json"
        self.assertTrue(status.is_file(), "status.json must still be written")
        self.assertIn("ended", status.read_text())
        self.assertEqual(0, rc, "multiverse-session itself must exit 0")

    def test_successful_launch_records_no_exit_marker(self) -> None:
        rc, session_dir, _ = _run_with_fake_runtime('exit 0\n')

        self.assertFalse(
            (session_dir / "last-exit").exists(),
            "a clean exit must not leave a last-exit marker",
        )
        self.assertIn("ended", (session_dir / "status.json").read_text())
        self.assertEqual(0, rc)

    def test_runtime_stderr_still_reaches_the_terminal(self) -> None:
        """Teeing must not swallow stderr — the pane is the primary surface."""
        _, _, output = _run_with_fake_runtime('echo "boom" >&2\nexit 3\n')
        self.assertIn("boom", output)


if __name__ == "__main__":
    unittest.main()
