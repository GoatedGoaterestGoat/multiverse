"""Former path of the pane runner (now multiverse.runtime.runner).

Runners started before the package restructure rerun this file by path to clean
up their tmux wrapper, so it stays until no such agent remains.
"""
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from multiverse.runtime import runner
    if sys.argv[1:2] == ["--cleanup-wrapper"]:
        runner.cleanup_wrapper(Path(sys.argv[2]))
    else:
        sys.exit(runner.main())
