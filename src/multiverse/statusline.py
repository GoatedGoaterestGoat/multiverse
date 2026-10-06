"""Former path of the tmux status line script (now multiverse.terminal.statusline).

tmux sessions launched before the package restructure run this file by path, so it
stays until no such agent remains. tmux runs it with `python -S`: standard library only.
"""
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from multiverse.terminal.statusline import main
    main()
