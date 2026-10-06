"""Dashboard data/actions, separate from rendering so the UI can run headlessly."""
from pathlib import Path
from hashlib import sha256
import re

from datetime import datetime, timezone

from . import display, universe
from .display import clean
from .harnesses import load as load_harness
from .store import read
from .tools import (ask, get_usage, attach, capabilities, compaction, deliver, inspect, kill, list_agents, move, native_status, remove,
                    resume, send, spawn, stop, subverse)
from .views import normalize_widgets

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


def with_usage(agent):
    """Display fields from the agent's native usage reading."""
    reading = agent.get("usage") if isinstance(agent.get("usage"), dict) else {}
    return {**agent, "context": display.context_display(reading), "context_level": display.context_level(reading),
            "cost": display.cost_display(reading), "context_line": display.context_line(reading)}


def rate_limits(items):
    """Account-wide windows from the newest reading among live agents, unexpired: '5h 34% · 7d 12%'."""
    now = datetime.now(timezone.utc)
    best = {}
    for agent in items:
        if agent.get("live") != "alive" or not isinstance(agent.get("usage"), dict):
            continue
        for label, percent, resets, reported in display.rate_limit_windows(agent["usage"]):
            try:
                if resets and datetime.fromisoformat(resets.replace("Z", "+00:00")) <= now:
                    continue
            except ValueError:
                pass
            if label not in best or str(reported or "") >= best[label][1]:
                best[label] = (percent, str(reported or ""))
    return " · ".join(f"{label} {best[label][0]:.0f}%" for label in ("5h", "7d") if label in best)


def within(item, root):
    return Path(item["universe"]).is_relative_to(root)


def counts(items):
    return {"total": len(items), "alive": sum(a.get("live") in ('alive', 'ending') for a in items),
            "blocked": sum(a.get("phase") == "blocked" for a in items),
            "waiting": sum(a.get("status") == "waiting" for a in items),
            "unknown": sum(a.get("live") == "unknown" for a in items)}


def harnesses(root):
    """Discover this universe's spawn adapters without loading prompt bodies."""
    available = []
    for path in sorted((root / "harnesses").glob("*/harness.json")):
        try:
            name = universe.slug(path.parent.name)
            load_harness(root, name)
            available.append(name)
        except (OSError, ValueError, AttributeError):
            continue
    return sorted(available, key=lambda name: ({"codex": 0, "claude": 1}.get(name, 2), name))


class LocalBackend:
    demo = False

    def snapshot(self, root):
        meta, config = read(root / "universe.json"), view(root)
        members = list_agents.run(root, recursive=True, json=True)
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
        return spawn.run(root, name=values["name"], task=values.get("task", ""),
                         harness=values.get("harness") or "codex", model=values.get("model") or None)

    def send(self, root, reference, text, delivery='steer', queue_only=False):
        return send.run(root, agent=reference, text=text, sender={"name": "user", "agent_id": None},
                        delivery=delivery, queue_only=queue_only)

    def ask(self, root, reference, question):
        return ask.run(root, agent=reference, question=question)

    def get_usage(self, root, reference):
        return get_usage.run(root, agent=reference)

    def capabilities(self, root, reference, probe=False):
        return capabilities.run(root, agent=reference, probe=probe)

    def deliver(self, root, reference, retry=False, check=False):
        return deliver.run(root, agent=reference, retry=retry, check=check)

    def native_status(self, root, reference, operation="usage"):
        return native_status.run(root, agent=reference, operation=operation)

    def resume(self, root, reference):
        return resume.run(root, agent=reference)

    def stop(self, root, reference):
        return stop.run(root, agent=reference)

    def kill(self, root, reference):
        return kill.run(root, agent=reference)

    def inspect(self, root, reference):
        return inspect.run(root, agent=reference)

    def attach(self, root, reference):
        return attach.run(root, agent=reference)

    def move_agent(self, root, reference, destination):
        return move.run(root, target=reference, destination=str(destination))

    def move_subverse(self, path, destination):
        return move.run(universe.parent(path), target=str(path), destination=str(destination), subverse=True)

    def remove_agent(self, root, reference):
        return remove.run(root, target=reference)

    def remove_subverse(self, path):
        return remove.run(universe.parent(path), target=str(path), subverse=True)

    def compaction_overview(self, root):
        return compaction.overview(root)

    def save_compaction(self, root, value):
        return compaction.save_settings(root, value)

    def compaction_agent(self, root, reference, **preview):
        return compaction.agent_view(root, agent=reference, **preview)

    def set_compaction_scheduler(self, root, reference, scheduler):
        return compaction.set_scheduler(root, agent=reference, scheduler=scheduler)

    def subverse(self, root, values):
        result = subverse.run(root, name=values["name"], template=values.get("template") or "base",
                              workspace=values.get("workspace") or None)
        return Path(result["universe"])


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
                    ("name", "id", "task", "summary", "phase", "live", "status", "harness")).casefold():
                descendants.append({**with_usage(agent), "kind": "agent", "key": agent_key(agent),
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
