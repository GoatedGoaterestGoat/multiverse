"""A universe's `views/default.json`: title, accent, agent columns and local panels."""
from hashlib import sha256
import re

from .. import universe
from ..plugins.loader import CORE_COLUMN_ALIASES
from ..store import read
from . import theme
from .columns import COLUMNS
from .panels import error_widget, normalize_widgets
from .plugin_panels import option_of

DEFAULT_COLUMNS = ["name", "harness", "live", "status", "context", "phase", "summary"]
# Former column keys that views written for older versions still use (the plugin API reads the same table).
COLUMN_ALIASES = CORE_COLUMN_ALIASES


def view(root):
    """The view's settings. Never raises: a view that cannot be read shows as an error
    widget with default settings, so it cannot keep the agent list from loading."""
    try:
        return _read_view(root)
    except Exception as error:
        return {"columns": DEFAULT_COLUMNS, "accent": theme.ACCENTS[0], "title": "", "replace": True,
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
    requested = [_unaliased(key) if isinstance(key, str) else key for key in requested]
    # A plugin column stays named while its plugin is off: `plugin:key` always, a short key while a plugin uses it.
    offered = _plugin_column_keys(root) if any(isinstance(key, str) and key not in COLUMNS for key in requested) else set()
    selected = list(dict.fromkeys(key for key in requested if isinstance(key, str)
                                  and (key in COLUMNS or key in offered or _core(key) or _qualified(key))))
    widgets = normalize_widgets(config.get("widgets", []), root)
    replace = config.get("replace", True)  # false: no plugin column stands in for a core one in this view
    if not isinstance(replace, bool):
        replace = True
        widgets = [*widgets, error_widget("View configuration error", ValueError("replace must be true or false"))]
    try:
        plugin_panels = option_of(config.get("plugin_panels"))
    except ValueError as error:
        plugin_panels = "auto"
        widgets = [*widgets, error_widget("View configuration error", error)]
    return {"columns": selected or DEFAULT_COLUMNS, "accent": accent, "replace": replace,
            "title": str(config.get("title", "")), "widgets": widgets, "plugin_panels": plugin_panels}


def columns(root):
    return view(root)["columns"]


def _unaliased(key):
    """A view's column key with former names replaced, `core:KEY` included."""
    if key.startswith("core:"):
        return "core:" + COLUMN_ALIASES.get(key[5:], key[5:])
    return COLUMN_ALIASES.get(key, key)


def _core(key):
    """`core:KEY` for a core column: core's own column, never one a plugin's column stands in for."""
    return key.startswith("core:") and key[5:] in COLUMNS


def _qualified(key):
    plugin, separator, name = key.partition(":")
    return bool(separator and plugin and name and plugin != "core" and re.fullmatch(r"[a-z][a-z0-9-]*", plugin))


def _plugin_column_keys(root):
    """Short and qualified keys of the columns plugins on anywhere in this tree register (two plugins' same key included)."""
    try:
        from .. import plugins
        keys = set()
        for scope in universe.tree(root):
            for loaded in plugins.loader.active(scope, wait=plugins.loader.REFRESH_WAIT):
                for key in loaded.registrations.columns:
                    keys.update((key, f"{loaded.name}:{key}"))
        return keys
    except Exception:
        return set()
