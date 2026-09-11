"""A timed-out ask fork must not outlive its killer.

The ask paths spawn a CLI that writes the fork's transcript, then delete that
transcript when the ask finishes. Signalling only the direct child on timeout
leaves its descendants running: one of them finishes the turn and writes the
files *after* the cleanup deleted them, so the delete is silently undone. The
recreated transcript inherits the origin session's custom-title and surfaces
as a second, preview-less dashboard row under a real agent's own name.

These tests use a real process tree (a shell that backgrounds a grandchild)
rather than a mock, because the bug is exactly that a mock would not have:
descendants that survive `Popen.kill()`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import ask_agents


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class KillProcessGroupTests(unittest.TestCase):
    """_kill_process_group must reap descendants, not just the direct child."""

    def _spawn_tree(self, marker: Path) -> subprocess.Popen:
        # Parent exits immediately; the grandchild sleeps, then touches the
        # marker. Killing only the parent leaves the grandchild to fire.
        script = (
            f"( sleep 30; touch {marker!s} ) & "
            "echo spawned; "
            # Keep the direct child alive so the timeout path is what kills it.
            "sleep 30"
        )
        proc = subprocess.Popen(
            ["/bin/sh", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        def _close_pipes() -> None:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass

        self.addCleanup(_close_pipes)
        return proc

    def test_group_kill_takes_descendants_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "survivor-ran"
            proc = self._spawn_tree(marker)
            self.addCleanup(ask_agents._kill_process_group, proc)
            self.assertTrue(
                _wait_until(lambda: proc.stdout.readline().strip() == "spawned"),
                "child never started",
            )

            ask_agents._kill_process_group(proc)

            self.assertIsNotNone(proc.poll(), "direct child still running")
            # The grandchild would touch the marker ~30s out. Give it well
            # past a plain child-only kill's blast radius.
            self.assertFalse(
                _wait_until(marker.exists, timeout=3.0),
                "a descendant survived the group kill",
            )

    def test_group_kill_is_safe_on_an_already_dead_child(self) -> None:
        proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
        proc.wait()
        ask_agents._kill_process_group(proc)  # must not raise

    def test_child_sharing_our_group_is_killed_alone(self) -> None:
        # Without start_new_session the child shares our process group, and
        # signalling that group would kill this process — a monitor or daemon
        # — instead of the fork. Such a child must be signalled on its own.
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
        self.addCleanup(proc.kill)
        self.assertEqual(os.getpgid(proc.pid), os.getpgid(0), "expected a shared group")

        ask_agents._kill_process_group(proc)

        self.assertIsNotNone(proc.poll(), "child survived")
        # Reaching this line at all proves we did not signal our own group.


class ClaudeAskTimeoutReapsForkTests(unittest.TestCase):
    """A timed-out Claude ask leaves no transcript behind."""

    def _fake_claude(self, root: Path, projects: Path, full: Path) -> Path:
        # Stands in for the CLI: backgrounds a grandchild that writes the fork
        # transcript and its mirror sidecars AFTER the ask has timed out, then
        # blocks so the ask's timeout is what ends it.
        fake = root / "claude"
        fake.write_text(
            "#!/bin/sh\n"
            'fork=""\n'
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--session-id" ]; then fork="$2"; fi\n'
            "  shift\n"
            "done\n"
            "(\n"
            "  sleep 2\n"
            f'  echo "{{}}" > "{projects}/$fork.jsonl"\n'
            f'  echo "{{}}" > "{full}/$fork.jsonl"\n'
            f'  echo marker > "{full}/$fork.marker"\n'
            f'  echo "{{}}" > "{full}/$fork.cursor.json"\n'
            f'  : > "{full}/.$fork.sync.lock"\n'
            ") &\n"
            "sleep 30\n"
        )
        fake.chmod(0o755)
        return fake

    def test_timed_out_fork_cannot_recreate_its_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects" / "-slug"
            projects.mkdir(parents=True)
            full = root / "full_jsonls"
            full.mkdir()
            (projects / "origin.jsonl").write_text("{}\n")
            fake = self._fake_claude(root, projects, full)

            target = ask_agents.AskTarget(
                name="agent",
                cwd=str(root),
                runtime="claude",
                session_id="origin",
            )
            with (
                patch.dict(
                    os.environ,
                    {"MULTIVERSE_CLAUDE_BIN": str(fake)},
                ),
                patch.object(ask_agents, "CLAUDE_PROJECTS_DIR", projects.parent),
                patch.object(ask_agents, "_full_dir", return_value=full),
                patch.object(ask_agents.forks, "mark_active", lambda *a, **k: None),
                patch.object(ask_agents.forks, "mark_done", lambda *a, **k: None),
            ):
                result = ask_agents.ask_one("question?", target, timeout=1)
                self.assertIsNotNone(result.error)
                self.assertIn("timed out", result.error)

                fork = result.fork_uuid
                artifacts = [
                    projects / f"{fork}.jsonl",
                    full / f"{fork}.jsonl",
                    full / f"{fork}.marker",
                    full / f"{fork}.cursor.json",
                    full / f".{fork}.sync.lock",
                ]
                # The grandchild's write lands ~2s in — after cleanup. Wait
                # past it, then assert nothing came back. Every path is
                # inside this test's tmpdir, so a failure cannot leak a
                # phantom into a real dashboard.
                reappeared = _wait_until(
                    lambda: any(p.exists() for p in artifacts), timeout=5.0
                )
                self.assertFalse(
                    reappeared,
                    "a surviving fork recreated its transcript after cleanup",
                )


if __name__ == "__main__":
    unittest.main()
