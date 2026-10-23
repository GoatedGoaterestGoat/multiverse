"""What the dashboard asks of plugins, synchronous and free of Textual.

Every method may run plugin code, so the app calls them from worker threads and
never on the event loop. A plugin's failure is recorded against it by
`multiverse.plugins.loader` (the header counts it) and shows up here as a short
text, never as an exception the dashboard has to survive. Tests replace this
object with a fake.
"""
import asyncio
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from textual.worker import WorkerFailed

from .. import plugins, universe
from ..plugins import host, loader

async def in_daemon(function, *args):
    """Run function(*args) in a daemon thread and await its result.

    For calls into plugin code that can take as long as the plugin likes. asyncio.to_thread runs in the loop's
    executor, whose threads asyncio waits for when the dashboard exits (up to five minutes), so a plugin call
    that never returns would keep the process alive after q. This thread is left behind instead.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def finish(setter, value):
        if not future.done():  # the waiter was cancelled (the dashboard is closing)
            setter(value)

    def run():
        try:
            outcome = (future.set_result, function(*args))
        except BaseException as error:
            outcome = (future.set_exception, error)
        try:
            loop.call_soon_threadsafe(finish, *outcome)
        except RuntimeError:  # the loop is closed: the dashboard has gone and nobody is waiting
            pass

    threading.Thread(target=run, daemon=True, name=f"dashboard call: {getattr(function, '__name__', 'plugin')}").start()
    return await future


def universe_name(root):
    """The universe's own name from universe.json, else its directory's (a top universe's directory is `.multiverse`)."""
    from ..store import read
    try:
        return str(read(Path(root) / "universe.json").get("name") or Path(root).name)
    except (OSError, ValueError, AttributeError):
        return Path(root).name


def plugin_of(error):
    """(plugin name, its Loaded or None, its package) when `error` was raised in code of a plugin, else None.

    A plugin's modules live in a package of their own (mv2_plugins.NAME_digest), so a frame of the error's own
    traceback in one says whose code it was, whatever Textual code stands between it and the event loop. Only the
    error's own frames count, never the error it was raised while handling (`__context__`, `__cause__`): a bug of
    the dashboard's that happens while it deals with a plugin's error stays the dashboard's, and ends it. The one
    wrapper looked through is Textual's WorkerFailed, which carries a worker's error without being raised in it."""
    if isinstance(error, WorkerFailed):
        error = error.error
    frames, traceback = [], getattr(error, "__traceback__", None)
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    for frame in frames:
        package = _plugin_package(str(frame.f_globals.get("__name__", "")))
        if package:
            return _owner(package)
    # No plugin code ran, but Textual was working on a plugin's widget: drawing a Static whose text reads as broken
    # markup, drawing what a render() returned, running a timer it set to a core function. The widget a frame works
    # on (innermost first), or a widget it sits in, says whose.
    for frame in reversed(frames):
        for node in _nodes_of(frame):
            for owner in node.ancestors_with_self:
                package = _plugin_package(type(owner).__module__) or getattr(owner, "mv_plugin_package", None)
                if package:
                    return _owner(package)
    return None


def _plugin_package(module):
    """`mv2_plugins.NAME_digest` for a module of a plugin's, else None."""
    if module and module.startswith(loader.PACKAGE + "."):
        return ".".join(module.split(".")[:2])
    return None


def _owner(package):
    found = loader.loaded_by_package(package)
    return (found.name if found else package.split(".")[1].rsplit("_", 1)[0]), found, (found.package if found else package)


def _nodes_of(frame):
    """The widgets (DOM nodes) a Textual frame works on: its `self`, a `widget` it handles, or a timer's target."""
    from textual.dom import DOMNode
    try:
        values = [frame.f_locals.get("self"), frame.f_locals.get("widget")]
    except Exception:
        return []
    found = []
    for value in values:
        target = getattr(value, "_target", None)  # a Timer keeps a weak reference to the widget that set it
        if callable(target) and not isinstance(value, DOMNode):
            try:
                value = target()
            except TypeError:
                value = None
        if isinstance(value, DOMNode):
            found.append(value)
    return found


def error_text(error, kind=True):
    """An error as a person reads it (`kind`: led by its type's name). A stylesheet error carries a renderable whose
    repr says nothing; its messages, with the line of the CSS they are about, are written out instead."""
    from textual.css.stylesheet import StylesheetErrors
    errors = next((arg for arg in getattr(error, "args", ()) if isinstance(arg, StylesheetErrors)), None)
    if errors is None:
        if not kind:
            return str(error) or type(error).__name__
        return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
    from io import StringIO
    from itertools import chain
    from rich.console import Console
    lines = []
    for token, message in dict.fromkeys(chain.from_iterable(rule.errors for rule in errors.rules)):
        console = Console(file=StringIO(), width=200, color_system=None, legacy_windows=False)
        console.print(message)
        text = " ".join(console.file.getvalue().split())
        line = (token.referenced_by or token).location[0] + 1
        lines.append(f"line {line}: {text}")
    return "invalid CSS: " + ("; ".join(lines) or "the stylesheet could not be read")


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
        """{needed, running, stopped: {plugin: reason}, pid, workers, problem} for this tree's host, or None when unreadable.

        `problem` is why the host's recorded status cannot be used (a damaged status file), or None."""
        try:
            info = host.status(self.host_top(root))
        except Exception:
            return None
        return {"needed": bool(info.get("needed")), "running": bool(info.get("running")),
                "stopped": dict(info.get("stopped") or {}), "pid": info.get("pid"),
                "workers": sorted(info.get("workers") or ()), "problem": info.get("problem")}

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
        # A plugin still loading says so instead of holding up the Plugins screen; its real problem, if any,
        # shows when the screen reads again after the load ends (loader.generation() rises).
        return plugins.problems(Path(root), wait=loader.REFRESH_WAIT)

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
            # A plugin still loading (a slow register()) is left out for now; the dashboard reads this again
            # when loader.generation() changes, so it fills in once the plugin has loaded.
            for loaded in loader.active(scope, wait=loader.REFRESH_WAIT):
                for name, function in sorted(loaded.registrations.actions.items()):
                    actions.setdefault((loaded.name, name), (loaded.name, name, loaded, function))
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

    def settings_view(self, root, name):
        """A plugin's own settings screen or fields, loading it now (also where it is off), or why not."""
        return plugins.settings_view(Path(root), name)

    def settings_screen(self, root, name, reference):
        """The plugin's own settings screen class, imported now."""
        found = plugins.entry(Path(root), name)
        return loader.load(found, off=not found.on).resolve(reference)  # a plugin is configured where it is off, too
