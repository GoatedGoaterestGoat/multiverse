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
from multiverse.plugins import api, events, harness, host, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.universe import relocate


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

    def test_late_plugin_writes_never_bring_back_a_removed_agent(self):
        """No plugin write, event or delivery recreates the folder of an agent that was removed, moved to another
        universe or lost with its subverse, while writes for a present agent and paths outside agent folders still
        work; fragile because every writer reaches for mkdir(parents=True), and the folder comes back later as a
        record-less orphan (2ef80f6)."""
        with self.subTest("while the agent is there"):
            state = self.mv.agent_state_dir(self.worker)
            self.assertEqual(state, self.folder / "writer")
            api.files.write_json(state / "deep" / "er" / "x.json", {"a": 1})
            self.assertEqual(json.loads((state / "deep" / "er" / "x.json").read_text()), {"a": 1})
            with api.files.lock(state / "locks" / ".x.lock"):
                pass
            self.assertEqual(api.files.ensure_dir(state / "a" / "b"), state / "a" / "b")
            self.mv.statusline.write(self.worker, "note", "hello")
            self.assertEqual(harness.item_path(self.folder, "note").read_text(), "hello\n")
        with self.subTest("outside agent folders"):
            target = self.root / "state" / "plugins" / "writer" / "deep" / "x.json"
            api.files.write_json(target, {"b": 2})
            self.assertEqual(json.loads(target.read_text()), {"b": 2})
            with api.files.lock(self.root / "state" / "plugins" / "writer" / "other" / ".lock"):
                pass
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
        with self.subTest("moved to another universe"):
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
        with self.subTest("in a subverse that was removed"):
            sub = self.subverse("doomed")
            folder = self.agent("inside", root=sub)
            os.rename(sub, self.work / "trashed-subverse")
            with self.assertRaises(api.AgentGone):
                api.files.ensure_dir(folder / "writer")
            with self.assertRaises(api.AgentGone):
                with api.files.lock(folder / "writer" / ".lock"):
                    pass
            host._write_cursor(sub, "writer", 5)  # the host's cursor and delivery there
            host._deliver(self.loaded("writer"), self.mv, sub, "writer")
            self.assertFalse(sub.exists(), "a late write brought the removed subverse back")
        with self.subTest("core's moves and removals reach both journals"):
            self.write_plugin("listener", {"extends": ["events"], "events": ["*"]})
            self.enable("listener")
            child = self.subverse("child")
            self.enable("listener", root=child)
            folder = self.agent("traveller", runtime={"launch_id": "L1", "state": "stopped"})
            with mock.patch.object(relocate, "_stopped", return_value=True), \
                 mock.patch("multiverse.universe.relocate.observe", return_value={"live": "dormant"}, create=True):
                relocate.move_agent(self.root, "traveller", child)
            self.assertFalse(folder.exists(), "recording the move brought the agent back")
            for scope in (self.root, child):
                [moved] = [event for event in events.read(scope, 0)[0] if event["type"] == "agent.moved"]
                self.assertEqual(moved["data"], {"from": str(self.root), "to": str(child)})
            created = [event for event in events.read(self.root, 0)[0] if event["type"] == "universe.created"]
            self.assertEqual([event["data"]["name"] for event in created], ["child"])
            leaver = self.agent("leaver", runtime={"launch_id": "L2", "state": "stopped"})
            with mock.patch.object(relocate, "_stopped", return_value=True):
                relocate.remove_agent(self.root, "leaver")
            [removed] = [event for event in events.read(self.root, 0)[0] if event["type"] == "agent.removed"]
            self.assertEqual(removed["agent"]["name"], "leaver")
            self.assertFalse(leaver.exists(), "recording the removal brought the agent back")

    def test_a_removal_between_two_steps_still_wins(self):
        """A removal that lands between two steps of a plugin's directory creation still wins; fragile because an
        existence check then a makedirs is the natural code, and the window is too short to see by hand (2ef80f6)."""
        real = Path.mkdir

        def removed_meanwhile(path, *args, **kwargs):
            if path == self.folder / "writer" and self.folder.exists():
                os.rename(self.folder, self.work / "trashed-agent")  # right before the plugin's folder is made
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, "mkdir", removed_meanwhile):
            with self.assertRaises(api.AgentGone):
                api.files.write_json(self.folder / "writer" / "x.json", {})
        self.assertFalse(self.folder.exists(), "the removed agent came back")
