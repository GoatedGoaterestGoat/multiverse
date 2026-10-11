"""unittest helpers for plugin developers, and for mv2's own plugin tests.

    from multiverse.plugins.testing import PluginTestCase

    class MyPluginTest(PluginTestCase):
        plugin = Path(__file__).parents[1]      # the directory with plugin.json

        def test_column(self):
            column = self.loaded().registrations.columns["uptime"]
            self.assertEqual(column.value({"name": "worker"}), "—")

Each test gets a temporary universe made from the base template, a temporary
home (so ~/.multiverse is never the developer's), no inherited agent identity,
and `plugin` found through MV2_PLUGIN_PATH and turned on. Nothing here starts an
agent, tmux or a native harness.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from . import config, loader
from .. import plugins
from ..universe import templates

CLEARED = ("MV2_UNIVERSE", "MV2_AGENT_ID", "MV2_AGENT_DIR", "MV2_LAUNCH_ID", "MV2_CONTROL_SOCKET",
           "MV2_PLUGIN_PATH", loader.SAFE_MODE)


class PluginTestCase(unittest.TestCase):
    # The plugin under test: a directory with plugin.json, or None to write plugins in the test.
    plugin = None

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
        environment.update(HOME=str(self.home), MV2_PLUGIN_PATH=os.pathsep.join(
            str(path) for path in (self.plugin, self.plugins_dir) if path is not None))
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        loader.reset()
        self.addCleanup(loader.reset)
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
