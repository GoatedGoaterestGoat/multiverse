"""Fail any test that writes under the real ~/.multiverse, ~/.claude or ~/.codex, to ~/.claude.json, or under this
checkout's own .multiverse.

The guard lives in multiverse.plugins.testing, where PluginTestCase installs it for plugin tests (mv2's and the
plugins' own). mv2's other tests and the parallel runner install it from here:

    home_safety.install()        # once per process, before a fixture moves HOME; returns the protected directory
    home_safety.running(name)    # the test running now, or None between tests
    home_safety.check()          # AssertionError for refused writes not reported yet
    home_safety.violations()     # everything refused so far, as dicts

A refused write raises PermissionError where it happened, so the real home never changes; one that no check()
reported makes the process exit 1 at its end.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import check, install, running, violations  # noqa: E402,F401
