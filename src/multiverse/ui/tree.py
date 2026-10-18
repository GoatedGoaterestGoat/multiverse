"""The dashboard's rows: a snapshot projected to folder rows, their keys and counts."""
from datetime import datetime, timezone
from pathlib import Path

from .. import display
from ..display import clean
from ..universe import focus


def with_usage(agent):
    """Display fields from the agent's native usage reading."""
    reading = agent.get("usage") if isinstance(agent.get("usage"), dict) else {}
    held = agent.get("focus")
    return {**agent, "context": display.context_display(reading), "context_level": display.context_level(reading),
            "cost": display.cost_display(reading), "context_line": display.context_line(reading),
            "focus_line": ("Focused " + focus.ends(held) + ": only inbox-only messages reach it") if held else "Not focused"}


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
            "focused": sum(bool(a.get("focus")) for a in items),
            "unknown": sum(a.get("live") == "unknown" for a in items)}


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
