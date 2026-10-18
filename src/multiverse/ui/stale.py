"""Has mv2's own code changed on disk since this dashboard started?

A dashboard runs for hours, and mv2's source can change under it (an update, a merge, a checkout). Python
keeps the modules it already loaded, but imports the rest from disk when they are first needed, so an action
can pair a new module with an old one it imports from and fail with an error such as
`cannot import name X from multiverse.harnesses.types`. Nothing is wrong with the action: the dashboard is
running old code. This module notices the change, so the dashboard can say so instead of showing the raw error.
"""
import os
from pathlib import Path

# What a mix of old and new modules raises: a name that is not there yet, or a call whose shape changed.
SKEW_ERRORS = (ImportError, AttributeError, TypeError, NameError)


def fingerprint(root):
    """{path below root: (modified time in ns, size)} of every .py file under `root`."""
    found, pending = {}, [str(root)]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name != "__pycache__":
                            pending.append(entry.path)
                    elif entry.name.endswith(".py"):
                        try:
                            info = entry.stat()
                        except OSError:
                            continue  # gone since the scan began: the next look reports it
                        found[os.path.relpath(entry.path, root)] = (info.st_mtime_ns, info.st_size)
        except OSError:
            continue  # a folder that cannot be read is not evidence of a change
    return found


class CodeWatch:
    """The fingerprint of mv2's code when the dashboard started, and whether it differs now.

    Once the code has changed it stays changed: the dashboard still runs what it loaded, and putting the
    files back would not undo that.
    """

    def __init__(self, root=None):
        if root is None:
            import multiverse
            root = Path(multiverse.__file__).resolve().parent
        self.root = Path(root)
        self.baseline = fingerprint(self.root)
        self.changed_files = []

    @property
    def stale(self):
        return bool(self.changed_files)

    def check(self):
        """Look at the disk now; True when any .py file was added, removed or modified since the start."""
        if not self.changed_files:
            now = fingerprint(self.root)
            self.changed_files = sorted(name for name in now.keys() | self.baseline.keys()
                                        if now.get(name) != self.baseline.get(name))
        return self.stale

    def explain(self, error):
        """The text to show for `error`: the plain one, or, when it is what old code mixed with new code raises
        and the code has changed, a sentence that says what to do."""
        text = str(error)
        if isinstance(error, SKEW_ERRORS) and self.check():
            return f"mv2 changed since this dashboard started; quit and reopen it ({type(error).__name__}: {text})"
        return text
