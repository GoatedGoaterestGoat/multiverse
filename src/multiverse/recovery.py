"""Former module of automatic reconnect (now multiverse.runtime.recovery).

Runners started before the package restructure start `python -m multiverse.recovery`
after a relay loss, so it stays until no such agent remains.
"""
import sys
from pathlib import Path

if __name__ == "__main__":
    from multiverse.runtime.recovery import run
    run(Path(sys.argv[1]), sys.argv[2])
