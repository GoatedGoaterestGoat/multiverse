"""One test's environment: its own HOME, and nothing the shell or agent running the suite would pass on.

    from isolation import isolate

    def setUp(self):
        isolate(self)   # first in setUp, before anything reads HOME or starts a process; returns the HOME

The processes a test starts inherit its HOME, so neither the test nor the runners, relays and CLI calls it
starts can write the real ~/.multiverse. BaseTest calls it; a TestCase that is not a BaseTest can too.

    track(tmux_socket=socket)   # a tmux server, or track(pid=pid) a daemon, started outside the test's process group

so that tests/run.py ends it even if the test is stopped before its cleanups run.
"""
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

import home_safety  # captures the real ~/.multiverse at import, before any test moves HOME
import tracking

# What a test must not inherit from the shell or agent running the suite: the agent's identity, universe,
# tmux server and messaging socket, and the harnesses' homes. MV2_CLI and MV2_PYTHON name this checkout's code.
KEPT = ("MV2_CLI", "MV2_PYTHON")
INHERITED_PREFIXES = ("MV2_", "CODEX_", "CLAUDE_CODE_")
INHERITED = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR", "TMUX", "TMUX_PANE")
STARTED_WITH = dict(os.environ)  # before any test changed it: a value a test set itself is the test's to keep
TRACKED = os.environ.get("MV2_TEST_TRACKED")  # where tests/run.py reads what to end after this unit


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
    test.addCleanup(home_safety.check, test.id())  # registered first, so it runs last: this test's refused writes
    test.addCleanup(home_safety.running, None)
    temporary = tempfile.TemporaryDirectory(prefix="mv2-test-home-", dir="/tmp")
    test.addCleanup(temporary.cleanup)
    test.enterContext(mock.patch.dict(os.environ, isolated_environment(Path(temporary.name).resolve()),
                                      clear=True))
    return Path(os.environ["HOME"])


def track(*, tmux_socket=None, pid=None):
    """Tell tests/run.py about a tmux server (by its socket) or a process (by PID) this test started outside its own
    process group, where the runner's kill cannot reach. The runner ends it after the unit, even if the test was
    stopped or killed before its cleanups ran; for a process that leads its own group (a daemon), the whole group,
    helpers included, even once the leader has exited. Does nothing when the tests run some other way."""
    if not TRACKED:
        return
    from multiverse.runtime.process import process_started  # home_safety put this checkout's src on the path
    record = {}
    if tmux_socket is not None:
        record["tmux_socket"] = str(tmux_socket)
    if pid is not None and (started := process_started(pid)):
        record.update(pid=pid, started=started)  # its start time, so that a later process with its PID is spared
        try:
            if os.getpgid(pid) == pid:
                record["group"] = pid  # it leads its group: tests/run.py records who joins it, and ends them all
        except OSError:
            pass
    if record:
        with open(TRACKED, "a") as out:
            out.write(json.dumps(record) + "\n")
        if "group" in record:
            tracking.sample(TRACKED)  # who is in it now, while its leader is known to be alive
