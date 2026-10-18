"""A plugin's late write never brings back a universe that was removed: its root (the folder holding
universe.json and state/) is never made again by mv.state_dir, mv.log, files under state/plugins/NAME/ or
mv2's own plugin logs. Below an existing root, directories are made one level at a time without their parents."""
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, events, host, loader
from multiverse.plugins.testing import PluginTestCase


class UniverseGoneTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(loader.reset)
        self.write_plugin("writer", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"})
        self.enable("writer")
        self.loaded_writer = self.loaded("writer")
        self.mv = api.Plugin(self.loaded_writer)
        self.sub = self.subverse("doomed")
        self.universe = self.mv.universe(self.sub)

    def removed(self):
        os.rename(self.sub, self.work / "trashed-subverse")  # as core removal does: renamed away first

    def test_a_removed_universe_is_never_made_again(self):
        """Every plugin writer and log refuses a removed universe's root: easy to break and unnoticed, since any
        mkdir with parents (or store.write) on such a path quietly makes an orphan root with no universe.json."""
        state = self.sub / "state" / "plugins" / "writer"

        def locked():
            with api.files.lock(state / ".x.lock"):
                pass
        writes = {  # entry point -> (call, raises): logs are best effort and write nothing
            "state_dir": (lambda: self.mv.state_dir(self.universe), api.UniverseGone),
            "files.write_json": (lambda: api.files.write_json(state / "x.json", {}), api.UniverseGone),
            "files.ensure_dir": (lambda: api.files.ensure_dir(state / "a"), api.UniverseGone),
            "files.lock": (locked, api.Gone),
            "log": (lambda: self.mv.log("note", self.universe), None),
            "record": (lambda: loader.record(self.loaded_writer, "call", RuntimeError("x"), self.sub), None),
            "events log": (lambda: events._log(self.sub, "note"), None),
            "host log": (lambda: host._log(self.sub, "note"), None),
        }
        for name, (call, _) in writes.items():  # the healthy control: while it is there, each writes
            with self.subTest("present", writer=name):
                call()
        self.assertIn("note", (state / "plugin.log").read_text())
        self.removed()
        for name, (call, raises) in writes.items():
            with self.subTest("removed", writer=name):
                if raises is None:
                    call()
                else:
                    with self.assertRaises(raises):
                        call()
                self.assertFalse(self.sub.exists(), f"{name} brought the removed universe back")

    def test_a_removal_between_two_steps_still_wins(self):
        """A removal right before the plugin's own folder is made still wins: an existence check first, then a
        mkdir with parents, would pass the check and then make the removed root again."""
        real = Path.mkdir
        target = self.sub / "state" / "plugins" / "writer"

        def removed_meanwhile(path, *args, **kwargs):
            if path == target and self.sub.exists():
                self.removed()
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, "mkdir", removed_meanwhile):
            with self.assertRaises(api.UniverseGone):
                self.mv.state_dir(self.universe)
        self.assertFalse(self.sub.exists(), "the removed universe came back")
