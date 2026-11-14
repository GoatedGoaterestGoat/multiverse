"""Refresh updates unedited definition files from the template and never overwrites local edits."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.universe import shipped_harnesses, templates
from multiverse.store import read, write
from multiverse.tools import refresh
from isolation import isolate


class RefreshTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.template = self.work / "template"
        write(self.template / "universe.json", {"schema": 1, "name": "template", "workspace": ".."})
        self.put("bootstrap.md", "Bootstrap v1\n")
        self.put("skills/coordination/SKILL.md", "Coordinate v1\n")
        self.put("harnesses/codex/harness.json", '{"schema": 2, "adapter": "codex", "settings": {}}\n')
        write(self.template / "subverses" / "child" / "universe.json", {"schema": 1, "name": "child", "workspace": ".."})
        self.put("subverses/child/bootstrap.md", "Child v1\n")
        self.root = templates.create(self.work / "project" / ".multiverse", "project", str(self.template), self.work / "project")

    def put(self, path, text, base=None):
        target = (base or self.template) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def plan(self, **options):
        return refresh.run(self.root, **options)["universes"][0]

    def actions(self, **options):
        return {change["path"]: change["action"] for change in self.plan(**options)["changes"]}

    def test_refresh_never_overwrites_or_resurrects_local_state(self):
        """refresh decides each file from manifest, template and local hashes: unedited files update with a backup,
        edits and deletions are kept, a missing manifest means unknown, a file new to the template is added, take
        refuses paths outside it, and a template that is gone is refused naming the universe and how to compare;
        fragile because a missing manifest flips the default, and a break silently overwrites a user's edited bootstrap
        or harness.json, including copying a parent's template over a subverse's own source (LC-7, LC-13, LC-14)."""
        manifest = (self.root / templates.MANIFEST).read_text()
        with self.subTest('a dry run writes nothing'):
            self.put("bootstrap.md", "Bootstrap v2\n")
            self.put("skills/review/SKILL.md", "Review v1\n")
            self.assertEqual(self.actions(), {"bootstrap.md": "update", "skills/review/SKILL.md": "add"})
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v1\n")
            self.assertFalse((self.root / "skills/review/SKILL.md").exists())
            self.assertEqual((self.root / templates.MANIFEST).read_text(), manifest)
            self.assertFalse((self.root / "trash").exists())
        with self.subTest('apply updates unedited files, adds new ones and saves the replaced copy'):
            result = self.plan(apply=True)
            self.assertEqual(sorted(result["applied"]["written"]), ["bootstrap.md", "skills/review/SKILL.md"])
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v2\n")
            self.assertEqual((self.root / "skills/review/SKILL.md").read_text(), "Review v1\n")
            backup = Path(result["applied"]["trash"])
            self.assertEqual((backup / "bootstrap.md").read_text(), "Bootstrap v1\n")
            self.assertEqual(read(backup / ".removed.json")["files"], ["bootstrap.md"])
            self.assertEqual(self.plan()["changes"], [])
        with self.subTest('local edits are kept, and conflicts need take'):
            self.put("bootstrap.md", "My bootstrap\n", self.root)
            self.assertEqual(self.actions(), {"bootstrap.md": "local"})
            self.put("bootstrap.md", "Bootstrap v3\n")
            self.assertEqual(self.actions(apply=True), {"bootstrap.md": "conflict"})
            self.assertEqual((self.root / "bootstrap.md").read_text(), "My bootstrap\n")
            self.assertEqual(self.actions(), {"bootstrap.md": "conflict"})
            self.assertEqual(self.actions(take=["bootstrap.md"], apply=True), {"bootstrap.md": "update"})
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v3\n")
            self.assertEqual(self.plan()["changes"], [])
        with self.subTest('local deletions stay deleted, and template removals are only reported'):
            (self.root / "skills/coordination/SKILL.md").unlink()
            (self.template / "harnesses/codex/harness.json").unlink()
            self.assertEqual(self.actions(apply=True), {"skills/coordination/SKILL.md": "deleted",
                                                        "harnesses/codex/harness.json": "removed"})
            self.assertFalse((self.root / "skills/coordination/SKILL.md").exists())
            self.assertTrue((self.root / "harnesses/codex/harness.json").exists())
            # Back as they were installed, so that only the next case's changes differ.
            self.put("skills/coordination/SKILL.md", "Coordinate v1\n", self.root)
            self.put("harnesses/codex/harness.json", '{"schema": 2, "adapter": "codex", "settings": {}}\n')
        with self.subTest('without a manifest, differences are unknown until taken'):
            (self.root / templates.MANIFEST).unlink()
            self.put("harnesses/codex/harness.json",
                     '{"schema": 2, "adapter": "codex", "settings": {"inbox_bridge": true}}\n', self.root)
            self.put("bootstrap.md", "Bootstrap v4\n")
            self.assertEqual(self.actions(apply=True),
                             {"bootstrap.md": "unknown", "harnesses/codex/harness.json": "unknown"})
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v3\n")
            # Kept files now count as local edits of the current template; a harness file, which could otherwise
            # merge from a base it never had, stays a whole-file conflict.
            self.assertEqual(self.actions(), {"bootstrap.md": "local", "harnesses/codex/harness.json": "conflict"})
            self.put("harnesses/codex/harness.json", '{"schema": 2, "adapter": "codex", "settings": {"model": "x"}}\n')
            self.assertEqual(self.actions()["harnesses/codex/harness.json"], "conflict")
            self.assertEqual(self.actions(take=["bootstrap.md"], apply=True)["bootstrap.md"], "update")
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v4\n")
        with self.subTest('take refuses paths outside the template'), self.assertRaises(ValueError):
            self.plan(take=["universe.json"])
        with self.subTest("recursive apply uses each nested universe's recorded source"):
            child = self.root / 'subverses/child'
            self.put('subverses/child/bootstrap.md', 'Child v2\n')
            other = self.work / 'other-template'
            write(other / 'universe.json', {'schema': 1, 'name': 'other', 'workspace': '..'})
            self.put('bootstrap.md', 'Other v1\n', other)
            independent = templates.create(self.root / 'subverses/independent', 'independent', str(other), self.work)
            self.put('bootstrap.md', 'Other v2\n', other)
            self.put('bootstrap.md', 'Bootstrap v5\n')
            result = refresh.run(self.root, recursive=True, apply=True)
            plans = {plan['universe']: plan for plan in result['universes']}
            for scope, source, text in ((self.root, self.template, 'Bootstrap v5\n'),
                                        (child, self.template / 'subverses/child', 'Child v2\n'),
                                        (independent, other, 'Other v2\n')):
                self.assertEqual(plans[str(scope)]['source'], str(source))
                self.assertEqual((scope / 'bootstrap.md').read_text(), text)
        with self.subTest('a template that is gone names the universe, the template and how to compare'):
            shutil.rmtree(self.template)
            with self.assertRaises(ValueError) as caught:
                self.plan()
            self.assertIn(str(self.root), str(caught.exception))
            self.assertIn(str(self.template), str(caught.exception))
            self.assertIn(f"mv2 -u {self.root} refresh --template", str(caught.exception))
            with self.assertRaisesRegex(ValueError, "elsewhere"):
                self.plan(template="elsewhere")

    def test_harness_settings_merge_key_by_key(self):
        """A harness.json edited in the universe takes the template's new keys from refresh, from the saved base."""
        path, policy = "harnesses/claude/harness.json", "harnesses/policy/harness.json"

        def template(target=path, description="v1", **settings):
            write(self.template / target, {"schema": 2, "adapter": "claude", "description": description,
                                           "settings": settings})

        def install(name, bases=True):
            root = templates.create(self.work / name / ".multiverse", name, str(self.template), self.work / name)
            if not bases:  # as mv2 before refresh merged wrote the manifest
                manifest = read(root / templates.MANIFEST)
                manifest["bases"].clear()
                write(root / templates.MANIFEST, manifest)
            return root

        def change(root, target=path, **options):
            plan = refresh.run(root, **options)["universes"][0]
            return next((c for c in plan["changes"] if c["path"] == target), {"action": "current"}), plan

        def edit(root, update, target=path):
            data = read(root / target)
            update(data["settings"])
            write(root / target, data)

        v1 = {"args": ["--a"], "model": "m1", "env": {"OLD": "1"}}
        template(**v1)
        root = install("merge")
        with self.subTest("the template's changes merge into the universe's own settings"):
            edit(root, lambda s: (s.update(inbox_bridge=True, args=["--a", "--effort", "x"]), s.pop("model")))
            template(description="v2", args=["--a"], model="m1", env={}, model_names={"m1": "M"})
            before = (root / path).read_text()
            found, plan = change(root, diff=True)
            self.assertEqual(found["action"], "merge")
            for words in ("adds settings.model_names", "changes description, settings.env"):
                self.assertIn(words, found["reason"])
            self.assertIn('+    "model_names": {', found["diff"])
            self.assertFalse([key for key in found if key.startswith("_")])
            self.assertEqual((root / path).read_text(), before)
            found, plan = change(root, apply=True)
            self.assertEqual(read(root / path), {"schema": 2, "adapter": "claude", "description": "v2", "settings": {
                "args": ["--a", "--effort", "x"], "env": {}, "inbox_bridge": True, "model_names": {"m1": "M"}}})
            self.assertEqual((Path(plan["applied"]["trash"]) / path).read_text(), before)
            self.assertEqual(change(root)[0]["action"], "local")
        with self.subTest("a key both changed keeps the universe's value, and stays a conflict until taken"):
            template(description="v2", args=["--b"], model="m1", env={"EXTRA": "1"}, model_names={"m1": "M"})
            found, _ = change(root, apply=True)
            self.assertEqual(found["action"], "merge")
            self.assertIn("changes settings.env", found["reason"])
            self.assertIn("conflict on settings.args, yours kept", found["reason"])
            self.assertEqual(read(root / path)["settings"]["args"], ["--a", "--effort", "x"])
            self.assertEqual(read(root / path)["settings"]["env"], {"EXTRA": "1"})
            found, plan = change(root, apply=True)
            self.assertEqual((found["action"], plan["applied"]["written"]), ("conflict", []))
            self.assertIn("conflict on settings.args, yours kept", found["reason"])
            change(root, take=[path], apply=True)
            self.assertEqual(read(root / path), read(self.template / path))
        for case, later in (("deleting a merged key", lambda s: s["env"].pop("NEW")),
                            ("resetting a merged key", lambda s: s.update(model="m1"))):
            with self.subTest(f"after a merge with a conflict, {case} stays the universe's choice"):
                template(**v1)
                partial = install(f"partial-{len(case)}")
                edit(partial, lambda s: s.update(args=["--mine"]))
                template(args=["--theirs"], model="m2", env={"OLD": "1", "NEW": "1"})
                self.assertEqual(change(partial, apply=True)[0]["action"], "merge")
                edit(partial, later)
                found, _ = change(partial, apply=True)
                self.assertEqual(found["action"], "conflict")
                self.assertIn("conflict on settings.args, yours kept", found["reason"])
                settings = read(partial / path)["settings"]
                self.assertEqual((settings["args"], settings["model"], "NEW" in settings["env"]),
                                 (["--mine"], "m1" if "resetting" in case else "m2", "resetting" in case))
        for case, damage in (("no saved base", None),
                             ("a saved base that isn't the recorded version",
                              lambda bases: bases.update({path: '{"settings": {"args": ["--mine"]}}'}))):
            with self.subTest(f"{case}, from a text mv2 never shipped: nothing merges, a whole-file conflict"):
                template(**v1)
                legacy = install(f"legacy-{len(case)}", bases=damage is not None)
                if damage:
                    manifest = read(legacy / templates.MANIFEST)
                    damage(manifest["bases"])
                    write(legacy / templates.MANIFEST, manifest)
                edit(legacy, lambda s: s.pop("args"))
                before = (legacy / path).read_text()
                template(args=["--a"], model="m1", env={"OLD": "1", "EXTRA": "2"}, model_names={"m2": "N"})
                found, _ = change(legacy, apply=True)
                self.assertEqual(found["action"], "conflict")
                self.assertEqual((legacy / path).read_text(), before)

        def shipped(settings):
            return next(text for text in shipped_harnesses.TEXTS.values()
                        if json.loads(text).get("settings") == settings and json.loads(text)["description"].startswith(
                            "Codex" if "--dangerously-bypass-approvals-and-sandbox" in str(settings) else "Claude"))

        bundled = templates.TEMPLATES / "Silicon23" / "harnesses"
        for name, flag, mine in (("claude", "--dangerously-skip-permissions", "sonnet"),
                                 ("codex", "--dangerously-bypass-approvals-and-sandbox", "gpt-6.1-terra")):
            with self.subTest(f"no saved base, from main's Silicon23 {name}: a deleted {flag} stays deleted"):
                target, kept = f"harnesses/{name}/harness.json", (self.template / path).read_text()
                try:
                    (self.template / target).write_text(shipped({"args": [flag]}))
                    upgraded = install(f"upgraded-{name}", bases=False)
                    edit(upgraded, lambda s: (s.pop("args"), s.update(model=mine)), target)
                    (self.template / target).write_text((bundled / name / "harness.json").read_text())
                    found, _ = change(upgraded, target, apply=True)
                    self.assertEqual(found["action"], "merge")
                    settings = read(upgraded / target)["settings"]
                    self.assertNotIn("args", settings)
                    self.assertEqual(settings["model"], mine)
                    self.assertIn("conflict on settings.model, yours kept", found["reason"])
                    expected = read(bundled / name / "harness.json")["settings"]
                    self.assertEqual((settings["models"], settings["model_names"]),
                                     (expected["models"], expected["model_names"]))
                    edit(upgraded, lambda s: s.pop("model_names"), target)
                    change(upgraded, target, apply=True)
                    self.assertNotIn("model_names", read(upgraded / target)["settings"])
                finally:
                    if name != "claude":
                        (self.template / target).unlink()
                    (self.template / path).write_text(kept)
        with self.subTest("every shipped text is filed under its own fingerprint, or it would never be found"):
            self.assertEqual([key for key, text in shipped_harnesses.TEXTS.items()
                              if templates._text_fingerprint(text) != key], [])
        with self.subTest("no saved base, from a shipped text, no new key: the first apply still saves the base"):
            (self.template / path).write_text(shipped({}))
            plain = install("plain", bases=False)
            edit(plain, lambda s: s.update(model="claude-sonnet-5-5", inbox_bridge=True))
            data = json.loads(shipped({}))
            data["settings"]["model"] = "claude-sonnet-5-5"
            write(self.template / path, data)
            self.assertEqual(change(plain, apply=True)[0]["action"], "local")
            edit(plain, lambda s: s.pop("model"))
            self.assertEqual(change(plain, apply=True)[0]["action"], "local")
            self.assertNotIn("model", read(plain / path)["settings"])
        with self.subTest("a template changed between plan and apply stays pending"):
            template(**v1)
            racing = install("racing")
            edit(racing, lambda s: s.update(inbox_bridge=True))
            template(args=["--a"], model="m1", env={"OLD": "1", "FIRST": "1"})
            plan = templates.refresh_plan(racing)
            template(args=["--a"], model="m1", env={"OLD": "1", "FIRST": "1", "SECOND": "2"})
            templates.refresh_apply(racing, plan)
            self.assertNotIn("SECOND", read(racing / path)["settings"]["env"])
            found, _ = change(racing)
            self.assertEqual(found["action"], "merge")
            self.assertIn("changes settings.env", found["reason"])
            self.assertEqual(read(racing / path)["settings"]["env"]["FIRST"], "1")
        with self.subTest("a harness file the template drops between plan and apply is reported as removed"):
            template(**v1)
            dropped = install("dropped")
            edit(dropped, lambda s: s.update(inbox_bridge=True))
            template(args=["--a"], model="m1", env={"OLD": "1", "THIRD": "3"})
            plan = templates.refresh_plan(dropped)
            kept = (self.template / path).read_text()
            (self.template / path).unlink()
            templates.refresh_apply(dropped, plan)
            self.assertIn(path, read(dropped / templates.MANIFEST)["files"])
            self.assertEqual(change(dropped)[0]["action"], "removed")
            (self.template / path).write_text(kept)
        with self.subTest("a harness file edited while refresh runs is left as saved, and reported"):
            template(**v1)
            busy = install("busy")
            edit(busy, lambda s: s.update(args=["--mine"]))
            template(args=["--a"], model="m1", env={"OLD": "1", "NEW": "1"})
            plan = templates.refresh_plan(busy)
            edit(busy, lambda s: s.update(args=["--newer"]))
            applied = templates.refresh_apply(busy, plan)
            self.assertEqual((applied["written"], applied["skipped"]), ([], [path]))
            self.assertEqual(read(busy / path)["settings"]["args"], ["--newer"])
            self.assertEqual(change(busy)[0]["action"], "merge")
        with self.subTest("a file edited while an earlier one is written is left as saved, and not counted"):
            first, second = "harnesses/aa/harness.json", "harnesses/zz/harness.json"
            for target in (first, second):
                template(target, **v1)
            pair = install("pair")
            for target in (first, second):
                edit(pair, lambda s: s.update(args=["--mine"]), target)
                template(target, args=["--a"], model="m1", env={"OLD": "1", "NEW": "1"})
            real_replace, edited = templates.os.replace, []

            def replace(source, target):  # the person edits zz just after aa is written
                real_replace(source, target)
                if Path(target) == pair / first and not edited:
                    edited.append(True)
                    edit(pair, lambda s: s.update(args=["--newer"]), second)
            try:
                with mock.patch.object(templates.os, "replace", side_effect=replace):
                    result = refresh.run(pair, apply=True)
                self.assertEqual(edited, [True])
                applied = result["universes"][0]["applied"]
                self.assertEqual((applied["written"], applied["skipped"]), ([first], [second]))
                self.assertEqual(read(pair / second)["settings"]["args"], ["--newer"])
                trash = Path(applied["trash"])  # the written file's old copy only, and only it on record
                self.assertEqual(((trash / first).exists(), (trash / second).exists(),
                                  read(trash / ".removed.json")["files"]), (True, False, [first]))
                self.assertEqual(change(pair, second)[0]["action"], "merge")
            finally:
                for target in (first, second):
                    (self.template / target).unlink()
        with self.subTest("a file edited while its backup is made is left as saved, with no backup kept"):
            template(**v1)
            copying = install("copying")
            edit(copying, lambda s: s.update(args=["--mine"]))
            template(args=["--a"], model="m1", env={"OLD": "1", "NEW": "1"})
            real_copy = templates.shutil.copy2

            def copy2(source, target, **options):  # the person saves while refresh copies the file to the trash
                real_copy(source, target, **options)
                if Path(source) == copying / path:
                    edit(copying, lambda s: s.update(args=["--newer"]))
            with mock.patch.object(templates.shutil, "copy2", side_effect=copy2):
                applied = refresh.run(copying, apply=True)["universes"][0]["applied"]
            self.assertEqual((applied["written"], applied["skipped"], applied["trash"]), ([], [path], None))
            self.assertEqual(read(copying / path)["settings"]["args"], ["--newer"])
            self.assertFalse((copying / "trash").exists() and any((copying / "trash").rglob("harness.json")))
        with self.subTest("a skipped file whose template file is then removed stays on record, and reads removed"):
            template(**v1)
            gone = install("gone")
            edit(gone, lambda s: s.update(args=["--mine"]))
            template(args=["--a"], model="m1", env={"OLD": "1", "NEW": "1"})
            plan = templates.refresh_plan(gone)
            edit(gone, lambda s: s.update(args=["--newer"]))
            kept = (self.template / path).read_text()
            (self.template / path).unlink()
            try:
                self.assertEqual(templates.refresh_apply(gone, plan)["skipped"], [path])
                self.assertEqual(change(gone)[0]["action"], "removed")
            finally:
                (self.template / path).write_text(kept)
        with self.subTest("with no install record, an edited harness file stays unknown until taken"):
            template(**v1)
            bare = install("bare")
            (bare / templates.MANIFEST).unlink()
            edit(bare, lambda s: s.pop("args"))
            before = (bare / path).read_text()
            template(args=["--a"], model="m1", env={"OLD": "1"}, model_names={"m2": "N"})
            result = refresh.run(bare, apply=True)
            self.assertEqual(next(c for c in result["universes"][0]["changes"] if c["path"] == path)["action"],
                             "unknown")
            self.assertEqual((bare / path).read_text(), before)
            self.assertIn("change only with --take PATH.", refresh.format_output(result))
        with self.subTest("a harness file left unknown gets no record, so a later template change merges nothing"):
            self.assertNotIn(path, read(bare / templates.MANIFEST)["files"])
            for round, model in enumerate(("m2", "m3")):  # each apply, and each template change, keeps it so
                template(args=["--a"], model=model, env={"OLD": "1"}, model_names={model: "N"})
                found, _ = change(bare, apply=True)
                self.assertEqual(found["action"], "conflict", round)
                self.assertEqual((bare / path).read_text(), before)
                self.assertNotIn(path, read(bare / templates.MANIFEST)["files"])
            found, _ = change(bare, take=[path], apply=True)  # until the person takes the template's file
            self.assertEqual(found["action"], "update")
            self.assertEqual(read(bare / path)["settings"]["model"], "m3")
            self.assertEqual(change(bare)[0]["action"], "current")
        for damaged in ([], {}):
            with self.subTest(f"a recorded fingerprint damaged to {damaged!r} matches nothing, and refresh goes on"):
                template(**v1)
                broken = install(f"broken-{type(damaged).__name__}")
                manifest = read(broken / templates.MANIFEST)
                manifest["files"][path] = damaged
                write(broken / templates.MANIFEST, manifest)
                edit(broken, lambda s: s.pop("args"))
                before = (broken / path).read_text()
                template(args=["--a"], model="m1", env={"OLD": "1", "EXTRA": "2"})
                self.assertEqual(change(broken, apply=True)[0]["action"], "conflict")
                self.assertEqual((broken / path).read_text(), before)
        flag = "--dangerously-bypass-approvals-and-sandbox"
        for damaged in ([], {}, None, ""):
            with self.subTest(f"a recorded fingerprint damaged to {damaged!r} never becomes a base, over two applies"):
                target = "harnesses/codex/harness.json"
                write(self.template / target, {"schema": 2, "adapter": "codex", "settings": {"args": [flag]}})
                try:
                    kept = install(f"kept-{damaged!r}")
                    edit(kept, lambda s: (s.pop("args"), s.update(inbox_bridge=True)), target)
                    manifest = read(kept / templates.MANIFEST)
                    manifest["files"][target] = damaged
                    write(kept / templates.MANIFEST, manifest)
                    for description, settings in (("v2", {}), ("v3", {"args": [flag]})):
                        write(self.template / target, {"schema": 2, "adapter": "codex", "description": description,
                                                       "settings": settings})
                        self.assertEqual(change(kept, target, apply=True)[0]["action"], "conflict")
                        self.assertEqual(read(kept / target)["settings"], {"inbox_bridge": True})
                        self.assertNotIn(target, read(kept / templates.MANIFEST)["files"])
                finally:
                    (self.template / target).unlink()
        with self.subTest("a harness file that isn't a JSON object stays a whole-file conflict"):
            (root / path).write_text("not json\n")
            template(args=["--a"], model="m1", env={"OLD": "3"})
            self.assertEqual(change(root, apply=True)[0]["action"], "conflict")
            self.assertEqual((root / path).read_text(), "not json\n")
        canonical = {key: {} for key in ("claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5",
                                         "claude-haiku-5-5")}
        with self.subTest("a model subset merges as one value, so an alias subset is never doubled"):
            template(policy)
            aliased = install("aliased")
            edit(aliased, lambda s: s.update(model="haiku", models={"haiku": {}}), policy)
            template(policy, model="claude-opus-5-5", models=canonical, model_names={"claude-opus-5-5": "Opus"})
            found, _ = change(aliased, policy, apply=True)
            self.assertEqual(found["action"], "merge")
            self.assertIn("adds settings.model_names", found["reason"])
            self.assertEqual(read(aliased / policy)["settings"]["models"], {"haiku": {}})
        with self.subTest("a merge whose model settings would refuse launches is kept as a conflict"):
            template(policy)
            outside = install("outside")
            edit(outside, lambda s: s.update(model="claude-opus-4-7"), policy)
            template(policy, model="claude-opus-5-5", models=canonical)
            before = (outside / policy).read_text()
            found, _ = change(outside, policy, apply=True)
            self.assertEqual(found["action"], "conflict")
            self.assertIn("can't use", found["reason"])
            self.assertIn("claude-opus-4-7", found["reason"])
            self.assertEqual((outside / policy).read_text(), before)

if __name__ == "__main__":
    unittest.main()
