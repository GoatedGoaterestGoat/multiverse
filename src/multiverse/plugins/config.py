"""A universe's `plugins.json`: which plugins are on there, their access, the dashboard theme and event retention.

    {"schema": 1, "enabled": {"compaction": true}, "access": {"compaction": "spend"},
     "access_ceiling": null, "theme": null}

`access` records the level a person granted a plugin here (1.13); a plugin that
is on with none recorded has what its manifest requests. `access_ceiling` caps
every plugin here (observe makes a read-only universe).

It is the only on/off switch for a plugin. A missing file, or a plugin it does
not name, means the plugin's manifest default. There is no inheritance between
a universe and its subverses. An unusable file never stops anything: it reads
as the defaults, with the reason for `mv2 plugins` and `mv2 doctor`.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path

from ..store import lock, write
from . import access as levels
from .manifest import check_name

FILE = "plugins.json"
# "keys" chose between plugins' dashboard keys, which are gone; a file that still has it reads as before.
KNOWN_FIELDS = {"schema", "enabled", "theme", "keys", "events", "access", "access_ceiling"}


@dataclass(frozen=True)
class Config:
    enabled: dict = field(default_factory=dict)
    theme: str | None = None
    problem: str | None = None
    events: dict = field(default_factory=dict)  # journal retention: retain_days, max_mb
    exists: bool = False
    unknown: tuple = ()  # top-level fields a newer mv2 wrote: kept in the file, not used here
    access: dict = field(default_factory=dict)  # plugin name -> the level granted here
    ceiling: str | None = None  # access_ceiling: no plugin here has more

    def is_on(self, name, default="off"):
        value = self.enabled.get(name)
        return default == "on" if value is None else value


def path(root):
    return Path(root) / FILE


def _raw(root):
    """The file's JSON object, {} when it is missing; ValueError when it cannot be used."""
    try:
        text = path(root).read_text()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(f"{path(root)}: {error.strerror or error}") from None
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ValueError(f"{path(root)}: not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path(root)}: must be a JSON object")
    return data


def parse(data, where=FILE):
    """A Config from the file's object; ValueError naming the field otherwise."""
    if data.get("schema", 1) != 1:
        raise ValueError(f"{where}: schema must be 1")
    # Fields a newer mv2 wrote (its plugins still have their switches here) are kept and left alone: refusing the
    # file would turn every plugin to its default. notes() says an update would use them.
    unknown = tuple(sorted(set(data) - KNOWN_FIELDS))
    enabled = data.get("enabled", {})
    if not isinstance(enabled, dict):
        raise ValueError(f"{where}: enabled must map plugin names to true or false")
    for name, value in enabled.items():
        check_name(name, f"enabled: {name!r}")
        if not isinstance(value, bool):
            raise ValueError(f"{where}: enabled.{name} must be true or false")
    theme = data.get("theme")
    if theme is not None:
        check_name(theme, "theme")
    events = data.get("events", {})
    if not isinstance(events, dict) or set(events) - {"retain_days", "max_mb"}:
        raise ValueError(f"{where}: events takes retain_days and max_mb")
    for key, value in events.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100000:
            raise ValueError(f"{where}: events.{key} must be a positive number")
    access = data.get("access", {})
    if not isinstance(access, dict):
        raise ValueError(f"{where}: access must map plugin names to observe, spend or act")
    for name, value in access.items():
        check_name(name, f"access: {name!r}")
        if value not in levels.LEVELS:
            raise ValueError(f"{where}: access.{name} must be observe, spend or act")
    ceiling = data.get("access_ceiling")
    if ceiling is not None and ceiling not in levels.LEVELS:
        raise ValueError(f"{where}: access_ceiling must be observe, spend, act or null")
    return Config(enabled=dict(enabled), theme=theme, events=dict(events), exists=True, unknown=unknown,
                  access=dict(access), ceiling=ceiling)


def load(root):
    """This universe's Config. Never raises: an unusable file reads as the defaults with `problem` set."""
    try:
        data = _raw(root)
        if data is None:
            return Config()
        return parse(data, str(path(root)))
    except ValueError as error:
        return Config(problem=str(error), exists=True)


def set_enabled(root, name, value, grant=None):
    """Turn a plugin on (True) or off (False) here, and record `grant` with it when given, changing nothing
    else in the file."""
    check_name(name)
    if not isinstance(value, bool):
        raise ValueError("A plugin is turned on with true or off with false")
    if grant is not None:
        levels.check(grant, "grant")
    with lock(Path(root) / "state" / ".plugins.lock"):
        data = _raw(root)
        if data is None:
            data = {"schema": 1, "enabled": {}}
        parse(data, str(path(root)))  # refuse to rewrite a file that cannot be used
        data.setdefault("enabled", {})[name] = value
        if grant is not None:
            data.setdefault("access", {})[name] = grant
        write(path(root), data)
    return load(root)


def set_grant(root, name, level):
    """Record the access level granted to a plugin here, changing nothing else in the file."""
    check_name(name)
    levels.check(level, "grant")
    with lock(Path(root) / "state" / ".plugins.lock"):
        data = _raw(root)
        if data is None:
            data = {"schema": 1, "enabled": {}}
        parse(data, str(path(root)))
        data.setdefault("access", {})[name] = level
        write(path(root), data)
    return load(root)
