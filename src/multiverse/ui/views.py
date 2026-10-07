"""A universe's `views/default.json`: title, accent, agent columns and local panels."""
from hashlib import sha256
import re

from .. import universe
from ..store import read
from . import theme
from .columns import COLUMNS
from .panels import error_widget, normalize_widgets

DEFAULT_COLUMNS = ["name", "harness", "live", "status", "context", "phase", "summary"]
# Former column keys that views written for older versions still use.
COLUMN_ALIASES = {"activity": "status", "tokens": "context"}


def view(root):
    """The view's settings. Never raises: a view that cannot be read shows as an error
    widget with default settings, so it cannot keep the agent list from loading."""
    try:
        return _read_view(root)
    except Exception as error:
        return {"columns": DEFAULT_COLUMNS, "accent": theme.ACCENTS[0], "title": "",
                "widgets": [error_widget("View configuration error", error)]}


def _read_view(root):
    path = root / "views" / "default.json"
    try:
        config = read(path) if path.is_file() else {}
        if not isinstance(config, dict):
            raise ValueError("View must be a JSON object")
    except (OSError, ValueError) as error:
        config = {"widgets": [error_widget("View configuration error", error)]}
    accent = config.get("accent", "auto")
    if accent == "auto":
        identity = read(root / "universe.json")["id"]
        index = 1 + int(sha256(identity.encode()).hexdigest()[:8], 16) % (len(theme.ACCENTS) - 1)
        accent = theme.ACCENTS[index] if universe.parent(root) else theme.ACCENTS[0]
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = theme.ACCENTS[0]
    requested = config.get("columns", DEFAULT_COLUMNS)
    if not isinstance(requested, list):
        requested = DEFAULT_COLUMNS
    requested = [COLUMN_ALIASES.get(key, key) if isinstance(key, str) else key for key in requested]
    selected = list(dict.fromkeys(key for key in requested if isinstance(key, str) and key in COLUMNS))
    return {"columns": selected or DEFAULT_COLUMNS, "accent": accent,
            "title": str(config.get("title", "")), "widgets": normalize_widgets(config.get("widgets", []))}


def columns(root):
    return view(root)["columns"]
