"""Forwarding stub: `python -m multiverse.compaction ACTION [--agent-dir DIR] ...`.

Tree compaction is the `compaction` checkout plugin, whose helper actions run as
`python -m multiverse.plugins run compaction ACTION ...`; its own Claude
directory and new launches' PreCompact guard call that. This path is still
called by the multiverse mod's transition copy of the compaction hooks and by
the PreCompact guard of every launch from before the move (its --settings
names `-m multiverse.compaction guard`), so it forwards there, in the agent's
universe. Remove it only once no running launch's guard names this path, which
outlasts the mod's copy.
"""
import os
from pathlib import Path
import sys


def universe_of(argv):
    """The universe of the agent directory the arguments (or MV2_AGENT_DIR) name, or None."""
    directory = os.environ.get('MV2_AGENT_DIR')
    if '--agent-dir' in argv and argv.index('--agent-dir') + 1 < len(argv):
        directory = argv[argv.index('--agent-dir') + 1]
    if not directory:
        return None
    root = Path(directory).resolve().parent.parent.parent
    return root if (root / 'universe.json').is_file() else None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    from multiverse.plugins.__main__ import main as plugins
    root = universe_of(argv)
    return plugins([*(['-u', str(root)] if root else []), 'run', 'compaction', *argv])


if __name__ == '__main__':
    sys.exit(main())
