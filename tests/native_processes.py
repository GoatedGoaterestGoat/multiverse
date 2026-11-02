"""Drain process groups created exclusively for disposable native fixtures."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from unittest.mock import patch

from isolation import track


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
        track(pid=pid)  # tests/run.py ends the group if this test is stopped before close() runs

    def _members(self, *, deadline=None):
        command = ['ps', '-axo', 'pid=,pgid=,stat=']
        # A slow ps is unknown, not an empty group. Retry only that transient
        # failure, sharing close's deadline across both signal phases.
        deadline = time.monotonic() + 30 if deadline is None else deadline
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            try:
                output = subprocess.check_output(command, text=True, timeout=min(5, remaining))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
        return [int(fields[0]) for line in output.splitlines()
                if len(fields := line.split()) == 3 and int(fields[1]) == self.pgid
                and not fields[2].startswith('Z')]

    def close(self, *, reap=lambda: None, timeout=2):
        if self.closed:
            return  # Never signal a numeric group again after observing it empty.
        deadline = time.monotonic() + 30
        for ending in (signal.SIGTERM, signal.SIGKILL):
            reap()
            if not self._members(deadline=deadline):
                self.closed = True
                return
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(['ps', '-axo', 'pid=,pgid=,stat='], 30)
            try:
                os.killpg(self.pgid, ending)
            except (ProcessLookupError, PermissionError):
                # macOS can return EPERM for an already empty process group.
                # Only the process table establishes that the group is gone.
                if self._members(deadline=deadline):
                    raise
            phase_deadline = min(deadline, time.monotonic() + timeout)
            while True:
                reap()
                if not self._members(deadline=deadline):
                    self.closed = True
                    return
                if time.monotonic() >= phase_deadline:
                    break
                time.sleep(.02)
        raise AssertionError(f'Private native helpers did not exit: {self._members(deadline=deadline)}')


class PrivateDaemonGroups:
    """Remember native start results without changing startup/restart behavior."""
    def __init__(self):
        self.groups = {}
        self.lock = threading.Lock()

    def remember(self, result):
        pid = result['pid']
        with self.lock:
            if pid not in self.groups:
                self.groups[pid] = PrivateProcessGroup(pid)

    @contextmanager
    def observe_starts(self):
        from multiverse.runtime import native
        original = native.start_shared_service

        def start(specification, workspace, log_path, unset_env, observe_versions=None):
            try:
                return original(specification, workspace, log_path, unset_env, observe_versions)
            finally:
                # Core writes the native start response before endpoint/version
                # validation. Capture it even when that validation raises.
                try:
                    result, _ = json.JSONDecoder().raw_decode(Path(log_path).read_text().lstrip())
                except (FileNotFoundError, ValueError):
                    pass  # A failed command may have produced no start response.
                else:
                    if isinstance(result, dict) and 'pid' in result:
                        self.remember(result)

        with patch.object(native, 'start_shared_service', start):
            yield

    def close(self, *, reap=lambda: None):
        errors = []
        for group in self.groups.values():
            try:
                group.close(reap=reap)
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]
