"""Dashboard data/actions, separate from rendering so the UI can run headlessly."""
from pathlib import Path

from .. import universe
from ..harnesses import load as load_harness
from ..store import read
from ..tools import (ask, get_usage, attach, capabilities, compaction, deliver, inspect, kill, list_agents, move,
                     native_status, remove, resume, send, spawn, stop, subverse)
from .. import plugins
from . import columns, theme
from .columns import COLUMNS
from .tree import counts, within
from .views import view


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
        plugin_columns = any(key not in COLUMNS for key in config["columns"])
        if plugin_columns:
            # Plugin columns: resolved and computed here, off the event loop. Each agent row says which
            # plugins its universe has on and carries its plugin cells; rendering only reads them.
            scopes = list(universe.tree(root))
            columns.PLUGIN_COLUMNS = columns.resolve_plugin_columns(scopes, config["columns"])
            on = {str(scope): sorted(plugins.on_here(scope)) for scope in scopes}
            for member in members:
                member["plugins"] = on.get(str(member.get("universe")), [])
                member["plugin_cells"] = columns.cells(member)
        else:
            columns.PLUGIN_COLUMNS = {}
        groups = []
        for scope in universe.tree(root):
            group = {"path": str(scope), "parent": str(scope.parent.parent) if scope != root else None,
                     "harnesses": harnesses(scope),
                     **counts([a for a in members if within(a, scope)])}
            try:
                data, style = read(scope / "universe.json"), view(scope)
                groups.append({**group, "name": style["title"] or data["name"],
                               "description": data.get("description", ""), "accent": style["accent"]})
                if plugin_columns:
                    groups[-1]["plugin_cells"] = columns.cells(groups[-1], group=True)
            except (OSError, ValueError, KeyError) as error:
                groups.append({**group, "name": scope.name, "description": str(error),
                               "accent": theme.RED, "error": True})
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
