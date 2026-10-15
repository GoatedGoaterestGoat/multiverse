"""What the dashboard asks of plugins, synchronous and free of Textual.

Every method may run plugin code, so the app calls them from worker threads and
never on the event loop. A plugin's failure is recorded against it by
`multiverse.plugins.loader` (the header counts it) and shows up here as a short
text, never as an exception the dashboard has to survive. Tests replace this
object with a fake.
"""
from pathlib import Path
import time
from types import SimpleNamespace

from .. import plugins, universe
from ..plugins import host, loader

def universe_name(root):
    """The universe's own name from universe.json, else its directory's (a top universe's directory is `.multiverse`)."""
    from ..store import read
    try:
        return str(read(Path(root) / "universe.json").get("name") or Path(root).name)
    except (OSError, ValueError, AttributeError):
        return Path(root).name


def plugin_of(error):
    """(plugin name, its Loaded or None) when `error` was raised by code of a loaded plugin, else None.

    A plugin's modules live in a package of their own (mv2_plugins.NAME_digest), so a traceback frame in
    one says whose code it was, whatever Textual code stands between it and the event loop."""
    traceback = error.__traceback__
    while traceback is not None:
        module = str(traceback.tb_frame.f_globals.get("__name__", ""))
        if module.startswith(loader.PACKAGE + "."):
            package = ".".join(module.split(".")[:2])
            found = loader.loaded_by_package(package)
            return (found.name if found else package.split(".")[1].rsplit("_", 1)[0]), found
        traceback = traceback.tb_next
    return None


ACTION_TIMEOUT = 30.0
FACTS_BUDGET = 4.0  # seconds for every plugin's inspector lines on one scan


class PluginService:
    def listing(self, root):
        return plugins.listing(Path(root))

    def set_enabled(self, root, name, on):
        return plugins.set_enabled(Path(root), name, on)

    def edit_settings(self, root, name, changes):
        return plugins.edit_settings(Path(root), name, changes)

    def host_top(self, root):
        """The top-level universe the plugin host runs for."""
        top = Path(root)
        while (parent := universe.parent(top)) is not None:
            top = parent
        return top

    def host_state(self, root):
        """{needed, running, stopped: {plugin: reason}, pid, workers} for this tree's host, or None when unreadable."""
        try:
            info = host.status(self.host_top(root))
        except Exception:
            return None
        return {"needed": bool(info.get("needed")), "running": bool(info.get("running")),
                "stopped": dict(info.get("stopped") or {}), "pid": info.get("pid"),
                "workers": sorted(info.get("workers") or ())}

    def start_host(self, root):
        """Start the host unless one runs; raises with the reason when it does not come up."""
        info = host.start(self.host_top(root))
        if not info.get("running"):
            raise RuntimeError(f"it did not take its lock within 10 s; see {host.host_directory(self.host_top(root)) / 'host.log'}")
        return info

    def stop_host(self, root):
        return host.stop(self.host_top(root))

    def on_here(self, root):
        return plugins.on_here(Path(root))

    def problems(self, root):
        return plugins.problems(Path(root))

    def error_count(self):
        """Plugin failures recorded in this process (cheap: no plugin code runs)."""
        return sum(len(texts) for texts in loader.errors().values())

    def registry(self, root):
        """Palette actions and screens of every plugin on anywhere in the tree under `root`.

        {"actions": {(plugin, name): (plugin, name, Loaded, function)}, "screens": {(plugin, name): (Loaded, reference)}}.
        Where a plugin is off is checked when an entry is used, not here.
        """
        actions, screens = {}, {}
        for scope in universe.tree(Path(root)):
            for loaded, name, function in plugins.dashboard_actions(scope):
                actions.setdefault((loaded.name, name), (loaded.name, name, loaded, function))
            for loaded in loader.active(scope):
                for name, reference in loaded.registrations.screens.items():
                    screens.setdefault((loaded.name, name), (loaded, reference))
        return {"actions": actions, "screens": screens}

    def facts(self, root, row, budget=FACTS_BUDGET):
        """[(plugin, text)] for one agent row: each plugin on in its universe, in name order.

        Every fact runs in a daemon thread of its own within what is left of `budget` seconds
        (loader.guarded), so a fact that never returns costs one parked thread, not one per scan,
        and never an executor thread. A fact that fails, runs out of time or is still running from
        an earlier scan shows as `unavailable`; one that returns nothing is left out.
        """
        lines, deadline = [], time.monotonic() + budget
        for loaded, fact in plugins.inspector_facts(Path(root)):
            left = max(0.05, min(loader.SYNCHRONOUS_BUDGET, deadline - time.monotonic()))
            outcome = loader.guarded(loaded, "inspector", fact, dict(row), root=root, timeout=left,
                                     key=("inspector", getattr(fact, "__qualname__", id(fact))))
            if outcome.state != "ok":
                lines.append((loaded.name, "unavailable"))
            elif outcome.value:
                lines.append((loaded.name, str(outcome.value)))
        return lines

    def run_action(self, scope, entry, row):
        """Run one plugin's palette action for `row` in `scope`; returns its status line or None.

        The action runs in a daemon thread (loader.guarded), never in an executor: one that never
        returns is left parked, cannot keep the process from exiting, and is reported as still
        running if it is run again. Raises ValueError when the plugin is off in that scope and
        RuntimeError when the action failed, ran too long or is still running.
        """
        plugin, name, loaded, function = entry
        scope = Path(scope)
        if plugin not in plugins.on_here(scope):
            raise ValueError(f"{plugin} is off in {universe_name(scope)}: turn it on from the Plugins screen (P)")
        context = SimpleNamespace(row=dict(row) if row else None, scope=scope, app=None)
        outcome = loader.guarded(loaded, f"action {name}", function, context, root=scope, timeout=ACTION_TIMEOUT)
        if outcome.state == "timeout":
            raise RuntimeError(f"{plugin}: {name} took longer than {ACTION_TIMEOUT:g} s")
        if outcome.state == "busy":
            raise RuntimeError(f"{plugin}: {name} is still running from an earlier try")
        if outcome.state != "ok":
            raise RuntimeError(f"{plugin}: {name} failed; see mv2 plugins")
        return None if outcome.value is None else str(outcome.value)

    def record(self, loaded, what, error, root):
        """Count a failure of plugin code against its plugin (the header shows the total)."""
        try:
            loader.record(loaded, what, error, Path(root))
        except Exception:
            pass

    def screen_class(self, loaded, reference):
        return loaded.resolve(reference)

    def settings_screen(self, root, name, reference):
        """The plugin's own settings screen class, imported now."""
        found = plugins.entry(Path(root), name)
        return loader.load(found).resolve(reference)
