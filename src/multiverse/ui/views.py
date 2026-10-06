"""A universe's `views/default.json`: title, accent, agent columns and local panels."""
from hashlib import sha256
import re

from .. import universe
from ..store import read
from .panels import normalize_widgets
COLORS = ("#94e2d5", "#cba6f7", "#89b4fa", "#f9e2af", "#f5c2e7")
COLUMNS = {"name": ("Agent", 20), "harness": ("Harness", 9), "live": ("Live", 9),
           "context": ("Context", 10), "cost": ("Cost", 7), "status": ("Status", 12), "phase": ("Phase", 10), "summary": ("Working on", 48),
           "id": ("ID", 12), "universe": ("Universe", 24), "task": ("Task", 48),
           "updated_at": ("Updated", 26)}


DEFAULT_COLUMNS = ["name", "harness", "live", "status", "context", "phase", "summary"]
# Former column keys that views written for older versions still use.
COLUMN_ALIASES = {"activity": "status", "tokens": "context"}


def view(root):
    path = root / "views" / "default.json"
    try:
        config = read(path) if path.is_file() else {}
        if not isinstance(config, dict):
            raise ValueError("View must be a JSON object")
    except (OSError, ValueError) as error:
        config = {"widgets": [{"type": "markdown", "title": "View configuration error", "text": str(error)}]}
    accent = config.get("accent", "auto")
    if accent == "auto":
        identity = read(root / "universe.json")["id"]
        index = 1 + int(sha256(identity.encode()).hexdigest()[:8], 16) % (len(COLORS) - 1)
        accent = COLORS[index] if universe.parent(root) else COLORS[0]
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = COLORS[0]
    requested = config.get("columns", DEFAULT_COLUMNS)
    if not isinstance(requested, list):
        requested = DEFAULT_COLUMNS
    requested = [COLUMN_ALIASES.get(key, key) if isinstance(key, str) else key for key in requested]
    selected = list(dict.fromkeys(key for key in requested if isinstance(key, str) and key in COLUMNS))
    return {"columns": selected or DEFAULT_COLUMNS, "accent": accent,
            "title": str(config.get("title", "")), "widgets": normalize_widgets(config.get("widgets", []))}


def columns(root):
    return view(root)["columns"]
