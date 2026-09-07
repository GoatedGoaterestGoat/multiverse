#!/usr/bin/env python3
"""Publish proof of life for one session, for as long as its wrapper runs.

Started in the background by a session wrapper and killed when the runtime
exits. It exists for the case a dashboard cannot cover: on a shared
``MULTIVERSE_ROOT``, liveness correction sweeps every session under the root,
but a host only vouches for sessions it can see — and it only looks while a
dashboard is open. A remote running agents with no dashboard, or with one open
for a different workspace, would otherwise publish nothing, and another
remote's dashboard would find those rows unvouched-for and bury them.

Exits on its own if the wrapper disappears without killing it. An orphaned
heartbeat writer is worse than none at all: it would keep a dead session
looking alive on every remote, forever, and the row could then only be cleared
by force-kill.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.hosts import HEARTBEAT_INTERVAL_SECONDS, touch_heartbeat  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: heartbeat.py <session-state-dir>\n")
        return 2
    session_dir = Path(argv[0])
    wrapper = os.getppid()
    while True:
        # Re-parenting means the wrapper is gone; on Linux the new parent is
        # init (or a subreaper), on macOS it is 1. Either way it is no longer
        # the process whose life this heartbeat is reporting.
        if os.getppid() != wrapper:
            return 0
        touch_heartbeat(session_dir)
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
