"""Detached completion of dashboard-requested session teardown."""

from __future__ import annotations

import argparse
import time

from . import sessions
from .atomic_files import atomic_write_json, exclusive_file_lock


def finish_ending_session(session_id: str, delay: float = 0.0) -> int:
    """Finish one durable ``ending`` request, serialized across monitors."""
    if delay > 0:
        time.sleep(delay)

    lock_path = sessions.MULTIVERSE_DIR / "teardown_locks" / f"{session_id}.lock"
    with exclusive_file_lock(lock_path):
        session = sessions.load_session(session_id)
        if session is None:
            return 0
        if session.status != "ending" or not session.ending_marker_path.is_file():
            return 0

        error_path = session.dir / "ending_error.json"
        error_path.unlink(missing_ok=True)
        try:
            sessions.kill_session(session)
        except Exception as exc:  # noqa: BLE001 - persist failure for the monitor
            atomic_write_json(
                error_path,
                {"error": str(exc), "ts": time.time()},
            )
            return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()
    raise SystemExit(finish_ending_session(args.session_id, args.delay))


if __name__ == "__main__":
    main()
