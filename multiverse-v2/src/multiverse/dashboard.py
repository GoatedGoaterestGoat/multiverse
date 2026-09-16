"""Dashboard data/actions, separate from rendering so the UI can run headlessly."""
from pathlib import Path
from hashlib import sha256
import re

from . import agents, universe
from .store import append_message, read
from .views import normalize_widgets

COLORS = ("#94e2d5", "#cba6f7", "#89b4fa", "#f9e2af", "#f5c2e7")
COLUMNS = {"name": ("Agent", 20), "harness": ("Harness", 9), "live": ("Live", 9),
           "activity": ("Activity", 10), "phase": ("Phase", 10), "summary": ("Working on", 48),
           "id": ("ID", 12), "universe": ("Universe", 24), "task": ("Task", 48),
           "updated_at": ("Updated", 26)}
DEFAULT_COLUMNS = ["name", "harness", "live", "activity", "phase", "summary"]


def clean(value):
    return "".join(c if c.isprintable() else " " for c in str(value))


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
    selected = list(dict.fromkeys(key for key in requested if isinstance(key, str) and key in COLUMNS))
    return {"columns": selected or DEFAULT_COLUMNS, "accent": accent,
            "title": str(config.get("title", "")), "widgets": normalize_widgets(config.get("widgets", []))}


def columns(root):
    return view(root)["columns"]


def within(item, root):
    return Path(item["universe"]).is_relative_to(root)


def counts(items):
    return {"total": len(items), "alive": sum(a.get("live") == "alive" for a in items),
            "blocked": sum(a.get("phase") == "blocked" for a in items),
            "unknown": sum(a.get("live") == "unknown" for a in items)}


def harnesses(root):
    """Discover this universe's spawn adapters without loading prompt bodies."""
    available = []
    for path in sorted((root / "harnesses").glob("*/harness.json")):
        try:
            name = universe.slug(path.parent.name)
            spec = read(path)
            argv = spec.get("spawn")
            if spec.get("schema") == 1 and isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv):
                available.append(name)
        except (OSError, ValueError, AttributeError):
            continue
    return sorted(available, key=lambda name: ({"codex": 0, "claude": 1}.get(name, 2), name))


class LocalBackend:
    demo = False

    def snapshot(self, root):
        meta, config = read(root / "universe.json"), view(root)
        members = agents.listing(root, recursive=True)
        groups = []
        for scope in universe.tree(root):
            group = {"path": str(scope), "parent": str(scope.parent.parent) if scope != root else None,
                     "harnesses": harnesses(scope),
                     **counts([a for a in members if within(a, scope)])}
            try:
                data, style = read(scope / "universe.json"), view(scope)
                groups.append({**group, "name": style["title"] or data["name"],
                               "description": data.get("description", ""), "accent": style["accent"]})
            except (OSError, ValueError, KeyError) as error:
                groups.append({**group, "name": scope.name, "description": str(error),
                               "accent": "#f38ba8", "error": True})
        return {"root": root, "name": config["title"] or meta["name"],
                "description": meta.get("description", ""), "accent": config["accent"],
                "groups": groups, "counts": counts(members),
                "columns": config["columns"], "widgets": config["widgets"], "agents": members}

    def spawn(self, root, values):
        return agents.start(root, name=values["name"], task=values["task"],
                            harness=values.get("harness") or "codex", model=values.get("model") or None)

    def send(self, root, reference, text):
        return append_message(agents.directory(root, reference), text, {"name": "user", "agent_id": None})

    def resume(self, root, reference):
        return agents.start(root, resume=reference)

    def stop(self, root, reference):
        return agents.terminal_action(root, reference, "stop")

    def inspect(self, root, reference):
        return agents.terminal_action(root, reference, "inspect")

    def attach(self, root, reference):
        from .cli import attach
        return attach(root, reference)

    def subverse(self, root, values):
        name = universe.slug(values["name"])
        return universe.create(root / "subverses" / name, name, values.get("template") or "base",
                               values.get("workspace") or universe.workspace(root))


def group_key(path):
    return "group:" + str(path)


def agent_key(agent):
    return str(Path(agent["universe"])) + ":" + agent["id"]


def hierarchy(data, collapsed=(), search=""):
    """Project one snapshot to folder rows; search reveals matches and ancestors.

    Folding is presentation only: the snapshot always contains the whole tree.
    A group-name match includes its descendants. Searching never changes folds.
    """
    groups = {g["path"]: g for g in data["groups"]}
    children = {path: [] for path in groups}
    members = {path: [] for path in groups}
    for group in groups.values():
        if group["parent"] in children:
            children[group["parent"]].append(group["path"])
    for agent in data["agents"]:
        members[agent["universe"]].append(agent)
    query = search.strip().casefold()

    def visit(path, depth, ancestor_match=False):
        group = groups[path]
        matched = ancestor_match or query in clean(group["name"]).casefold()
        descendants = []
        for agent in members[path]:
            if matched or query in " ".join(clean(agent.get(k, "")) for k in
                    ("name", "id", "task", "summary", "phase", "live", "activity", "harness")).casefold():
                descendants.append({**agent, "kind": "agent", "key": agent_key(agent),
                                    "parent_key": group_key(path), "depth": depth + 1, "accent": group["accent"]})
        for child in children[path]:
            descendants.extend(visit(child, depth + 1, matched))
        if query and not matched and not descendants:
            return []
        expanded = bool(query) or path not in collapsed
        row = {**group, "kind": "group", "key": group_key(path), "depth": depth, "expanded": expanded,
               "parent_key": group_key(group["parent"]) if group["parent"] else None}
        return [row, *descendants] if expanded else [row]

    projected = visit(str(data["root"]), 0)
    by_key = {row["key"]: row for row in projected}
    last_child = {row["parent_key"]: row["key"] for row in projected if row["parent_key"]}
    # Build guides from visible siblings so folding and search never leave a
    # continuation line pointing to a hidden branch.
    for row in projected:
        ancestry, node = [], row
        while node["parent_key"]:
            parent = by_key[node["parent_key"]]
            ancestry.append((last_child[parent["key"]] == node["key"], parent["accent"]))
            node = parent
        ancestry.reverse()
        row["guides"] = [("  " if last else "│ ", color) for last, color in ancestry[:-1]]
        if ancestry:
            last, color = ancestry[-1]
            row["guides"].append(("╰─" if last else "├─", color))
    return projected
