"""A user's plugin branch is never detached by mv2 update; all Git repositories are temporary."""
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch

from multiverse.tools import update
from test_update_plugins import PluginUpdateTest


class BranchUpdateTest(PluginUpdateTest):
    def assert_branch(self, name, commit):
        self.assertEqual(self.git(self.submodule, "symbolic-ref", "--short", "HEAD"), name)
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), commit)

    def test_detached_ahead_refusal_has_a_working_repair_for_check_and_update(self):
        published = self.plugin_commit()
        self.git(self.submodule, "fetch", "origin")
        self.git(self.submodule, "switch", "--detach", published)
        expected = shlex.join(["git", "-C", str(self.submodule), "switch", "--detach", self.old_pin])
        for check in (True, False):
            result = update.run(check=check)
            self.assertFalse(result["ok"], result)
            self.assertIn(f"is detached at {published[:7]} and is ahead of mv2's pin {self.old_pin[:7]}",
                          result["reason"])
            self.assertIn(f"pin its current revision in mv2, or run {expected}, then run mv2 update again",
                          update.format_output(result))
            self.assertNotIn("local commits", result["reason"])
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), published)
        self.assertFalse(self.log.exists())
        command = result["reason"].split("or run ", 1)[1].split(", then run", 1)[0]
        subprocess.run(shlex.split(command), check=True, capture_output=True, text=True)
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
        self.assertTrue(update.run()["ok"])

    def test_older_branch_is_preserved_when_mv2_is_already_current(self):
        target = self.plugin_commit()
        self.bump()
        self.git(self.checkout, "fetch", "--no-recurse-submodules", "origin")
        self.git(self.checkout, "-c", "submodule.recurse=false", "merge", "--ff-only", "origin/main")
        self.git(self.submodule, "checkout", "-b", "pinned-old", self.old_pin)
        real = update._git

        def no_plugin_fetch(directory, *args, **kwargs):
            if Path(directory) == self.submodule:
                self.assertNotIn("fetch", args)
            return real(directory, *args, **kwargs)
        with patch.object(update, "_git", side_effect=no_plugin_fetch):
            for check in (True, False):
                result = update.run(check=check)
                self.assertTrue(result["ok"], result)
                entry = self.entry(result)
                self.assertEqual((entry["state"], entry["branch"], entry["before"], entry["after"]),
                                 ("preserved", "pinned-old", self.old_pin, target))
                self.assertIn("Warning: plugins/compaction is on branch pinned-old", update.format_output(result))
                self.assert_branch("pinned-old", self.old_pin)
                if check:
                    self.assertFalse(self.log.exists())
        self.assertTrue(self.log.exists())

    def test_branch_at_the_old_pin_stays_attached_while_mv2_and_uv_update(self):
        self.plugin_commit()
        self.bump()
        self.git(self.submodule, "checkout", "-b", "my-plugin", self.old_pin)
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result["after"], self.before)
        self.assertEqual(result["environment"]["state"], "synced")
        self.assertEqual(self.entry(result)["state"], "preserved")
        self.assert_branch("my-plugin", self.old_pin)

    def test_published_and_unpublished_commits_on_branches_are_preserved(self):
        published = self.plugin_commit()
        self.git(self.submodule, "fetch", "origin")
        self.git(self.submodule, "checkout", "-b", "published", published)
        self.advance()
        first = update.run()
        self.assertTrue(first["ok"], first)
        self.assertEqual(self.entry(first)["state"], "preserved")
        self.assert_branch("published", published)
        self.git(self.submodule, "checkout", "-b", "private-work", self.old_pin)
        (self.submodule / "mine.txt").write_text("private work\n")
        self.git(self.submodule, "add", "mine.txt")
        self.git(self.submodule, "commit", "-m", "private work")
        private = self.git(self.submodule, "rev-parse", "HEAD")
        second = update.run()
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.entry(second)["state"], "preserved")
        self.assert_branch("private-work", private)

    def test_dirty_branch_still_refuses_before_mv2_moves(self):
        self.git(self.submodule, "checkout", "-b", "my-plugin", self.old_pin)
        (self.submodule / "plugin.json").write_text("edited\n")
        self.advance()
        result = update.run()
        self.assertFalse(result["ok"], result)
        self.assertIn("has local changes", result["reason"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
        self.assertFalse(self.log.exists())
        self.assert_branch("my-plugin", self.old_pin)

    def test_branch_created_during_fetch_is_never_detached(self):
        self.plugin_commit()
        self.bump()
        real = update._git

        def changed(directory, *args, **kwargs):
            result = real(directory, *args, **kwargs)
            if Path(directory) == self.submodule and "fetch" in args:
                self.git(self.submodule, "checkout", "-b", "chosen-during-fetch", self.old_pin)
            return result
        with patch.object(update, "_git", side_effect=changed):
            result = update.run()
        self.assertFalse(result["ok"], result)
        self.assertIn("changed during the update", self.entry(result)["reason"])
        self.assert_branch("chosen-during-fetch", self.old_pin)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(BranchUpdateTest(name) for name in loader.getTestCaseNames(BranchUpdateTest)
                              if name in BranchUpdateTest.__dict__)
