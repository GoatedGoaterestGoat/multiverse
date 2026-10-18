"""A plugin's late write never brings back the folder of an agent that was removed (state/agents/<id>): it
would come back as an agent with no records, an orphan. Directories below an agent's folder are made one level
at a time, never with their parents, so a removal at any moment, even between two steps, wins."""
import json
import os
from pathlib import Path
import shutil
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, harness, loader
from multiverse.plugins.testing import PluginTestCase


class AgentGoneTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(loader.reset)
        self.write_plugin("writer", {"module": "plugin", "extends": ["statusline"],
                                     "statusline": {"note": {"claude": True}}, "permissions": ["agents.read"]},
                          {"plugin.py": "def register(mv):\n    pass\n"})
        self.enable("writer")
        self.mv = api.Plugin(self.loaded("writer"))
        self.folder = self.agent("worker")
        self.worker = self.mv.universe(self.root).agent("worker")

    def removed(self, how="trash"):
        if how == "trash":
            os.rename(self.folder, self.work / "trashed-agent")  # as core removal does: renamed away first
        else:
            shutil.rmtree(self.folder)
        self.assertFalse(self.folder.exists())

    def test_while_the_agent_is_there_everything_is_written(self):
        state = self.mv.agent_state_dir(self.worker)
        self.assertEqual(state, self.folder / "writer")
        api.files.write_json(state / "deep" / "er" / "x.json", {"a": 1})
        self.assertEqual(json.loads((state / "deep" / "er" / "x.json").read_text()), {"a": 1})
        with api.files.lock(state / "locks" / ".x.lock"):
            pass
        self.assertEqual(api.files.ensure_dir(state / "a" / "b"), state / "a" / "b")
        self.mv.statusline.write(self.worker, "note", "hello")
        self.assertEqual(harness.item_path(self.folder, "note").read_text(), "hello\n")

    def test_after_removal_nothing_brings_the_folder_back(self):
        for how in ("trash", "deleted"):
            with self.subTest(how):
                self.folder = self.agent("worker-" + how)
                worker = self.mv.universe(self.root).agent("worker-" + how)
                self.removed(how)
                with self.assertRaises(api.AgentGone):
                    self.mv.agent_state_dir(worker)
                with self.assertRaises(api.AgentGone):
                    api.files.write_json(self.folder / "writer" / "x.json", {})
                with self.assertRaises(api.AgentGone):
                    api.files.write_json(self.folder / "x.json", {})
                with self.assertRaises(api.AgentGone):
                    with api.files.lock(self.folder / "writer" / ".x.lock"):
                        pass
                with self.assertRaises(api.AgentGone):
                    api.files.ensure_dir(self.folder / "writer" / "a")
                self.mv.statusline.write(worker, "note", "late")  # never raises
                self.mv.statusline.write(self.folder, "note", "late")
                self.assertFalse(self.folder.exists(), "a late write brought the removed agent back")

    def test_a_removal_between_two_steps_still_wins(self):
        real = Path.mkdir

        def removed_meanwhile(path, *args, **kwargs):
            if path == self.folder / "writer" and self.folder.exists():
                os.rename(self.folder, self.work / "trashed-agent")  # right before the plugin's folder is made
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, "mkdir", removed_meanwhile):
            with self.assertRaises(api.AgentGone):
                api.files.write_json(self.folder / "writer" / "x.json", {})
        self.assertFalse(self.folder.exists(), "the removed agent came back")

    def test_paths_outside_agent_folders_get_their_parents_as_before(self):
        target = self.root / "state" / "plugins" / "writer" / "deep" / "x.json"
        api.files.write_json(target, {"b": 2})
        self.assertEqual(json.loads(target.read_text()), {"b": 2})
        with api.files.lock(self.root / "state" / "plugins" / "writer" / "other" / ".lock"):
            pass


class MovedOrRemovedUniverseTest(PluginTestCase):
    """The old path of an agent moved to another universe, or in a subverse that was removed, stays gone."""

    def setUp(self):
        super().setUp()
        self.addCleanup(loader.reset)
        self.write_plugin("writer", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"})
        self.enable("writer")
        self.mv = api.Plugin(self.loaded("writer"))

    def test_an_agent_moved_to_another_universe(self):
        folder = self.agent("mover")
        other = self.subverse("other")
        target = other / "state" / "agents" / folder.name
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(folder, target)
        with self.assertRaises(api.AgentGone):
            api.files.write_json(folder / "writer" / "x.json", {})
        self.assertFalse(folder.exists())
        api.files.write_json(target / "writer" / "x.json", {"here": True})  # at its new place it is fine
        self.assertTrue((target / "writer" / "x.json").is_file())

    def test_an_agent_in_a_subverse_that_was_removed(self):
        sub = self.subverse("doomed")
        folder = self.agent("inside", root=sub)
        os.rename(sub, self.work / "trashed-subverse")
        with self.assertRaises(api.AgentGone):
            api.files.ensure_dir(folder / "writer")
        with self.assertRaises(api.AgentGone):
            with api.files.lock(folder / "writer" / ".lock"):
                pass
        self.assertFalse(sub.exists(), "a late write brought the removed subverse back")
