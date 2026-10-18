"""Moving and removing agents and subverses on disk."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import universe
from multiverse.universe import templates
from multiverse.universe import relocate
from multiverse.universe import trash
from multiverse.store import read, write
from isolation import isolate


class RelocateTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name) / "work"
        self.work.mkdir()
        self.root = templates.create(self.work / ".multiverse", "top", workdir=self.work)
        self.lab = templates.create(self.root / "subverses/lab", "lab", workdir=self.work)
        self.deep = templates.create(self.lab / "subverses/deep", "deep", workdir=self.work)
        self.review = templates.create(self.root / "subverses/review", "review", workdir=self.work)

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, scope, name, harness="claude", launched=True):
        agent_id = f"{name}-0000-0000-0000-000000000000"[:36]
        agent_dir = scope / "state/agents" / agent_id
        write(agent_dir / "properties.json", {"id": agent_id, "name": name, "harness": harness,
                                              "universe_id": read(scope / "universe.json")["id"],
                                              "workspace": str(self.work), "task": "", "phase": "done"})
        if launched:
            write(agent_dir / "runtime.json", {"launch_id": str(uuid4()), "state": "stopped"})
        (agent_dir / "inbox.jsonl").write_text('{"text": "hello"}\n')
        return agent_dir

    def live(self, *names):
        def observe(properties, _runtime, _snapshots=None):
            alive = properties["name"] in names
            return {"live": "alive" if alive else "stopped", "reason": ""}
        return patch.object(relocate, "observe", observe)

    def test_move_subverse_rewrites_nested_workspaces_for_new_depth(self):
        """Moving a subverse rewrites the relative workspace of it and its nested subverses for the new depth; a break
        goes unnoticed until an agent resumes in the wrong directory, because the .. chain is wrong only at another
        depth."""
        self.agent(self.deep, "inner")
        with self.live():
            result = relocate.move_subverse(self.lab, self.review)
        moved = self.review / "subverses/lab"
        self.assertEqual(Path(result["universe"]), moved)
        self.assertFalse(self.lab.exists())
        for scope in (moved, moved / "subverses/deep"):
            meta = read(scope / "universe.json")
            self.assertFalse(os.path.isabs(meta["workspace"]), meta["workspace"])
            self.assertEqual(universe.workspace(scope), self.work.resolve())
        self.assertEqual([p.name for p in universe.children(self.root)], ["review"])
        self.assertTrue((moved / "subverses/deep/state/agents").is_dir())

    def test_removed_records_go_to_trash_without_collisions(self):
        """remove moves records to trash, restorable with their origin, and two removals of one name in the same second
        do not collide; fragile because trash names are timestamp-based, and a collision silently overwrites a trashed
        agent."""
        with self.subTest('an agent goes to the trash and can be restored'):
            source = self.agent(self.lab, "worker")
            with self.live():
                result = relocate.remove_agent(self.lab, "worker")
            entry = Path(result["trash"])
            self.assertFalse(source.exists())
            self.assertEqual(entry.parent, self.lab / "trash")
            self.assertEqual(read(entry / trash.RECORD)["origin"], str(source))
            self.assertEqual(lifecycle.listing(self.lab), [])
            listed = trash.contents(self.root)["entries"]
            self.assertEqual([(e["kind"], e["name"]) for e in listed], [("agent", "worker")])
            (entry / trash.RECORD).unlink()
            entry.rename(source)
            self.assertEqual(agents.directory(self.lab, "worker"), source)
        with self.subTest('two removals of one name do not collide'):
            for _ in range(2):
                self.agent(self.lab, "worker")
                with self.live():
                    relocate.remove_agent(self.lab, "worker")
            names = sorted(p.name for p in (self.lab / "trash").iterdir())
            self.assertEqual(len(names), 2)
            self.assertRegex(names[0], r"^\d{8}T\d{6}-worker-")
        with self.subTest('a subverse goes to the trash, and emptying it deletes it'):
            self.agent(self.deep, "inner")
            with self.live("inner"), self.assertRaisesRegex(ValueError, "inner is alive"):
                relocate.remove_subverse(self.lab)
            with self.live():
                result = relocate.remove_subverse(self.lab)
            entry = Path(result["trash"])
            self.assertEqual(result["agents"], 1)
            self.assertEqual(entry.parent, self.root / "trash")
            self.assertEqual([p.name for p in universe.children(self.root)], ["review"])
            self.assertEqual(universe.workspace(entry / "subverses/deep"), self.work.resolve())
            with self.assertRaisesRegex(ValueError, "top-level"):
                relocate.remove_subverse(self.root)
            self.assertEqual(len(trash.contents(self.root)["entries"]), 1)
            self.assertEqual(len(trash.contents(self.root, empty=True)["entries"]), 1)
            self.assertFalse(entry.exists())
            self.assertEqual(trash.contents(self.root), {"entries": [], "emptied": False})


if __name__ == "__main__":
    unittest.main()
