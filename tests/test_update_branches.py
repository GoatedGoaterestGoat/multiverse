"""A user's plugin branch is never detached by mv2 update; all Git repositories are temporary."""
from pathlib import Path
import unittest
from unittest.mock import patch

from multiverse.tools import update
from test_update import PluginUpdateTest


class BranchUpdateTest(PluginUpdateTest):
    def test_a_plugin_branch_is_never_detached(self):
        """For each branch position mv2 update leaves the user's plugin branch attached at its commit and warns only
        when it is not at mv2's pin; fragile because submodule update detaches HEAD by default, and a detached
        branch's unpublished commits survive only in the reflog."""
        def at_incoming_pin():
            target = self.plugin_commit()
            self.bump()
            self.git(self.submodule, "fetch", "origin")
            return target

        def mv2_already_current():
            self.plugin_commit()
            self.bump()
            self.git(self.checkout, "fetch", "--no-recurse-submodules", "origin")
            self.git(self.checkout, "-c", "submodule.recurse=false", "merge", "--ff-only", "origin/main")
            return self.old_pin

        def behind_while_mv2_moves():
            self.plugin_commit()
            self.bump()
            return self.old_pin

        def published():
            commit = self.plugin_commit()
            self.git(self.submodule, "fetch", "origin")
            self.advance()
            return commit

        def unpublished():
            (self.submodule / "mine.txt").write_text("private work\n")
            self.git(self.submodule, "add", "mine.txt")
            self.git(self.submodule, "commit", "-m", "private work")
            self.advance()
            return self.git(self.submodule, "rev-parse", "HEAD")

        rows = (("at-pin", lambda: self.old_pin, "unchanged"),
                ("at-incoming-pin", at_incoming_pin, "unchanged"),
                ("pinned-old", mv2_already_current, "preserved"),
                ("my-plugin", behind_while_mv2_moves, "preserved"),
                ("published", published, "preserved"),
                ("private-work", unpublished, "preserved"))
        real = update._git

        def no_plugin_fetch(directory, *args, **kwargs):
            if Path(directory) == self.submodule:
                self.assertNotIn("fetch", args)
            return real(directory, *args, **kwargs)

        for branch, prepare, state in rows:
            with self.subTest(branch):
                self.restore()
                commit = prepare()
                self.git(self.submodule, "checkout", "-q", "-b", branch, commit)
                upstream = self.git(self.remote, "rev-parse", "main")
                pin = self.git(self.author, "rev-parse", "HEAD:plugins/compaction")
                for check in (True, False):
                    with patch.object(update, "_git", side_effect=no_plugin_fetch):
                        result = update.run(check=check)
                    self.assertTrue(result["ok"], result)
                    entry = self.entry(result)
                    self.assertEqual((entry["state"], entry["branch"], entry["before"], entry["after"]),
                                     (state, branch, commit, pin))
                    self.assertEqual("Warning: plugins/compaction is on branch" in update.format_output(result),
                                     state == "preserved")
                    self.assertEqual(self.git(self.submodule, "symbolic-ref", "--short", "HEAD"), branch)
                    self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), commit)
                    self.assertEqual(self.log.exists(), not check)  # check never syncs; the update does
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), upstream)
                self.assertEqual(result["environment"]["state"], "synced")


if __name__ == "__main__":
    unittest.main()
