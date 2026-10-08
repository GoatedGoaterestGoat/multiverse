"""Moving and removing agents and subverses on disk."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import universe
from multiverse.universe import templates
from multiverse.universe import relocate
from multiverse.universe import trash
from multiverse.cli import parser, run
from multiverse.store import read, write


class RelocateTests(unittest.TestCase):
    def setUp(self):
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
            write(agent_dir / "runtime.json", {"launch_id": "launch", "state": "stopped"})
        (agent_dir / "inbox.jsonl").write_text('{"text": "hello"}\n')
        return agent_dir

    def live(self, *names):
        def observe(properties, _runtime, _snapshots=None):
            alive = properties["name"] in names
            return {"live": "alive" if alive else "stopped", "reason": ""}
        return patch.object(relocate, "observe", observe)

    def test_move_agent_keeps_its_records_and_rebinds_universe(self):
        source = self.agent(self.lab, "worker")
        with self.live():
            result = relocate.move_agent(self.lab, "worker", self.review)
        target = self.review / "state/agents" / source.name
        self.assertFalse(source.exists())
        self.assertEqual((target / "inbox.jsonl").read_text(), '{"text": "hello"}\n')
        self.assertEqual(read(target / "properties.json")["universe_id"], read(self.review / "universe.json")["id"])
        self.assertEqual(result["universe"], str(self.review))
        self.assertEqual(agents.directory(self.review, "worker"), target)

    def test_move_agent_accepts_subverse_path_and_never_launched_agents(self):
        self.agent(self.root, "fresh", launched=False)
        relocate.move_agent(self.root, "fresh", relocate.exact("lab/deep", self.root))
        self.assertEqual(agents.directory(self.deep, "fresh").parent.parent.parent, self.deep)

    def test_move_agent_refusals(self):
        self.agent(self.lab, "worker")
        self.agent(self.review, "worker", harness="codex")
        self.agent(self.lab, "busy")
        self.agent(self.lab, "odd", harness="nonexistent")
        with self.live("busy"):
            for reference, destination, message in (
                    ("worker", self.review, "already has an agent named"),
                    ("worker", self.lab, "already in that universe"),
                    ("busy", self.review, "alive; stop it first"),
                    ("odd", self.review, "no nonexistent harness"),
                    ("worker", self.work / "missing", "Not a universe")):
                with self.subTest(reference=reference, destination=destination), \
                        self.assertRaisesRegex(ValueError, message):
                    relocate.move_agent(self.lab, reference, destination)
        self.assertEqual(len(list((self.lab / "state/agents").iterdir())), 3)

    def test_unknown_liveness_is_not_treated_as_stopped(self):
        self.agent(self.lab, "remote")
        unknown = {"live": "unknown", "reason": "State belongs to a different host"}
        with patch.object(relocate, "observe", return_value=unknown), \
                self.assertRaisesRegex(ValueError, "unknown; stop it first .State belongs to a different host"):
            relocate.remove_agent(self.lab, "remote")

    def test_move_subverse_rewrites_nested_workspaces_for_new_depth(self):
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

    def test_move_subverse_refusals(self):
        self.agent(self.deep, "busy")
        templates.create(self.review / "subverses/lab", "lab", workdir=self.work)
        with self.live("busy"):
            for path, destination, message in (
                    (self.lab, self.deep, "into itself"),
                    (self.lab, self.lab, "into itself"),
                    (self.lab, self.root, "already in that universe"),
                    (self.root, self.review, "top-level universe"),
                    (self.lab, self.review, "already has a subverse named lab")):
                with self.subTest(path=path, destination=destination), self.assertRaisesRegex(ValueError, message):
                    relocate.move_subverse(path, destination)
            with self.assertRaisesRegex(ValueError, "busy is alive"):
                relocate.move_subverse(self.deep, self.root)
        self.assertTrue(self.deep.is_dir())
        self.assertFalse(os.path.isabs(read(self.deep / "universe.json")["workspace"]))

    def test_remove_agent_goes_to_trash_and_can_be_restored(self):
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

    def test_remove_subverse_and_empty_trash(self):
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

    def test_trash_names_do_not_collide_and_have_no_timezone_suffix(self):
        for _ in range(2):
            self.agent(self.lab, "worker")
            with self.live():
                relocate.remove_agent(self.lab, "worker")
        names = sorted(p.name for p in (self.lab / "trash").iterdir())
        self.assertEqual(len(names), 2)
        self.assertRegex(names[0], r"^\d{8}T\d{6}-worker-")

    def test_cli_move_remove_and_trash(self):
        self.agent(self.lab, "worker")
        commands = (["-u", str(self.lab), "move", "worker", str(self.review)],
                    # Both names resolve from -u, not from the subverse's own parent.
                    ["-u", str(self.root), "move", "--subverse", "lab", "review"],
                    ["-u", str(self.review), "remove", "worker"],
                    ["-u", str(self.root), "remove", "--subverse", "review/lab"],
                    ["-u", str(self.root), "trash"])
        with self.live():
            results = [run(parser().parse_args(argv)) for argv in commands]
        self.assertEqual(results[0]["universe"], str(self.review))
        self.assertEqual(results[1]["universe"], str(self.review / "subverses/lab"))
        self.assertEqual({e["name"] for e in results[4]["entries"]}, {"worker", "lab"})
        json.dumps(results)

    def test_cli_resolves_nested_target_and_destination_from_the_same_universe(self):
        # deep's parent is lab, but "review" names a subverse of the -u universe.
        result = run(parser().parse_args(["-u", str(self.root), "move", "--subverse", "lab/deep", "review"]))
        self.assertEqual(result["universe"], str(self.review / "subverses/deep"))


if __name__ == "__main__":
    unittest.main()
