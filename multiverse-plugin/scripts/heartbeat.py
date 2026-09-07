#!/usr/bin/env python3
"""Publish proof of life for one session, for as long as its wrapper runs.

Started in the background by a session wrapper and killed when the runtime
exits. It exists for the case a dashboard cannot cover: on a shared
``MULTIVERSE_ROOT``, liveness correction sweeps every session under the root,
but a host only vouches for sessions it can see — and it only looks while a
dashboard is open. A remote running agents with no dashboard, or with one open
for a different workspace, would otherwise publish nothing, and another
remote's dashboard would find those rows unvouched-for and bury them.

Exits on its own once the wrapper is gone. An orphaned heartbeat writer is
worse than none at all: it would keep a dead session looking alive on every
remote, forever, and the row could then only be cleared by force-kill.

The wrapper's PID is passed in rather than read here, because reading it here
is too late. A wrapper that fails between spawning this process and this
process starting is already gone by the time ``getppid()`` first runs, so the
answer would be init — recorded as "the wrapper", matching forever, and
producing exactly the immortal writer this check exists to prevent. Rejecting
PID 1 instead would be wrong too: in a container a wrapper legitimately is
PID 1.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.hosts import HEARTBEAT_INTERVAL_SECONDS, touch_heartbeat  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: heartbeat.py <session-state-dir> <wrapper-pid>\n")
        return 2
    session_dir = Path(argv[0])
    try:
        wrapper = int(argv[1])
    except ValueError:
        sys.stderr.write(f"heartbeat.py: not a pid: {argv[1]!r}\n")
        return 2
    while True:
        # Checked before the first write as well as every later one: the
        # wrapper may already have died while this process was starting, and
        # a heartbeat published on its behalf after that is a lie that only
        # force-kill can retract.
        if os.getppid() != wrapper:
            return 0
        touch_heartbeat(session_dir)
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
