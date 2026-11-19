"""A universe's `views/default.json`: title, accent, agent columns and local panels."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from .. import universe
from ..plugins.loader import CORE_COLUMN_ALIASES
from ..store import is_file_replaced, open_regular, read, retry_missing
from . import theme
from .columns import COLUMNS, MARKS
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
        try:
            config = retry_missing(lambda: read(path)) if is_file_replaced(path) else {}
        except FileNotFoundError:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("View must be a JSON object")
    except (OSError, ValueError) as error:
        config = {"widgets": [error_widget("View configuration error", error)]}
    accent = config.get("accent", "auto")
    if accent == "auto":
        identity = retry_missing(lambda: read(root / "universe.json"))["id"]
        index = 1 + int(sha256(identity.encode()).hexdigest()[:8], 16) % (len(theme.ACCENTS) - 1)
        accent = theme.ACCENTS[index] if universe.parent(root) else theme.ACCENTS[0]
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = theme.ACCENTS[0]
    requested = config.get("columns", DEFAULT_COLUMNS)
    if not isinstance(requested, list):
        requested = DEFAULT_COLUMNS
    requested = [_unaliased(key) if isinstance(key, str) else key for key in requested]
    # A plugin column stays named while its plugin is off: `plugin:key` always, a short key while a plugin uses it.
    offered = (_plugin_column_keys(root) if any(isinstance(key, str) and key not in COLUMNS and key not in MARKS
                                                for key in requested) else set())
    selected = list(dict.fromkeys(key for key in requested if isinstance(key, str)
                                  and (key in COLUMNS or key in MARKS or key in offered or _core(key) or _qualified(key))))
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
    if not any(key not in MARKS for key in selected):  # nothing to show but a mark: the default columns, with it
        selected = [*DEFAULT_COLUMNS, *selected]
    return {"columns": selected, "accent": accent, "replace": replace,
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


class ViewChanged(Exception):
    """The view file changed on disk after the Columns screen read it, so nothing was written."""


def view_file(root):
    return Path(root) / "views" / "default.json"


def _view_bytes(path):
    """The view file's bytes, or None when there is none. Never waits on a special file such as a FIFO: it is
    refused (OSError), as a file that isn't a regular one can't be edited here."""
    try:
        with open_regular(path) as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def editable_columns(root):
    """The view file's own column keys for the Columns screen, with former names replaced, and the file's bytes as
    read (None when there is no file): (keys, read). Raises ValueError or OSError, saying why, when the file can't be
    edited: not a regular file, not plain UTF-8 JSON, not an object, or its columns not a list of names."""
    path = view_file(root)
    try:
        data = _view_bytes(path)
    except OSError as error:
        raise ValueError(f"{path.name} isn't a regular file: {error.strerror or error}") from None
    if data is None:
        return list(DEFAULT_COLUMNS), None
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path.name} starts with a byte-order mark; save it as plain UTF-8 to edit it here")
    try:
        config = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError(f"{path.name} isn't UTF-8; save it as plain UTF-8 to edit it here") from None
    except ValueError as error:
        raise ValueError(f"{path.name} isn't valid JSON: {error}") from None
    if not isinstance(config, dict):
        raise ValueError(f"{path.name} isn't a JSON object")
    keys = config.get("columns", DEFAULT_COLUMNS)
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError(f"The columns in {path.name} aren't a list of names")
    return list(dict.fromkeys(_unaliased(key) for key in keys)), data


def _space(text, index):
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _columns_span(text):
    """Where the top-level "columns" value is in a JSON object's text, as (start, end), or None, plus where the
    first key starts (for inserting one). The text is already known to parse."""
    decoder, index, span = json.JSONDecoder(), _space(text, 0) + 1, None
    index = _space(text, index)
    first = index
    while index < len(text) and text[index] != "}":
        key, index = json.decoder.scanstring(text, index + 1)
        index = _space(text, _space(text, index) + 1)  # past the colon
        _, end = decoder.raw_decode(text, index)
        if key == "columns":
            span = (index, end)  # the last one wins, as when it is read
        index = _space(text, end)
        if text[index] == ",":
            index = _space(text, index + 1)
    return span, first


def write_columns(root, keys, read_as):
    """Write `keys` as the view file's columns, changing only that value: the file's other keys, their order and
    formatting stay as they were. `read_as` is the file's bytes when the screen read it (None: it had no file).
    Raises ViewChanged, writing nothing, when the file is no longer those bytes. The file is replaced whole, so a
    reader sees the old file or the new one, never a partial write."""
    path = view_file(root)

    def current():
        return _view_bytes(path)

    if current() != read_as:
        raise ViewChanged(path)
    value = json.dumps(list(keys), ensure_ascii=False)
    if read_as is None:
        text = '{\n  "columns": ' + value + "\n}\n"  # the bundled templates' layout
    else:
        old = read_as.decode()
        span, first = _columns_span(old)
        if span:
            text = old[:span[0]] + value + old[span[1]:]
        elif old[first] == "}":  # an empty object
            text = old[:first] + f'"columns": {value}' + old[first:]
        else:
            line = old.rfind("\n", 0, first)
            indent = old[line + 1:first] if line >= 0 and not old[line + 1:first].strip() else ""
            text = old[:first] + f'"columns": {value},' + ("\n" + indent if indent or line >= 0 else " ") + old[first:]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        if read_as is not None:
            os.chmod(temp, path.stat().st_mode & 0o7777)
        if current() != read_as:  # checked again just before the replace, to keep the window small
            raise ViewChanged(path)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def plugin_column_choices(root):
    """{qualified key: title} of the columns the plugins on anywhere in this tree register, for the Columns screen."""
    try:
        from .. import plugins
        found = {}
        for scope in universe.tree(root):
            for loaded in plugins.loader.active(scope, wait=plugins.loader.REFRESH_WAIT):
                for key, column in loaded.registrations.columns.items():
                    found.setdefault(f"{loaded.name}:{key}", str(getattr(column, "title", "") or key))
        return found
    except Exception:
        return {}
