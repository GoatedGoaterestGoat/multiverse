"""Drain process groups created exclusively for disposable native fixtures."""
import os
import signal
import subprocess
import time


class PrivateProcessGroup:
    def __init__(self, pid):
        # Capture ownership while the fixture's known leader is still alive.
        # A daemon stop response can precede orphaned registry/git helper exits.
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise AssertionError('A private process group needs a known leader PID')
        if os.getpgid(pid) != pid or pid == os.getpgrp():
            raise AssertionError('Native fixture must own a separate process group')
        self.pgid = pid
        self.closed = False

    def _members(self):
        output = subprocess.check_output(['ps', '-axo', 'pid=,pgid='], text=True, timeout=5)
        return [int(fields[0]) for line in output.splitlines()
                if len(fields := line.split()) == 2 and int(fields[1]) == self.pgid]

    def close(self, *, reap=lambda: None, timeout=2):
        if self.closed:
            return  # Never signal a numeric group again after observing it empty.
        for ending in (signal.SIGTERM, signal.SIGKILL):
            reap()
            if not self._members():
                self.closed = True
                return
            try:
                os.killpg(self.pgid, ending)
            except (ProcessLookupError, PermissionError):
                # macOS can return EPERM for an already empty process group.
                # Only the process table establishes that the group is gone.
                if self._members():
                    raise
            deadline = time.monotonic() + timeout
            while True:
                reap()
                if not self._members():
                    self.closed = True
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(.02)
        raise AssertionError(f'Private native helpers did not exit: {self._members()}')
