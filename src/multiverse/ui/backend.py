"""Dashboard data/actions, separate from rendering so the UI can run headlessly."""
from pathlib import Path
import threading

from .. import universe
from ..harnesses import load as load_harness
from ..store import read
from ..tools import (ask, get_usage, attach, capabilities, deliver, focus, inspect, kill, list_agents, move,
                     native_status, remove, resume, send, spawn, stop, subverse, unfocus)
from .. import display, plugins
from ..runtime import lifecycle
from . import columns, plugin_panels, theme
from .columns import COLUMNS
from .panels import error_widget
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


USAGE_FIELDS = ("usage", "tokens", "usage_detail")


def usage_key(row):
    """Which agent a usage reading belongs to."""
    return str(row.get("universe")), row.get("id")


def usage_identity(row):
    """What a usage reading was read for: the agent, its launch and its conversation. A reading applies only to a row
    that still has all of them, so a stale read never shows the next conversation's usage."""
    return (str(row.get("universe")), row.get("id"), (row.get("runtime") or {}).get("launch_id"),
            row.get("native_session_id"), row.get("native_session_home"))


class LocalBackend:
    demo = False

    def __init__(self):
        # The last usage read per agent, kept with what it was read for: a refresh shows it until the next read lands.
        self._usage, self._usage_lock = {}, threading.Lock()

    def snapshot(self, root):
        meta, config = read(root / "universe.json"), view(root)
        # Without usage: reading it parses every Claude transcript (seconds in a new process), so it comes after the
        # agents are shown, from read_usage, in a thread quitting leaves behind. The listing itself, with the records it
        # reconciles, stays here, where quitting waits for it.
        members = lifecycle.listing(root, True, usage=False)
        with self._usage_lock:
            for member in members:
                if isinstance(member.get("damaged"), dict):
                    continue  # nothing to read
                if "identity_error" in member:  # its binding failed: that error is its usage, never an older reading
                    self._usage.pop(usage_key(member), None)
                    member.update(lifecycle.row_usage(member))  # reads nothing for such a row
                    continue
                last = self._usage.get(usage_key(member))
                if last and last[0] == usage_identity(member):
                    member.update(last[1])
                else:
                    member["usage_pending"] = True  # shown as … until read_usage lands
        for member in members:  # a leftover folder is not a damaged agent: say what to do with it, not what failed to read
            if isinstance(member.get("damaged"), dict) and member["damaged"].get("recover") == "remove":
                member["summary"] = "damaged: a leftover folder with no agent records · d moves it to the trash"
        replace = config.get("replace", True)
        plugin_columns = (any(key not in COLUMNS and not columns.core_key(key) for key in config["columns"])
                          or (replace and columns.replaceable(config["columns"])))
        trouble, on = [], {}
        if plugin_columns:
            # Plugin columns, and those standing in for a core one: resolved and computed here, off the event loop.
            # Each agent row says which plugins its universe has on and carries its plugin cells; rendering only
            # reads them.
            try:
                scopes = list(universe.tree(root))
                columns.PLUGIN_COLUMNS = columns.resolve_plugin_columns(scopes, config["columns"], replace=replace)
                on = {str(scope): sorted(plugins.on_here(scope)) for scope in scopes} if columns.PLUGIN_COLUMNS else {}
                for member in members:
                    member["plugins"] = on.get(str(member.get("universe")), [])
                columns.fill_cells(members)
            except Exception as error:  # reading plugins failed: their columns show dashes, the agents still list
                columns.PLUGIN_COLUMNS = {}
                columns.forget_cells()
                trouble.append(f"plugin columns: {type(error).__name__}: {error}")
        else:
            columns.PLUGIN_COLUMNS = {}
            columns.forget_cells()
        # Plugin panels render here, off the event loop, from this scope's agents; the dashboard only draws the result.
        try:
            widgets = plugin_panels.expand(root, config["widgets"], members, config.get("plugin_panels", "auto"))
        except Exception as error:  # the view's own widgets stay; the plugins' say they could not be read
            trouble.append(f"plugin panels: {type(error).__name__}: {error}")
            widgets = [error_widget(widget["title"], "plugin widgets could not be read; see the header")
                       if widget.get("type") == "plugin" else widget for widget in config["widgets"]]
        groups = []
        for scope in universe.tree(root):
            group = {"path": str(scope), "parent": str(scope.parent.parent) if scope != root else None,
                     "harnesses": harnesses(scope),
                     **counts([a for a in members if within(a, scope)])}
            try:
                data, style = read(scope / "universe.json"), view(scope)
                groups.append({**group, "name": style["title"] or data["name"],
                               "description": data.get("description", ""), "accent": style["accent"]})
                if on:
                    groups[-1]["plugins"] = on.get(str(scope), [])
            except (OSError, ValueError, KeyError) as error:
                groups.append({**group, "name": scope.name, "description": str(error),
                               "accent": theme.RED, "error": True})
        if on:
            try:
                columns.fill_cells([g for g in groups if "plugins" in g], group=True)
            except Exception as error:
                trouble.append(f"plugin columns: {type(error).__name__}: {error}")
        return {"root": root, "name": config["title"] or meta["name"],
                "description": meta.get("description", ""), "accent": config["accent"],
                "groups": groups, "counts": counts(members),
                "columns": config["columns"], "widgets": widgets, "agents": members,
                "plugin_trouble": trouble[0] if trouble else None}

    def spawn(self, root, values):
        return spawn.run(root, name=values["name"], task=values.get("task", ""),
                         harness=values.get("harness") or "codex", model=values.get("model") or None)

    def send(self, root, reference, text, delivery='steer', queue_only=False):
        return send.run(root, agent=reference, text=text, sender={"name": "user", "agent_id": None},
                        delivery=delivery, queue_only=queue_only)

    def focus(self, root, reference, duration=None, turn=False):
        return focus.run(root, agent=reference, duration=duration, turn=turn, as_user=True)

    def unfocus(self, root, reference):
        return unfocus.run(root, agent=reference, as_user=True)

    def ask(self, root, reference, question):
        return ask.run(root, agent=reference, question=question)

    def get_usage(self, root, reference):
        return get_usage.run(root, agent=reference)

    def read_usage(self, rows):
        """Each row's usage read now, as {usage_key: (usage_identity, fields)}, and kept for the next snapshots. Never
        raises: a row whose read fails says so in its usage. Run it where quitting needn't wait for it."""
        found = {}
        for row in rows:
            if isinstance(row.get("damaged"), dict) or "identity_error" in row:
                continue  # nothing to read; the snapshot already shows an identity error
            try:
                fields = {key: row_value for key, row_value in lifecycle.row_usage(row).items() if key in USAGE_FIELDS}
            except Exception as error:
                failed = {"state": "error", "reason": f"{type(error).__name__}: {error}"}
                fields = {"usage": failed, "tokens": display.usage_total(failed), "usage_detail": display.usage_details(failed)}
            found[usage_key(row)] = (usage_identity(row), fields)
        with self._usage_lock:
            self._usage.update(found)
        return found

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

    def live_pane(self, root, reference, harness):
        """The terminal as the inspector shows it: without the harness's own chrome, where it can say what that is."""
        return inspect.display(root, harness, inspect.run(root, agent=reference))

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

    def subverse(self, root, values):
        result = subverse.run(root, name=values["name"], template=values.get("template") or "base",
                              workspace=values.get("workspace") or None)
        return Path(result["universe"])
