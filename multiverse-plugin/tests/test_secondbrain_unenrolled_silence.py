"""An un-enrolled workspace must write nothing under ``~/.multiverse/secondbrain``.

The signal recorders were already gated on enrolment + exposure, so no SecondBrain
event or notification is recorded or queued once a workspace is torn down. But two
writers were not gated, and both recreated the directory after a teardown:

* ``record_pending_tool`` -- the PreToolUse correlation cache, deliberately not
  *seeded*-gated (we can't trust env vars at PreToolUse), which left enrolment
  unchecked as a side effect.
* ``log_hook_outcome`` -- the hook diagnostic log.

Neither is ever delivered to SB and neither replays on reconnect, so this is residue
rather than data loss. It still matters: a directory that reappears after teardown
reads as "still recording", and every consumer of both is a SecondBrain path, so with
no device identity nothing can ever read what they write.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import sb_notify  # noqa: E402


class UnenrolledSilenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("MULTIVERSE_ROOT")
        os.environ["MULTIVERSE_ROOT"] = str(self.root)
        self.sb = self.root / "secondbrain"
        self.cwd = "/ws/proj"
        self.agent = "Alice"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("MULTIVERSE_ROOT", None)
        else:
            os.environ["MULTIVERSE_ROOT"] = self._prev

    def _files(self) -> list[str]:
        if not self.sb.exists():
            return []
        return sorted(p.name for p in self.sb.rglob("*") if p.is_file())

    def _enrol(self) -> None:
        self.sb.mkdir(parents=True, exist_ok=True)
        (self.sb / "device.key.json").write_text(
            json.dumps({"device_id": "dev", "granted_workspaces": [self.cwd]})
        )
        (self.sb / "state.json").write_text(
            json.dumps({"seeded": [], "exposed": [self.cwd]})
        )

    def _fire_every_writer(self) -> None:
        sb_notify.record_pending_tool(
            self.cwd, self.agent, "toolu_x", "Bash", {"command": "ls"}
        )
        sb_notify.log_hook_outcome(
            "sb-emit-response", "recorded", cwd=self.cwd, agent=self.agent
        )
        sb_notify.record_user_prompt_signal(self.cwd, self.agent, "hi")
        sb_notify.record_response_signal(self.cwd, self.agent)

    def test_writes_nothing_at_all_when_not_enrolled(self) -> None:
        self._fire_every_writer()
        self.assertEqual(self._files(), [])

    def test_does_not_even_create_the_directory(self) -> None:
        # A reappearing directory is the visible symptom, so assert on it directly.
        self._fire_every_writer()
        self.assertFalse(self.sb.exists())

    def test_still_records_when_enrolled_and_exposed(self) -> None:
        # The gate must not silence a workspace that IS being observed.
        self._enrol()
        self._fire_every_writer()
        written = self._files()
        self.assertIn("toolu_x.json", written)
        self.assertIn("hooks.log", written)

    def test_still_logs_when_enrolled_but_not_exposed(self) -> None:
        # The diagnostic exists for exactly this case: an enrolled workspace whose
        # events aren't arriving, where someone needs to see WHY they were dropped.
        self._enrol()
        (self.sb / "state.json").write_text(json.dumps({"seeded": [], "exposed": []}))
        sb_notify.record_user_prompt_signal(self.cwd, self.agent, "hi")
        log = (self.sb / "hooks.log")
        self.assertTrue(log.is_file())
        self.assertIn("not-exposed", log.read_text())


if __name__ == "__main__":
    unittest.main()
