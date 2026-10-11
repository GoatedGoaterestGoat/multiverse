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
from ..plugins import loader

ACTION_TIMEOUT = 30.0
FAILED = object()


class PluginService:
    def listing(self, root):
        return plugins.listing(Path(root))

    def set_enabled(self, root, name, on):
        return plugins.set_enabled(Path(root), name, on)

    def edit_settings(self, root, name, changes):
        return plugins.edit_settings(Path(root), name, changes)

    def on_here(self, root):
        return plugins.on_here(Path(root))

    def problems(self, root):
        return plugins.problems(Path(root))

    def error_count(self):
        """Plugin failures recorded in this process (cheap: no plugin code runs)."""
        return sum(len(texts) for texts in loader.errors().values())

    def registry(self, root, core_keys):
        """Keys and screens of every plugin on anywhere in the tree under `root`.

        {"keys": {key: (plugin, label)}, "screens": {(plugin, name): reference},
         "clashes": [sentence]}. The top scope's plugins.json settles clashes first.
        """
        keys, screens, clashes = {}, {}, []
        for scope in universe.tree(Path(root)):
            found, problems = plugins.dashboard_keys(scope, core_keys)
            for key, (loaded, item) in found.items():
                keys.setdefault(key, (loaded.name, item.label, loaded, item))
            clashes += [text for text in problems if text not in clashes]
            for loaded in loader.active(scope):
                for name, reference in loaded.registrations.screens.items():
                    screens.setdefault((loaded.name, name), (loaded, reference))
        return {"keys": keys, "screens": screens, "clashes": clashes}

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

    def run_key(self, scope, entry, row):
        """Run one plugin key's action for `row` in `scope`; returns its status line or None.

        Raises ValueError when the plugin is off in that scope, RuntimeError
        when the action failed (the loader has the details) or ran too long.
        """
        name, label, loaded, item = entry
        scope = Path(scope)
        if name not in plugins.on_here(scope):
            raise ValueError(f"{name} is off in {scope.name}: turn it on from the Plugins screen")
        context = SimpleNamespace(row=dict(row) if row else None, scope=scope, app=None)
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(loader.guard, loaded, f"key {item.key}", item.action, context, root=scope, default=FAILED)
            try:
                result = future.result(timeout=ACTION_TIMEOUT)
            except Timeout:
                loader.record(loaded, f"key {item.key}", TimeoutError(f"took longer than {ACTION_TIMEOUT:g} s"), scope)
                raise RuntimeError(f"{name}: {label} took longer than {ACTION_TIMEOUT:g} s") from None
        finally:
            pool.shutdown(wait=False)
        if result is FAILED:
            raise RuntimeError(f"{name}: {label} failed; see mv2 plugins")
        return None if result is None else str(result)

    def screen_class(self, loaded, reference):
        return loaded.resolve(reference)

    def settings_screen(self, root, name, reference):
        """The plugin's own settings screen class, imported now."""
        found = plugins.entry(Path(root), name)
        return loader.load(found).resolve(reference)
