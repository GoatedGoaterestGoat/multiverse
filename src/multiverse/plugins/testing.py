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
import importlib
import json
import os
from pathlib import Path
import tempfile
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
    plugin = json.loads((directory / "plugin.json").read_text())["name"]
    with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(directory)}):
        found = loader.entry(None, plugin)
    if Path(found.directory).resolve() != directory:
        raise RuntimeError(f"mv2 found {plugin} at {found.directory}, not {directory}")
    return importlib.import_module(f"{loader.load(found, off=True).package}.{name}")


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
