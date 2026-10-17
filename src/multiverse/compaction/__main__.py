"""`python -m multiverse.compaction ACTION [--agent-dir DIR] ...`, for Claude launches from before compaction became a plugin.

`guard` lets Claude compact: such a launch no longer has the hooks that would
answer /compact, so refusing it would leave the agent unable to compact at all.
It says to restart the agent, which brings tree compaction back. Any other action
goes to the compaction plugin (`python -m multiverse.plugins run compaction`)
when it is installed and on in the agent's universe. Otherwise `install` answers
with a failed event that says to restart (an old copy of the hooks still in
memory shows it), and the rest do nothing.
"""
import json
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


RESTART = 'restart this agent to use tree compaction'


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['guard']:
        print(f'mv2 compaction: {RESTART}', file=sys.stderr)
        return 0
    root = universe_of(argv)
    if root is not None and installed(root):
        from multiverse.plugins.__main__ import main as plugins
        return plugins(['-u', str(root), 'run', 'compaction', *argv])
    if argv[:1] == ['install']:
        print(json.dumps({'event': 'failed', 'message': RESTART}), flush=True)
        return 1
    return 0


def installed(root):
    """Whether the compaction plugin is installed and on in the universe at `root`."""
    try:
        from multiverse.plugins import entry
        return entry(root, 'compaction').state in ('on', 'loaded')
    except Exception:  # not installed here, or the plugins cannot be read
        return False


if __name__ == '__main__':
    sys.exit(main())
