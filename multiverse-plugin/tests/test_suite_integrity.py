"""Guards the test-running convention itself.

Every test file here is run as a script (``python tests/test_x.py``), so a file
without an ``if __name__ == "__main__": unittest.main()`` guard imports, defines its
classes, runs nothing, prints nothing, and exits 0 — indistinguishable from passing.
``test_tmux_attach.py`` sat like that with 8 tests in it, on the module the terminal
delivery paths depend on.

That defect is invisible in review: the file is correct, the tests are correct, and
the only fault is the absence of something nothing points at. So it can't be caught
by looking harder — it has to be encoded. This file is that encoding: the convention
now fails loudly when it's broken instead of being remembered.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# The guard as every other file in this directory writes it.
_MAIN_GUARD_RE = re.compile(
    r"^if\s+__name__\s*==\s*[\"']__main__[\"']\s*:\s*$", re.MULTILINE
)
# ``async def`` counts — IsolatedAsyncioTestCase files are the ones this first
# got wrong, and a check that misses them reports a file as empty when it is not.
_TEST_DEF_RE = re.compile(r"^\s+(?:async\s+)?def test_\w+", re.MULTILINE)


def _test_files() -> list[Path]:
    return sorted(p for p in TESTS_DIR.glob("test_*.py") if p.name != Path(__file__).name)


class SuiteIntegrityTests(unittest.TestCase):
    def test_every_test_file_runs_when_invoked_as_a_script(self) -> None:
        missing = [
            p.name for p in _test_files()
            if not _MAIN_GUARD_RE.search(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            missing, [],
            "these files have no __main__ guard, so running them as scripts executes "
            "nothing and exits 0 — a silent pass. Add:\n\n"
            '    if __name__ == "__main__":\n        unittest.main()\n',
        )

    def test_every_test_file_actually_defines_tests(self) -> None:
        # A file with a guard but no test methods is the same silent zero by another
        # route: it reports "Ran 0 tests ... OK".
        empty = [
            p.name for p in _test_files()
            if not _TEST_DEF_RE.search(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            empty, [], "these files define no test methods and would report a vacuous OK"
        )

    def test_this_guard_is_not_itself_vacuous(self) -> None:
        # The check has the same silent-success property as the thing it checks: a
        # discovery bug that found no files would report a clean bill of health from a
        # check that measured nothing. Assert it is actually looking at something.
        files = _test_files()
        self.assertGreater(
            len(files), 5, f"expected to inspect the suite, found {len(files)} files"
        )


if __name__ == "__main__":
    unittest.main()
