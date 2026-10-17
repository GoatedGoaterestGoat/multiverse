"""One test's environment: its own HOME, and nothing the shell or agent running the suite would pass on.

    from isolation import isolate

    def setUp(self):
        isolate(self)   # first in setUp, before anything reads HOME or starts a process; returns the HOME

The processes a test starts inherit its HOME, so neither the test nor the runners, relays and CLI calls it
starts can write the real ~/.multiverse. BaseTest calls it; a TestCase that is not a BaseTest can too.
"""
import os
from pathlib import Path
import tempfile
from unittest import mock

import home_safety  # captures the real ~/.multiverse at import, before any test moves HOME

# What a test must not inherit from the shell or agent running the suite: the agent's identity, universe,
# tmux server and messaging socket, and the harnesses' homes. MV2_CLI and MV2_PYTHON name this checkout's code.
KEPT = ("MV2_CLI", "MV2_PYTHON")
INHERITED_PREFIXES = ("MV2_", "CODEX_", "CLAUDE_CODE_")
INHERITED = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR", "TMUX", "TMUX_PANE")
STARTED_WITH = dict(os.environ)  # before any test changed it: a value a test set itself is the test's to keep


def _inherited(key, value):
    return (key not in KEPT and (key.startswith(INHERITED_PREFIXES) or key in INHERITED)
            and STARTED_WITH.get(key) == value)


def isolated_environment(home):
    """os.environ without what was inherited, and HOME `home` unless the test already gave itself one."""
    environment = {key: value for key, value in os.environ.items() if not _inherited(key, value)}
    if os.environ.get("HOME") == STARTED_WITH.get("HOME"):
        environment["HOME"] = str(home)
    return environment


def isolate(test):
    """Isolate `test` (a TestCase, from its setUp) until it ends; returns the HOME it now has."""
    home_safety.install()
    home_safety.running(test.id())
    test.addCleanup(home_safety.running, None)
    temporary = tempfile.TemporaryDirectory(prefix="mv2-test-home-", dir="/tmp")
    test.addCleanup(temporary.cleanup)
    test.enterContext(mock.patch.dict(os.environ, isolated_environment(Path(temporary.name).resolve()),
                                      clear=True))
    return Path(os.environ["HOME"])
