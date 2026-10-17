"""unittest helpers for plugin developers, and for mv2's own plugin tests.

    from multiverse.plugins.testing import PluginTestCase

    class MyPluginTest(PluginTestCase):
        plugin = Path(__file__).parents[1]      # the directory with plugin.json

        def test_column(self):
            column = self.loaded().registrations.columns["uptime"]
            self.assertEqual(column.value({"name": "worker"}), "—")

Each test gets a temporary universe made from the base template, a temporary
home (so ~/.multiverse is never the developer's), a temporary Claude config
directory (CLAUDE_CONFIG_DIR), a bin/ directory first on PATH for fake
executables, no inherited agent identity, and `plugin` found through
MV2_PLUGIN_PATH and turned on. Nothing here starts an agent, tmux or a native
harness: `agent()` writes an agent's records and `executable()` a fake program.
"""
import atexit
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from . import config, loader
from .. import plugins
from ..universe import templates

def plugin_module(directory, name):
    """A module of the plugin at `directory` (`name` such as "engine.store"), imported as mv2 imports it:
    one copy per process, with no universe and whether or not the plugin is on anywhere (API 1.9).

    For test modules that import a plugin's own modules at module level, before
    any test's universe exists. Its relative imports work as they do in mv2."""
    directory = Path(directory).resolve()
    # Exactly this directory, as a developer's (MV2_PLUGIN_PATH) plugin: no plugin discovery, which would scan
    # installed packages and cache that in the home of whatever runs it (the real one, at a test module's import).
    from .manifest import load as load_manifest
    if not (directory / "plugin.json").is_file():
        raise FileNotFoundError(f"{directory / 'plugin.json'}: no such plugin")
    manifest = load_manifest(directory)
    found = loader._entry(None, manifest.name, False, "path", directory, manifest, [])
    return importlib.import_module(f"{loader.load(found, off=True).package}.{name}")


# The guard: no test writes under the real ~/.multiverse. An audit hook (in native_safety's style) refuses
# writes, renames, removals, directories and mode changes there with PermissionError, so the real home is never
# changed, and records each one, so the test fails (check(), which PluginTestCase calls when it ends) or, for a
# thread that outlived its test, the process exits 1 at its end.

_protected = set()  # absolute directories no test may change
_exempt = set()  # directories inside them that are not mv2's state: the plugin under test (PluginTestCase.plugin)
_violations = []  # {"path", "event", "test", "thread", "unreported"}
_current = None  # the test running now
_installed = False
_strict = False  # whether writes no check() reported make the process exit 1 (the default once installed)
_TEST = "_mv2_test"  # on a Thread: the test it was started in (threads it starts carry it on)
_WRITES = {"os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.chmod", "os.utime", "os.link", "os.symlink",
           "os.truncate", "shutil.rmtree", "shutil.copyfile", "shutil.move"}
_REAL_HOME = os.path.join(os.path.expanduser("~"), ".multiverse")  # captured at import, before a test moves HOME


def install(home=None, strict=True):
    """Guard this process (once; later calls add directories): no test writes under `home`, by default the real
    ~/.multiverse as it was when this module was imported. Returns the directory now protected.

    A refused write counts against the test it came from: the test running then, or for a thread the test that
    started it. Strict (the default): writes that no check() reported (no test named itself, or a thread outlived
    its test) make the process exit 1 at its end, after they are printed. strict=False only prints them; it never
    makes a process that another caller installed strict lenient again."""
    global _installed, _strict
    _strict = _strict or strict
    given = os.path.normpath(os.path.abspath(home if home is not None else _REAL_HOME))
    directory = os.path.realpath(given)
    _protected.update({given, directory})  # as written and as resolved (/tmp, /var are links on macOS)
    if not _installed:
        _installed = True
        start = threading.Thread.start

        def tagged_start(thread, *args, **kwargs):
            setattr(thread, _TEST, getattr(threading.current_thread(), _TEST, None) or _current)
            return start(thread, *args, **kwargs)
        threading.Thread.start = tagged_start
        sys.addaudithook(_hook)
        atexit.register(_at_exit)
    return Path(directory)


def running(name):
    """Name the test running now (None between tests): violations say which test they happened in."""
    global _current
    _current = name


def violations():
    """Every write the guard refused since install(), as copies."""
    return [dict(item) for item in _violations]


def check(test=None):
    """Raise AssertionError for refused writes not reported yet, and mark them reported: those of `test` when given
    (what a test's own cleanup asks: never another test's), else all."""
    found = [item for item in _violations if item["unreported"] and (test is None or item["test"] == test)]
    for item in found:
        item["unreported"] = False
    if found:
        raise AssertionError("a test wrote under the real ~/.multiverse (refused): " + "; ".join(
            f"{item['event']} {item['path']} in {item['test'] or 'no test'} ({item['thread']})" for item in found))


def _record(event, path):
    thread = threading.current_thread()
    _violations.append({"path": path, "event": event, "test": getattr(thread, _TEST, None) or _current,
                        "thread": thread.name, "unreported": True})


# For each event the guard watches: (index of a path the call changes, index of the directory fd it is relative
# to, or None), from the audit events table. A path read but not changed (a link's target) is not listed.
_TARGETS = {"os.mkdir": [(0, 2)], "os.rmdir": [(0, 1)], "os.remove": [(0, 1)], "os.chmod": [(0, 2)],
            "os.utime": [(0, 3)], "os.rename": [(0, 2), (1, 3)], "os.link": [(1, 3)], "os.symlink": [(1, 2)],
            "os.truncate": [(0, None)], "shutil.rmtree": [(0, 1)], "shutil.copyfile": [(1, None)],
            "shutil.move": [(0, None), (1, None)]}


def _hook(event, args):
    if event == "open":
        if len(args) < 3 or not _writes(args[1], args[2]):
            return
        targets = [(args[0], None)]
    elif event in _TARGETS:
        targets = [(args[at] if at < len(args) else None, args[fd] if fd is not None and fd < len(args) else None)
                   for at, fd in _TARGETS[event]]
    else:
        return
    for path, directory in targets:
        where = _absolute(path, directory)
        if where is None:
            continue
        # As written and as resolved: a link elsewhere may point into the home. realpath resolves the links that
        # exist and keeps a new file's own name, so a file about to be made through one is judged by where it goes.
        for candidate in dict.fromkeys((where, os.path.realpath(where))):
            if _guarded(candidate):
                _record(event, candidate)
                raise PermissionError(f"a test may not change the real ~/.multiverse: {candidate}")


def _guarded(where):
    return any(where == top or where.startswith(top + os.sep) for top in _protected) and not _allowed(where)


def _allowed(where):
    """Not mv2's state although inside the home: the plugin under test's own directory, and Python's bytecode in
    a personal plugin (~/.multiverse/plugins/NAME), which importing its test modules writes before any test runs."""
    if any(where == top or where.startswith(top + os.sep) for top in _exempt):
        return True
    parts = where.split(os.sep)
    return ("__pycache__" in parts or where.endswith((".pyc", ".pyo"))) and any(
        where.startswith(os.path.join(top, "plugins") + os.sep) for top in _protected)


def _writes(mode, flags):
    if isinstance(mode, str):
        return any(letter in mode for letter in "wax+")
    return isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))


def _absolute(path, directory=None):
    """The absolute path a call changes: relative to `directory` (a dir_fd, as shutil.rmtree passes) when given,
    else to the working directory; None when it cannot be told (a file descriptor, or a dir_fd whose path this
    system cannot give), which is then not counted."""
    if isinstance(path, int) or path is None:
        return None
    try:
        path = os.fsdecode(os.fspath(path))
    except TypeError:
        return None
    if os.path.isabs(path):
        return os.path.normpath(path)
    if isinstance(directory, int) and not isinstance(directory, bool) and directory >= 0:  # AT_FDCWD is negative
        base = _fd_path(directory)
        return os.path.normpath(os.path.join(base, path)) if base else None
    return os.path.normpath(os.path.join(os.getcwd(), path))


def _fd_path(fd):
    """The path of an open directory: /proc/self/fd on Linux, F_GETPATH on macOS; None elsewhere or if closed."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        pass
    try:
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0]) or None
    except (OSError, ValueError):
        pass
    return None


def _exit_status(strict=None):
    """1 when a strict process has refused writes no check() reported, else 0."""
    strict = _strict if strict is None else strict
    return 1 if strict and any(item["unreported"] for item in _violations) else 0


def _at_exit():
    found = [item for item in _violations if item["unreported"]]
    if not found:
        return
    print("mv2 tests: writes under the real ~/.multiverse were refused but never reported:", file=sys.stderr)
    for item in found:
        print(f"  {item['event']} {item['path']} in {item['test'] or 'no test'} ({item['thread']})",
              file=sys.stderr)
    if _exit_status():
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


CLEARED = ("MV2_UNIVERSE", "MV2_AGENT_ID", "MV2_AGENT_DIR", "MV2_LAUNCH_ID", "MV2_CONTROL_SOCKET",
           "MV2_PLUGIN_PATH", "CLAUDE_CONFIG_DIR", "CODEX_HOME", loader.SAFE_MODE)


class PluginTestCase(unittest.TestCase):
    # The plugin under test: a directory with plugin.json, or None to write plugins in the test.
    plugin = None
    # Whether mv2's bundled plugins (such as typeahead, on by default), the checkout's submodule plugins and
    # pip-installed plugins are found too. Off, a test sees only its own plugins, whatever is installed.
    bundled = False

    def setUp(self):
        super().setUp()
        install()  # before anything here moves HOME; checked last, once everything else is cleaned up
        running(self.id())
        self.addCleanup(check, self.id())
        if self.plugin is not None:  # its own directory is its developer's code, not mv2's state
            own = {os.path.normpath(os.path.abspath(self.plugin)), os.path.realpath(self.plugin)}
            _exempt.update(own)
            self.addCleanup(_exempt.difference_update, own)
        self.addCleanup(running, None)
        temp = tempfile.TemporaryDirectory(prefix="mv2-plugin-test-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name).resolve()
        self.home = self.work / "home"
        self.home.mkdir()
        self.plugins_dir = self.work / "plugins"
        self.plugins_dir.mkdir()
        environment = {key: value for key, value in os.environ.items() if key not in CLEARED}
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.claude_config = self.work / "claude-config"
        self.claude_config.mkdir()
        environment.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.claude_config),
                           PATH=os.pathsep.join([str(self.bin), environment.get("PATH", "")]),
                           MV2_PLUGIN_PATH=os.pathsep.join(
                               str(path) for path in (self.plugin, self.plugins_dir) if path is not None))
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        loader.reset()
        self.addCleanup(loader.reset)
        if not self.bundled:
            empty = self.work / "no-bundled-plugins"
            empty.mkdir()
            for patcher in (mock.patch.object(loader, "BUNDLED", empty), mock.patch.object(loader, "CHECKOUT", None),
                            mock.patch.object(loader, "_installed", [])):
                patcher.start()
                self.addCleanup(patcher.stop)
        previous = loader.process
        self.addCleanup(setattr, loader, "process", previous)
        loader.process = "cli"
        self.root = templates.create(self.work / "project" / ".multiverse", "test", workdir=self.work / "project")
        if self.plugin is not None:
            name = json.loads((Path(self.plugin) / "plugin.json").read_text())["name"]
            self.enable(name)

    def write_plugin(self, name, manifest=None, files=None, where=None):
        """A plugin directory NAME with plugin.json and `files` ({relative path: text}); returns it."""
        directory = Path(where or self.plugins_dir) / name
        directory.mkdir(parents=True, exist_ok=True)
        data = {"schema": 1, "name": name, "version": "0.1.0", "api": "1.0", **(manifest or {})}
        (directory / "plugin.json").write_text(json.dumps(data, indent=2) + "\n")
        for relative, text in (files or {}).items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return directory

    def enable(self, name, on=True, root=None):
        return config.set_enabled(root or self.root, name, on)

    def subverse(self, name):
        return templates.create(self.root / "subverses" / name, name, workdir=self.work / "project")

    def entry(self, name, root=None):
        return plugins.entry(root or self.root, name)

    def loaded(self, name=None, root=None):
        """The plugin loaded and registered, as a process that needs it would load it."""
        if name is None:
            name = json.loads((Path(self.plugin) / "plugin.json").read_text())["name"]
        return loader.load(self.entry(name, root))

    def module(self, name):
        """A module of the plugin under test (`plugin`), as mv2 loaded it for this test (API 1.9)."""
        return importlib.import_module(f"{self.loaded().package}.{name}")

    def dashboard(self, size=(120, 40), root=None):
        """The real dashboard on this universe with the plugins really loaded, driven by verbs (API 1.9).

        Use it with `async with` in a test that is also an IsolatedAsyncioTestCase; see
        multiverse.plugins.dashboard_testing for what a test may rely on."""
        from .dashboard_testing import Dashboard
        return Dashboard(self, size=size, root=root)

    def agent(self, name, harness="claude", root=None, native_id=None, runtime=None, **properties):
        """Write an agent's records (properties.json, runtime.json) and return its directory. No process starts."""
        from uuid import uuid4
        from ..store import read, write
        root = Path(root or self.root)
        identity = str(uuid4())
        directory = root / "state" / "agents" / identity
        write(directory / "properties.json", {
            "id": identity, "name": name, "harness": harness, "task": "", "phase": "working", "summary": "",
            "universe_id": read(root / "universe.json")["id"], "workspace": str(self.work / "project"),
            **({"native_session_id": native_id} if native_id else {}), **properties})
        write(directory / "runtime.json", {"launch_id": "launch-1", "state": "running", "status": "idle",
                                           **(runtime or {})})
        return directory

    def claude_transcript(self, native_id, records, project="project"):
        """Write a Claude session transcript (JSON lines) where Claude keeps it, under CLAUDE_CONFIG_DIR."""
        path = self.claude_config / "projects" / project / f"{native_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return path

    def executable(self, name, script):
        """A fake program on PATH: `script` is its shell body (#!/bin/sh is added)."""
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(0o755)
        return path
