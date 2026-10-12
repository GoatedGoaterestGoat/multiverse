"""A plugin's `plugin.json`: what it is, which API it needs, what it extends and asks for.

Reading a manifest imports nothing from the plugin, so `mv2 --help`, `mv2 plugins`
and the typeahead can describe plugins without running their code.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re

FILE = "plugin.json"
# The same rule as a bridge id: lowercase letters, digits and single hyphens.
NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
NAME_LENGTH = 40
# A plugin's per-agent state is state/agents/<id>/NAME/, so it may not take a
# name an agent directory already uses, or one core keeps for itself.
RESERVED_NAMES = frozenset({
    "launches", "claude-asks", "claude-receipts", "status-items", "events", "plugins", "host",
    "state", "inbox", "properties", "runtime", "session", "mv2", "multiverse", "core",
})
# Extension points of this mv2's plugin API; a later minor version adds more.
EXTENSION_POINTS = frozenset({
    "cli", "catalog", "bootstrap", "doctor",
    "dashboard.column", "dashboard.panel", "dashboard.inspector", "dashboard.theme",
    "dashboard.key", "dashboard.screen",
    # API 1.1: the harness side.
    "launch", "capabilities", "usage", "statusline", "helper",
    # API 1.2: events and long-running work in the plugin host.
    "events", "service",
})
# Event types core records; a plugin's own events are PLUGIN.*.
CORE_EVENTS = ("agent.created", "agent.launched", "agent.ready", "agent.status", "agent.exited", "agent.killed",
               "agent.reported", "agent.moved", "agent.removed", "message.saved", "message.delivered",
               "message.failed", "message.read", "universe.created", "refresh.applied")
EVENT_PATTERN = re.compile(r"(\*|[a-z][a-z0-9-]*\.(\*|[a-z][a-z0-9_-]*))")
FAMILIES = ("claude", "codex", "pi")
# Status-line items core draws itself (Claude's line and the tmux bar); a plugin's item takes another name.
CORE_ITEMS = frozenset({"agent", "harness", "status", "phase", "summary", "inbox", "context"})
VERSION = re.compile(r"\d+\.\d+\.\d+")
PERMISSIONS = {
    "agents.read": "read this universe's agents: names, state, reported work, terminal tails and usage",
    "agents.control": "spawn, resume and kill agents in this universe",
    "messages.send": "send messages to any agent in this universe, as this plugin",
    "messages.ask": "ask agents side questions",
    "messages.read": "read agents' messages",
    "universe.write": "change this universe's definition files (refresh)",
    "network.listen": "listen for network connections",
    "process.spawn": "start other programs",
    "harness.claude": "add to Claude Code launches",
    "harness.codex": "add to Codex launches",
    "harness.pi": "add to Pi launches",
}
MODEL_PERMISSION = re.compile(r"models\.[a-z][a-z0-9-]*")
DEFAULTS = ("on", "off")
KNOWN_FIELDS = {"schema", "name", "version", "description", "api", "module", "default", "extends",
                "permissions", "commands", "settings", "harnesses", "statusline", "events"}
SETTINGS_FIELDS = {"version", "schema", "defaults", "legacy"}


class ManifestError(ValueError):
    """The manifest cannot be used; the message names the file and the field."""


@dataclass(frozen=True)
class Settings:
    """Where a plugin's settings come from: their shape's version, schema, defaults and pre-plugin file."""
    version: int
    schema: str | None = None
    defaults: str | None = None
    legacy: str | None = None


@dataclass(frozen=True)
class HarnessEntry:
    """One harness extension: for Claude a plugin directory (`dir`), for Pi an extension file
    (`extension`), for Codex the fields of its contract (`fields`, checked by the Codex adapter).
    Versions are inclusive bounds on the harness executable's (major, minor, patch)."""
    family: str
    min_version: tuple | None = None
    max_version: tuple | None = None
    path: str | None = None
    needs_bridge: bool = True
    fields: dict = field(default_factory=dict)

    def fits(self, version):
        """None when this harness version can take the entry, else why not."""
        if self.min_version is None and self.max_version is None:
            return None
        if version is None:
            return "the harness version could not be read"
        shown = ".".join(map(str, version))
        if self.min_version is not None and version < self.min_version:
            return f"needs {self.family} {'.'.join(map(str, self.min_version))} or newer; this is {shown}"
        if self.max_version is not None and version > self.max_version:
            return f"needs {self.family} {'.'.join(map(str, self.max_version))} or older; this is {shown}"
        return None


@dataclass(frozen=True)
class Item:
    """A status-line item a plugin writes: its name and whether it shows by default on each line."""
    name: str
    claude: bool = True
    tmux: bool = False


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    directory: Path
    description: str = ""
    api: tuple[int, int] = (1, 0)
    module: str | None = None
    default: str = "off"
    extends: frozenset = frozenset()
    permissions: frozenset = frozenset()
    commands: dict = field(default_factory=dict)
    settings: Settings | None = None
    harnesses: dict = field(default_factory=dict)  # family -> tuple of HarnessEntry
    statusline: tuple = ()  # Item
    events: tuple = ()  # event type patterns it subscribes to: "agent.status", "message.*", "*"

    def permission_texts(self):
        """Each permission as words, for `mv2 plugins enable` and `info`."""
        return [PERMISSIONS.get(name) or (f"use {name.split('.', 1)[1]} models (this spends tokens)"
                                          if MODEL_PERMISSION.fullmatch(name) else name)
                for name in sorted(self.permissions)]


def check_name(value, what="name"):
    if not isinstance(value, str) or len(value) > NAME_LENGTH or not NAME.fullmatch(value):
        raise ValueError(f"{what} must be lowercase letters, digits and single hyphens, starting with a letter, "
                         f"at most {NAME_LENGTH} characters")
    return value


def parse_api(value):
    """`"1.0"` as (1, 0): major 1, at least minor 0."""
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+", value):
        raise ValueError('api must be "MAJOR.MINOR", such as "1.0"')
    major, minor = (int(part) for part in value.split("."))
    return major, minor


def _inside(directory, value, what):
    """A relative path inside the plugin directory."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{what} must stay inside the plugin directory")
    return value


def load(directory):
    """The manifest in `directory`, or ManifestError naming what is wrong."""
    directory = Path(directory)
    path = directory / FILE
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise ManifestError(f"{path}: {error.strerror or error}") from None
    except ValueError as error:
        raise ManifestError(f"{path}: not valid JSON: {error}") from None
    try:
        return _parse(directory, data)
    except ValueError as error:
        raise ManifestError(f"{path}: {error}") from None


def _parse(directory, data):
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object")
    if data.get("schema") != 1:
        raise ValueError("schema must be 1")
    unknown = set(data) - KNOWN_FIELDS
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    name = check_name(data.get("name"))
    if name in RESERVED_NAMES:
        raise ValueError(f"name {name!r} is reserved")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip() or len(version) > 40:
        raise ValueError("version must be a short string, such as \"1.0.0\"")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    api = parse_api(data.get("api"))
    module = data.get("module")
    if module is not None and (not isinstance(module, str)
                               or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", module)):
        raise ValueError("module must be a Python module name inside the plugin, such as \"plugin\"")
    default = data.get("default", "off")
    if default not in DEFAULTS:
        raise ValueError('default must be "on" or "off"')
    extends = data.get("extends", [])
    if not isinstance(extends, list) or not all(isinstance(item, str) for item in extends):
        raise ValueError("extends must be a list of extension point names")
    unknown_points = sorted(set(extends) - EXTENSION_POINTS)
    if unknown_points:
        raise ValueError(f"extends names extension points this mv2 does not have: {', '.join(unknown_points)}")
    permissions = data.get("permissions", [])
    if not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions):
        raise ValueError("permissions must be a list of permission names")
    unknown_permissions = sorted(item for item in permissions
                                 if item not in PERMISSIONS and not MODEL_PERMISSION.fullmatch(item))
    if unknown_permissions:
        raise ValueError(f"unknown permissions: {', '.join(unknown_permissions)}")
    commands = data.get("commands", {})
    if not isinstance(commands, dict):
        raise ValueError("commands must map each command's name to its help text")
    for command, text in commands.items():
        check_name(command, f"command {command!r}")
        if not isinstance(text, str):
            raise ValueError(f"commands.{command} must be its help text")
    if commands and "cli" not in extends:
        raise ValueError('commands needs "cli" in extends')
    settings = data.get("settings")
    if settings is not None:
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")
        unknown_settings = set(settings) - SETTINGS_FIELDS
        if unknown_settings:
            raise ValueError(f"unknown settings fields: {', '.join(sorted(unknown_settings))}")
        settings_version = settings.get("version")
        if not isinstance(settings_version, int) or isinstance(settings_version, bool) or settings_version < 1:
            raise ValueError("settings.version must be a whole number from 1")
        settings = Settings(
            settings_version,
            _inside(directory, settings["schema"], "settings.schema") if "schema" in settings else None,
            _inside(directory, settings["defaults"], "settings.defaults") if "defaults" in settings else None,
            _inside(directory, settings["legacy"], "settings.legacy") if "legacy" in settings else None)
        if settings.legacy is not None and Path(settings.legacy).name != settings.legacy:
            raise ValueError("settings.legacy must be a file name at a universe's root")
    harnesses = _harnesses(directory, data.get("harnesses", {}), permissions)
    statusline = _statusline(data.get("statusline", {}), extends)
    events = data.get("events", [])
    if not isinstance(events, list) or not all(isinstance(item, str) and EVENT_PATTERN.fullmatch(item) for item in events):
        raise ValueError('events must list event types such as "agent.status", "message.*" or "*"')
    if events and "events" not in extends:
        raise ValueError('events needs "events" in extends')
    return Manifest(name, version.strip(), directory, description.strip(), api, module, default,
                    frozenset(extends), frozenset(permissions), dict(commands), settings, harnesses, statusline,
                    tuple(events))


def _version(value, what):
    if value is None:
        return None
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError(f'{what} must be "MAJOR.MINOR.PATCH", such as "2.1.292"')
    return tuple(int(part) for part in value.split("."))


def _harnesses(directory, value, permissions):
    if not isinstance(value, dict):
        raise ValueError("harnesses must map a harness family to a list of entries")
    result = {}
    for family, entries in value.items():
        if family not in FAMILIES:
            raise ValueError(f"harnesses: unknown harness family {family!r}; use {', '.join(FAMILIES)}")
        if f"harness.{family}" not in permissions:
            raise ValueError(f'harnesses.{family} needs the permission "harness.{family}"')
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise ValueError(f"harnesses.{family} must be a list of objects")
        found = []
        for index, entry in enumerate(entries):
            where = f"harnesses.{family}[{index}]"
            bounds = (_version(entry.get("min_version"), f"{where}.min_version"),
                      _version(entry.get("max_version"), f"{where}.max_version"))
            if family == "codex":
                found.append(HarnessEntry(family, *bounds, fields={key: item for key, item in entry.items()
                                                                     if key not in ("min_version", "max_version")}))
                continue
            key = "dir" if family == "claude" else "extension"
            unknown = set(entry) - {key, "min_version", "max_version", "needs_bridge"}
            if unknown:
                raise ValueError(f"{where}: unknown fields: {', '.join(sorted(unknown))}")
            # A Claude plugin's hooks load only with the bridge's function hooks; a Pi extension loads either way.
            needs_bridge = entry.get("needs_bridge", family == "claude")
            if not isinstance(needs_bridge, bool):
                raise ValueError(f"{where}.needs_bridge must be true or false")
            found.append(HarnessEntry(family, *bounds, _inside(directory, entry.get(key), f"{where}.{key}"), needs_bridge))
        result[family] = tuple(found)
    return result


def _statusline(value, extends):
    if not isinstance(value, dict):
        raise ValueError("statusline must map item names to {\"claude\": true/false, \"tmux\": true/false}")
    if value and "statusline" not in extends:
        raise ValueError('statusline needs "statusline" in extends')
    items = []
    for name, lines in value.items():
        check_name(name, f"statusline item {name!r}")
        if name in CORE_ITEMS:
            raise ValueError(f"statusline item {name!r} is one mv2 draws itself")
        if not isinstance(lines, dict) or set(lines) - {"claude", "tmux"} or not all(
                isinstance(flag, bool) for flag in lines.values()):
            raise ValueError(f'statusline.{name} must be {{"claude": true/false, "tmux": true/false}}')
        items.append(Item(name, lines.get("claude", True), lines.get("tmux", False)))
    return tuple(items)
