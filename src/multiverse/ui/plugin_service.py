"""What the dashboard asks of plugins, synchronous and free of Textual.

Every method may run plugin code, so the app calls them from worker threads and
never on the event loop. A plugin's failure is recorded against it by
`multiverse.plugins.loader` (the header counts it) and shows up here as a short
text, never as an exception the dashboard has to survive. Tests replace this
object with a fake.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as Timeout
from pathlib import Path
from types import SimpleNamespace

from .. import plugins, universe
from ..plugins import host, loader

ACTION_TIMEOUT = 30.0
FAILED = object()


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
            raise RuntimeError("it did not take its lock within 10 s; see state/plugins/host.log")
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

    def facts(self, root, row):
        """[(plugin, text)] for one agent row: each plugin on in its universe, in name order.

        A fact that raises is recorded and shown as `unavailable`; one that
        returns nothing is left out.
        """
        lines = []
        for loaded, fact in plugins.inspector_facts(Path(root)):
            text = loader.guard(loaded, "inspector", fact, dict(row), root=root, default=FAILED)
            if text is FAILED:
                lines.append((loaded.name, "unavailable"))
            elif text:
                lines.append((loaded.name, str(text)))
        return lines

    def run_action(self, scope, entry, row):
        """Run one plugin's palette action for `row` in `scope`; returns its status line or None.

        Raises ValueError when the plugin is off in that scope, RuntimeError
        when the action failed (the loader has the details) or ran too long.
        """
        plugin, name, loaded, function = entry
        scope = Path(scope)
        if plugin not in plugins.on_here(scope):
            raise ValueError(f"{plugin} is off in {scope.name}: turn it on from the Plugins screen (P)")
        context = SimpleNamespace(row=dict(row) if row else None, scope=scope, app=None)
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(loader.guard, loaded, f"action {name}", function, context, root=scope, default=FAILED)
            try:
                result = future.result(timeout=ACTION_TIMEOUT)
            except Timeout:
                loader.record(loaded, f"action {name}", TimeoutError(f"took longer than {ACTION_TIMEOUT:g} s"), scope)
                raise RuntimeError(f"{plugin}: {name} took longer than {ACTION_TIMEOUT:g} s") from None
        finally:
            pool.shutdown(wait=False)
        if result is FAILED:
            raise RuntimeError(f"{plugin}: {name} failed; see mv2 plugins")
        return None if result is None else str(result)

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
