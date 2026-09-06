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
from typing import Iterable, Optional


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


# A successor inherits its predecessor's name and takes the next
# generation: "Quasar" hands off to "Quasar II", which hands off to
# "Quasar III". Roman numerals because a lineage is a succession, not a
# version number, and "Quasar 2" reads like a second copy.
_ROMAN_UNITS = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)
_ROMAN_DIGITS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def to_roman(value: int) -> str:
    """``4`` → ``"IV"``. Values below 1 have no numeral; they return ``""``."""
    if value < 1:
        return ""
    out = []
    for amount, numeral in _ROMAN_UNITS:
        while value >= amount:
            out.append(numeral)
            value -= amount
    return "".join(out)


def from_roman(text: str) -> Optional[int]:
    """``"IV"`` → ``4``, or ``None`` when ``text`` isn't a canonical numeral.

    Round-trips through :func:`to_roman` rather than accepting anything
    parseable, so "IIII" and "VV" are rejected — an agent legitimately
    named "MIX" or "DID" must not read as a generation.
    """
    if not text or any(ch not in _ROMAN_DIGITS for ch in text):
        return None
    total = 0
    previous = 0
    for ch in reversed(text):
        digit = _ROMAN_DIGITS[ch]
        total += digit if digit >= previous else -digit
        previous = max(previous, digit)
    if total < 1 or to_roman(total) != text:
        return None
    return total


def split_generation(name: str) -> tuple[str, int]:
    """Split ``"Quasar III"`` into ``("Quasar", 3)``.

    A name with no trailing numeral is the first of its line, so
    ``"Quasar"`` is ``("Quasar", 1)``.
    """
    stem, sep, tail = name.rpartition(" ")
    if not sep:
        return name, 1
    generation = from_roman(tail)
    if generation is None or not stem.strip():
        return name, 1
    return stem, generation


def successor_name(predecessor: str, taken: Iterable[str] = ()) -> str:
    """The name a hand-off should offer for ``predecessor``'s successor.

    Walks forward past generations already in use, so handing off from
    "Quasar" while "Quasar II" is still around offers "Quasar III"
    rather than a name the workspace would reject.
    """
    predecessor = (predecessor or "").strip()
    if not predecessor:
        return ""
    stem, generation = split_generation(predecessor)
    used = {n.casefold() for n in taken}
    while True:
        generation += 1
        candidate = f"{stem} {to_roman(generation)}"
        if candidate.casefold() not in used:
            return candidate
