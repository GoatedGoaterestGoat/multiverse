"""Each plugin's settings for a universe: `plugin-settings/NAME.json`.

    {"schema": 2, "default": {"scheduler": "last_k_raw"}}

`schema` is the plugin's own settings version, from its manifest. The file is
checked against the JSON Schema the plugin ships (the subset below), then by the
plugin's own check. A missing file means the plugin's defaults. An unusable file
never stops a launch: the plugin acts as if off in that universe, with the
reason. An edit writes the file as it was with only the changed keys, after the
whole result is accepted.

Schema keywords supported: type (one or a list), enum, const, properties,
required, additionalProperties (true, false or a schema), items, minimum,
maximum, exclusiveMinimum, exclusiveMaximum, minLength, maxLength, pattern,
minItems, maxItems, title, description, default. Anything else is refused when
the schema is read, so a plugin never relies on a check that does not run.
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re

from ..store import lock, write

DIRECTORY = "plugin-settings"
DELETE = object()  # an edit value that removes the key
KEYWORDS = {"type", "enum", "const", "properties", "required", "additionalProperties", "items", "minimum",
            "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern", "minItems",
            "maxItems", "title", "description", "default", "$schema", "$comment"}
TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


@dataclass(frozen=True)
class Settings:
    """`values`: the settings in effect (the file, else the defaults). `problem`: why they cannot be used. `notes`:
    what the plugin's own check said about usable settings (1.15), each one line."""
    values: dict
    path: Path
    source: str  # "file", "defaults" or "none"
    problem: str | None = None
    notes: tuple = ()


def path(root, name):
    return Path(root) / DIRECTORY / f"{name}.json"


def _json(file):
    try:
        data = json.loads(Path(file).read_text())
    except OSError as error:
        raise ValueError(f"{file}: {error.strerror or error}") from None
    except ValueError as error:
        raise ValueError(f"{file}: not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{file}: must be a JSON object")
    return data


def check_schema(schema, where="schema"):
    """Refuse keywords this validator does not run."""
    if not isinstance(schema, dict):
        raise ValueError(f"{where} must be an object")
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise ValueError(f"{where} uses unsupported keywords: {', '.join(sorted(unknown))}")
    kinds = schema.get("type")
    for kind in (kinds if isinstance(kinds, list) else [kinds] if kinds is not None else []):
        if kind not in TYPES:
            raise ValueError(f"{where}.type: unknown type {kind!r}")
    # The schema's own shape, so validating settings against it can only ever say what is wrong with them.
    if not isinstance(schema.get("properties", {}), dict):
        raise ValueError(f"{where}.properties must be an object")
    if "required" in schema and (not isinstance(schema["required"], list)
                                 or not all(isinstance(item, str) for item in schema["required"])):
        raise ValueError(f"{where}.required must list property names")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ValueError(f"{where}.enum must be a list")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], (int, float))):
            raise ValueError(f"{where}.{key} must be a number")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], int)):
            raise ValueError(f"{where}.{key} must be a whole number")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError) as error:
            raise ValueError(f"{where}.pattern is not a regular expression: {error}") from None
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], (bool, dict)):
        raise ValueError(f"{where}.additionalProperties must be true, false or a schema")
    for key, child in schema.get("properties", {}).items():
        check_schema(child, f"{where}.properties.{key}")
    for key in ("items",):
        if key in schema:
            check_schema(schema[key], f"{where}.{key}")
    if isinstance(schema.get("additionalProperties"), dict):
        check_schema(schema["additionalProperties"], f"{where}.additionalProperties")
    return schema


def _is(kind, value):
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return kind == "number" or isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def _child(where, key):
    return f"{where}.{key}" if where else str(key)


def _same(value, other):
    """JSON equality: unlike Python's, true is not 1 and false is not 0, at any depth."""
    if isinstance(value, bool) or isinstance(other, bool):
        return isinstance(value, bool) and isinstance(other, bool) and value == other
    if isinstance(value, dict) and isinstance(other, dict):
        return value.keys() == other.keys() and all(_same(value[key], other[key]) for key in value)
    if isinstance(value, list) and isinstance(other, list):
        return len(value) == len(other) and all(_same(a, b) for a, b in zip(value, other))
    return value == other


def validate(value, schema, where=""):
    """Raise ValueError naming the first place `value` breaks `schema` (by its key path)."""
    name = where or "settings"
    kinds = schema.get("type")
    if kinds is not None:
        allowed = kinds if isinstance(kinds, list) else [kinds]
        if not any(_is(kind, value) for kind in allowed):
            raise ValueError(f"{name} must be {' or '.join(allowed)}")
    if "const" in schema and not _same(value, schema["const"]):
        raise ValueError(f"{name} must be {json.dumps(schema['const'])}")
    if "enum" in schema and not any(_same(value, item) for item in schema["enum"]):
        raise ValueError(f"{name} must be one of {', '.join(json.dumps(item) for item in schema['enum'])}")
    if _is("number", value):
        for key, test, words in (("minimum", lambda limit: value >= limit, "at least"),
                                 ("maximum", lambda limit: value <= limit, "at most"),
                                 ("exclusiveMinimum", lambda limit: value > limit, "more than"),
                                 ("exclusiveMaximum", lambda limit: value < limit, "less than")):
            if key in schema and not test(schema[key]):
                raise ValueError(f"{name} must be {words} {schema[key]}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{name} must have at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{name} must have at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError(f"{name} must match {schema['pattern']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{name} must have at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{name} must have at most {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate(item, schema["items"], f"{name}[{index}]")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{_child(where, key)} is required")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], _child(where, key))
            elif extra is False:
                raise ValueError(f"{name}: unknown setting {key!r}")
            elif isinstance(extra, dict):
                validate(item, extra, _child(where, key))


def schema_of(manifest):
    """The plugin's settings schema, checked, or None when it ships none."""
    if manifest.settings is None or manifest.settings.schema is None:
        return None
    return check_schema(_json(manifest.directory / manifest.settings.schema), f"{manifest.name} settings schema")


def defaults_of(manifest):
    """The plugin's default settings, as its file would hold them."""
    if manifest.settings is None:
        return {}
    if manifest.settings.defaults is None:
        return {"schema": manifest.settings.version}
    return _json(manifest.directory / manifest.settings.defaults)


def run_check(check, data, root):
    """A plugin's settings check: check(settings), or check(settings, universe directory) when it takes two. Returns
    what it returned: notes, or anything else (None) for none."""
    import inspect
    try:
        parameters = [parameter for parameter in inspect.signature(check).parameters.values()
                      if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        parameters = []
    return check(data, Path(root) if root is not None else None) if len(parameters) >= 2 else check(data)


NOTES, NOTE_CHARACTERS = 20, 200


def _notes(value):
    """A check's notes: the strings of a list or tuple it returned, each its first line, bounded; else none."""
    if not isinstance(value, (list, tuple)):
        return ()
    lines = (note.strip().splitlines()[0][:NOTE_CHARACTERS] for note in value if isinstance(note, str) and note.strip())
    return tuple(lines)[:NOTES]


def _accept(manifest, data, check, where, root=None):
    """Raise ValueError when `data` cannot be this plugin's settings; else the notes its check gave."""
    expected = manifest.settings.version
    found = data.get("schema")
    if found != expected:
        if isinstance(found, int) and not isinstance(found, bool) and found < expected:
            raise ValueError(f"{where}: written for settings version {found}; run mv2 plugins migrate")
        raise ValueError(f"{where}: schema must be {expected}")
    schema = schema_of(manifest)
    if schema is not None:
        try:
            validate({key: value for key, value in data.items() if key != "schema"}, schema)
        except ValueError as error:
            raise ValueError(f"{where}: {error}") from None
    return _notes(run_check(check, data, root)) if check is not None else ()


def read(root, manifest, check=None):
    """The plugin's settings in this universe. Never raises; `problem` says why they cannot be used.

    Order: plugin-settings/NAME.json, else the defaults. A pre-plugin file the
    manifest names (`legacy`) is never read here: it is converted on first use
    (plugins.migrate_on_first_use), and the plugin may refuse to run while it
    waits.
    """
    file = path(root, manifest.name)
    if manifest.settings is None:
        return Settings({}, file, "none")
    try:
        if file.is_file():
            data, source, where = _json(file), "file", str(file)
        else:
            data, source, where = defaults_of(manifest), "defaults", f"{manifest.name} default settings"
        notes = _accept(manifest, data, check, where, root)
        return Settings(data, file, source, notes=notes)
    except ValueError as error:
        return Settings({}, file, "file" if file.is_file() else "defaults", str(error))
    except Exception as error:  # a plugin's own check must not break its caller
        return Settings({}, file, "file" if file.is_file() else "defaults",
                        f"{manifest.name}: its settings check failed: {type(error).__name__}: {error}")


def install_defaults(root, manifest):
    """Write the default settings when this universe has none; True when written."""
    if manifest.settings is None:
        return False
    file = path(root, manifest.name)
    legacy = Path(root) / manifest.settings.legacy if manifest.settings.legacy else None
    if file.exists() or (legacy is not None and legacy.exists()):
        return False  # defaults here would stand in front of a pre-plugin file waiting to be converted
    write(file, defaults_of(manifest))
    return True


def edit(root, manifest, changes, check=None):
    """Change only the named settings and write the file; returns the new Settings.

    `changes` maps a key path (a tuple, or a dotted string) to its new value,
    or to DELETE to remove it and any object that leaves empty. The whole
    result must pass the schema and the plugin's check, or nothing is written.
    """
    if manifest.settings is None:
        raise ValueError(f"{manifest.name} has no settings")
    file = path(root, manifest.name)
    legacy = manifest.settings.legacy
    if legacy and not file.exists() and (Path(root) / legacy).is_file():
        # Built from defaults, the new file would stand in front of the real settings for good.
        raise ValueError(f"{legacy} is still here and not converted: run mv2 plugins migrate --apply first, "
                         f"so its settings are kept")
    with lock(Path(root) / "state" / ".plugins.lock"):
        data = _json(file) if file.is_file() else defaults_of(manifest)
        for key, value in changes.items():
            keys = tuple(key.split(".")) if isinstance(key, str) else tuple(key)
            if not keys or keys[0] == "schema":
                raise ValueError("The settings version is not edited directly; use mv2 plugins migrate")
            if value is DELETE:
                _delete(data, keys)
            else:
                target = data
                for part in keys[:-1]:
                    child = target.get(part)
                    if not isinstance(child, dict):
                        child = target[part] = {}
                    target = child
                target[keys[-1]] = value
        _accept(manifest, data, check, str(file), root)
        write(file, data)
    return read(root, manifest, check)


def _delete(data, keys):
    trail = [data]
    for part in keys[:-1]:
        child = trail[-1].get(part)
        if not isinstance(child, dict):
            return
        trail.append(child)
    trail[-1].pop(keys[-1], None)
    for parent, part in zip(reversed(trail[:-1]), reversed(keys[:-1])):
        if parent.get(part) == {}:
            del parent[part]
