"""Name pool for multiverse sessions.

Reads ``data/names.csv`` (columns: ``name, gender``) and exposes helpers
for picking a random unused name. Gender is carried through but not
consulted yet.
"""

from __future__ import annotations

import csv
import os
import random
from pathlib import Path
from typing import Optional


_CACHE: Optional[list[tuple[str, str]]] = None


def _names_csv_path() -> Path:
    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT")
    if plugin_root:
        return Path(plugin_root) / "data" / "names.csv"
    return Path(__file__).resolve().parent.parent / "data" / "names.csv"


def load_names() -> list[tuple[str, str]]:
    """Return the full ``(name, gender)`` list, cached for the process."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = _names_csv_path()
    out: list[tuple[str, str]] = []
    try:
        with open(path, "r", newline="") as fh:
            reader = csv.reader(fh, skipinitialspace=True)
            for row in reader:
                if not row:
                    continue
                name = row[0].strip()
                if not name:
                    continue
                gender = row[1].strip() if len(row) > 1 else ""
                out.append((name, gender))
    except OSError:
        pass
    _CACHE = out
    return out


def all_names() -> list[str]:
    return [n for n, _ in load_names()]


def pick_random_name(exclude: set[str]) -> Optional[str]:
    """Return a random name not in ``exclude``, or None if all are used."""
    available = [n for n in all_names() if n not in exclude]
    if not available:
        return None
    return random.choice(available)
