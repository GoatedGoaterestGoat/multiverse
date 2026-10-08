"""Trash: removed agents and subverses and replaced definition files, kept until emptied."""
from datetime import datetime, timezone
import shutil

from ..store import read
from . import exact, tree

TRASH = "trash"
RECORD = ".removed.json"


def entry(container, name):
    """A new, unused path in the trash of `container`, named after `name`."""
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-") + name
    path, number = container / TRASH / base, 2
    while path.exists():
        path, number = container / TRASH / f"{base}-{number}", number + 1
    path.parent.mkdir(exist_ok=True)
    return path


def contents(root, empty=False):
    """List, or permanently delete, removed entries across this universe tree."""
    entries = []
    for scope in tree(exact(root)):
        for record in sorted((scope / TRASH).glob("*/" + RECORD)):
            entries.append({**read(record), "trash": str(record.parent)})
    if empty:
        for item in entries:
            shutil.rmtree(item["trash"])
    return {"entries": entries, "emptied": empty}
