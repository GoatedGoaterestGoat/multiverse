#!/usr/bin/env python3
"""Generate src/multiverse/harnesses/claude/models.json from the model catalog built into Claude Code.

    python scripts/gen_claude_models.py                # from the claude on PATH
    python scripts/gen_claude_models.py BINARY         # from that Claude Code binary
    python scripts/gen_claude_models.py --check        # exit 1 when the table is stale for that binary

Claude Code has no command that lists its models, but its binary embeds two catalogs, as JavaScript object
literals: a hand-maintained one (every model's IDs per provider, efforts and context), and a release copy of its
downloadable model catalog, which is what its /model picker shows (default efforts it adds, the default model, and
which models it offers at all). This reads both, converts them to JSON and keeps what mv2 uses. mv2 never reads the
binary or Claude's own caches at run time: each Claude Code version check reruns this script and commits the table
with the version it came from.
"""
import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "multiverse" / "harnesses" / "claude" / "models.json"
MARKER = b"Hand-maintained baked-in model catalog"
RELEASE = b'$schema:"https://downloads.claude.ai/model-catalog/v1/schema.json"'
PROVIDERS = ("first_party", "bedrock", "vertex", "foundry", "anthropic_aws", "anthropic_google_cloud", "mantle",
             "gateway")
STRING = re.compile(r'("(?:\\.|[^"\\])*")')


def literal(data, marker=MARKER, what="model catalog"):
    """A catalog's object literal, from the `{` before its marker to the matching `}`."""
    at = data.find(marker)
    if at < 0:
        raise SystemExit(f"no {what} in this binary: Claude Code moved or renamed it")
    start = data.rindex(b"={", 0, at) + 1
    depth, quote, index = 0, None, start
    while index < len(data):
        char = data[index:index + 1]
        if quote:
            if char == b"\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in (b'"', b"'", b"`"):
            quote = char
        elif char in (b"{", b"["):
            depth += 1
        elif char in (b"}", b"]"):
            depth -= 1
            if not depth:
                return data[start:index + 1].decode()
        index += 1
    raise SystemExit(f"the {what} never ends: Claude Code changed its format")


def parse(text):
    """JSON from the minified literal: bare keys quoted, !0 and !1 as true and false; strings left alone."""
    parts = STRING.split(text)
    for index in range(0, len(parts), 2):
        part = re.sub(r"([{,])([A-Za-z_$][\w$]*):", r'\1"\2":', parts[index])
        parts[index] = part.replace("!0", "true").replace("!1", "false")
    try:
        return json.loads("".join(parts))
    except ValueError as error:
        raise SystemExit(f"the model catalog isn't plain data any more: {error}") from None


def shape(ok, what):
    """Fail loudly on a catalog shape this script doesn't know, rather than write a partial table."""
    if not ok:
        raise SystemExit(f"model catalog: {what}; update scripts/gen_claude_models.py for it")


def tokens(value, what):
    shape(value is None or isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0, what)
    return None if value is None else int(value)


def efforts(capabilities):
    """Claude Code's rule: `effort` gives low, medium and high; `xhigh_effort` adds xhigh; `max_effort` adds max."""
    if "effort" not in capabilities:
        return []
    return ["low", "medium", "high"] + ["xhigh"] * ("xhigh_effort" in capabilities) + ["max"] * ("max_effort" in capabilities)


def picker(release):
    """From the release catalog, Claude Code's (`cc`) picker: each model it offers, with its default effort and the
    providers it is offered on, and the default model."""
    shape(isinstance(release, dict) and release.get("schema_version") == 1,
          f"release catalog schema {release.get('schema_version') if isinstance(release, dict) else None!r}")
    cc = (release.get("surfaces") or {}).get("cc")
    shape(isinstance(cc, dict), "a release catalog without Claude Code's surface")
    configs, states = cc.get("model_selector_config"), cc.get("model_selector_state")
    shape(isinstance(configs, list) and len(configs) == 1 and isinstance(configs[0], dict)
          and isinstance(configs[0].get("models"), list) and configs[0]["models"], "the release catalog's models")
    shape(isinstance(states, list) and len(states) == 1 and isinstance(states[0], dict)
          and isinstance(states[0].get("model"), str), "the release catalog's default model")
    offered = {}
    for entry in configs[0]["models"]:
        shape(isinstance(entry, dict) and isinstance(entry.get("id"), str), "a release model without an id")
        runtime = entry.get("runtime", {})
        shape(isinstance(runtime, dict), f"release model {entry['id']}'s runtime")
        effort, places = runtime.get("default_effort"), entry.get("offered_on")
        shape(effort is None or isinstance(effort, str), f"release model {entry['id']}'s default effort")
        shape(isinstance(places, list) and set(places) <= set(PROVIDERS), f"release model {entry['id']}'s providers")
        offered[entry["id"]] = (effort.strip().lower() if effort else None, sorted(places))
    return offered, states[0]["model"]


def model(entry, offered, default):
    shape(isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"], "a model without an id")
    name = entry["id"]
    ids, capabilities = entry.get("provider_ids"), entry.get("capabilities", [])
    shape(isinstance(ids, dict) and all(value is None or isinstance(value, str) for value in ids.values()),
          f"{name}'s provider_ids")
    shape(set(ids) <= set(PROVIDERS), f"{name} names an unknown provider {sorted(set(ids) - set(PROVIDERS))}")
    shape(isinstance(capabilities, list) and all(isinstance(value, str) for value in capabilities),
          f"{name}'s capabilities")
    context, output = entry.get("context", {}), entry.get("max_output_tokens", {})
    shape(isinstance(context, dict) and isinstance(output, dict), f"{name}'s context or max_output_tokens")
    window = tokens(context.get("window"), f"{name}'s context window")
    largest = tokens(context.get("maximum_window"), f"{name}'s maximum window") or window
    effort = entry.get("default_effort") or offered.get(name, (None,))[0]  # the picker's, where the hand-kept one has none
    shape(effort is None or effort in efforts(capabilities), f"{name}'s default effort {effort!r}")
    return {
        "id": name,
        "name": entry.get("display_name", name),
        "family": entry.get("family"),
        "provider_ids": {provider: ids.get(provider) for provider in PROVIDERS},
        "efforts": efforts(capabilities),
        "default_effort": effort,
        # Claude Code sends images to every model; `image_limits` only raises a model's size limits above its defaults.
        "inputs": ["text", "image"],
        "context_window": window,
        # The window with the "[1m]" suffix, only where Claude Code takes the suffix for this model.
        "long_context_window": largest if context.get("supports_1m_suffix") else None,
        "max_output_tokens": tokens(output.get("upper") or output.get("default"), f"{name}'s max output"),
        "default": name == default,
        # The providers whose picker lists it; on others it is hidden (older or gated), though --model may take it.
        "picker": offered.get(name, (None, []))[1],
    }


def table(catalog, version, release):
    shape(isinstance(catalog, dict) and catalog.get("schema_version") == 1,
          f"schema {catalog.get('schema_version') if isinstance(catalog, dict) else None!r}")
    shape(isinstance(catalog.get("models"), list) and catalog["models"], "no models list")
    shape(all(isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"] for entry in catalog["models"]),
          "a model without an id")
    # The release catalog names a model by its first-party ID, which is often dated ("claude-haiku-4-5-20251001").
    names = {}
    for entry in catalog["models"]:
        for name in (entry["id"], (entry.get("provider_ids") or {}).get("first_party")):
            if isinstance(name, str):
                names.setdefault(name, entry["id"])
    shown, default = picker(release)
    unknown = sorted(set(shown) - set(names)) + ([] if default in names else [default])
    shape(not unknown, f"the release catalog names models the hand-maintained one lacks: {unknown}")
    offered = {names[name]: facts for name, facts in shown.items()}
    models = [model(entry, offered, names[default]) for entry in catalog["models"]]
    known = {item["id"] for item in models}
    aliases = {}
    for name, alias in (catalog.get("aliases") or {}).items():
        shape(isinstance(alias, dict) and isinstance(alias.get("per_provider", {}), dict), f"alias {name}")
        targets = {"default": alias.get("default"), "per_provider": dict(alias.get("per_provider") or {})}
        shape(targets["default"] in known and set(targets["per_provider"]) <= set(PROVIDERS)
              and set(targets["per_provider"].values()) <= known, f"alias {name} names an unknown model or provider")
        aliases[name] = targets
    return {"schema": 1, "claude_code": version, "models": models, "aliases": aliases}


def binary_version(binary):
    found = re.fullmatch(r"\d+\.\d+\.\d+", Path(binary).name)
    if found:  # Claude's installer names each version's binary after it
        return found.group(0)
    output = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    found = re.search(r"\d+\.\d+\.\d+", output.stdout)
    if not found:
        raise SystemExit(f"{binary} --version printed no version")
    return found.group(0)


def render(binary):
    data = Path(binary).read_bytes()
    release = parse(literal(data, RELEASE, "release model catalog"))
    return json.dumps(table(parse(literal(data)), binary_version(binary), release), indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("binary", nargs="?", help="a Claude Code binary (default: the claude on PATH)")
    parser.add_argument("--check", action="store_true", help="exit 1 when models.json differs")
    args = parser.parse_args()
    binary = args.binary or shutil.which("claude")
    if not binary:
        raise SystemExit("no claude on PATH: name a Claude Code binary")
    binary = os.path.realpath(binary)
    text = render(binary)
    old = TARGET.read_text() if TARGET.exists() else ""
    if args.check:
        if text != old:
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), text.splitlines(True),
                                                       "models.json", "regenerated"))
            return 1
        print("models.json matches", binary)
        return 0
    TARGET.write_text(text)
    print(f"wrote {TARGET.relative_to(ROOT)} from Claude Code {json.loads(text)['claude_code']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
