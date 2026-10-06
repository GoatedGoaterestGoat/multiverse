"""Self-updates against local bare remotes only; never the running source checkout."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from multiverse import cli
from multiverse.tools import update
from multiverse.tools._common import output


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.remote = self.root / "remote.git"
        self.author = self.root / "author"
        self.checkout = self.root / "mv2"
        # Keep Git identity and global/system config independent of the user's setup.
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
               "GIT_ALLOW_PROTOCOL": "file"}
        git_env = patch.dict(os.environ, env)
        git_env.start()
        self.addCleanup(git_env.stop)
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.author))
        (self.author / "src/multiverse/tools").mkdir(parents=True)
        (self.author / "src/multiverse/tools/update.py").write_text("# source fixture\n")
        (self.author / "uv.lock").write_text("fixture\n")
        (self.author / "version.txt").write_text("one\n")
        self.git(self.author, "add", "src/multiverse/tools/update.py", "uv.lock", "version.txt")
        self.git(self.author, "commit", "-m", "initial")
        self.git(self.author, "push", "origin", "main")
        self.git(self.root, "clone", str(self.remote), str(self.checkout))
        self.before = self.git(self.checkout, "rev-parse", "HEAD")
        source = patch.object(update, "__file__", str(self.checkout / "src/multiverse/tools/update.py"))
        source.start()
        self.addCleanup(source.stop)
        self.log = self.root / "uv-called.json"
        self.uv = self.root / "fake-uv"
        self.uv.write_text("#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
                           f"pathlib.Path({str(self.log)!r}).write_text(json.dumps({{"
                           "'args':sys.argv[1:], 'cwd':os.getcwd(), "
                           "'environment':os.environ.get('UV_PROJECT_ENVIRONMENT'), "
                           "'project':os.environ.get('UV_PROJECT')}))\n")
        self.uv.chmod(0o755)
        real_which = shutil.which
        which = patch.object(update.shutil, "which", side_effect=lambda name: str(self.uv) if name == "uv" else real_which(name))
        which.start()
        self.addCleanup(which.stop)

    def git(self, directory, *args):
        env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE"):
            env.pop(key, None)
        process = subprocess.run(["git", "-C", str(directory), *args], env=env, text=True, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        return process.stdout.strip()

    def advance(self):
        (self.author / "version.txt").write_text("two\n")
        self.git(self.author, "add", "version.txt")
        self.git(self.author, "commit", "-m", "next version")
        self.git(self.author, "push", "origin", "main")
        return self.git(self.author, "rev-parse", "HEAD")

    def assert_unchanged(self, result, reason):
        self.assertFalse(result["ok"])
        self.assertIn(reason, result["reason"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
        self.assertFalse(self.log.exists())

    def test_up_to_date_still_syncs_environment(self):
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["before"], result["after"])
        self.assertEqual(result["commits"], 0)
        self.assertEqual(result["environment"]["state"], "synced")
        self.assertIn("already up to date", update.format_output(result))
        self.assertEqual(json.loads(self.log.read_text())["args"], ["sync", "--locked"])

    def test_fast_forward_ignores_untracked_and_universe_and_git_environment(self):
        after = self.advance()
        (self.checkout / "notes.txt").write_text("keep this\n")
        with patch.dict(os.environ, {"GIT_DIR": str(self.remote), "GIT_WORK_TREE": str(self.author),
                                     "UV_PROJECT": str(self.author), "UV_PROJECT_ENVIRONMENT": str(self.author / ".venv")}):
            result = update.run(root=self.author)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["after"], after)
        self.assertEqual(result["commits"], 1)
        self.assertEqual((self.checkout / "notes.txt").read_text(), "keep this\n")
        log = json.loads(self.log.read_text())
        self.assertEqual(log["cwd"], str(self.checkout))
        self.assertEqual(log["environment"], str(self.checkout / ".venv"))
        self.assertIsNone(log["project"])
        self.assertIn(f"Updated mv2 from {self.before[:7]} to {after[:7]} (1 commit); environment synced.", update.format_output(result))

    def test_check_fetches_but_does_not_merge_or_sync(self):
        after = self.advance()
        result = update.run(check=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["available"], after)
        self.assertEqual(self.git(self.checkout, "rev-parse", "origin/main"), after)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
        self.assertFalse(self.log.exists())
        self.assertIn("would update", update.format_output(result))
        self.assertIn("Check only", update.format_output(result))

    def test_tracked_dirty_files_refused_before_fetch(self):
        self.advance()
        for staged in (False, True):
            with self.subTest(staged=staged):
                (self.checkout / "version.txt").write_text("local edit\n")
                if staged:
                    self.git(self.checkout, "add", "version.txt")
                self.assert_unchanged(update.run(), "uncommitted changes to tracked files")
                self.assertEqual(self.git(self.checkout, "rev-parse", "origin/main"), self.before)

    def test_no_upstream_refused(self):
        self.git(self.checkout, "branch", "--unset-upstream")
        self.assert_unchanged(update.run(), "has no upstream")

    def test_fetch_failure_refused(self):
        self.git(self.checkout, "remote", "set-url", "origin", str(self.root / "missing.git"))
        self.assert_unchanged(update.run(), "Could not fetch")

    def test_tracked_changes_during_fetch_refused(self):
        original = update._git

        def fetch_then_edit(checkout, *args):
            process = original(checkout, *args)
            if args[0] == "fetch":
                (self.checkout / "version.txt").write_text("changed during fetch\n")
            return process

        with patch.object(update, "_git", side_effect=fetch_then_edit):
            self.assert_unchanged(update.run(), "uncommitted changes to tracked files")

    def local_commit(self):
        (self.checkout / "local.txt").write_text("local commit\n")
        self.git(self.checkout, "add", "local.txt")
        self.git(self.checkout, "commit", "-m", "local")
        self.before = self.git(self.checkout, "rev-parse", "HEAD")

    def test_ahead_refused(self):
        self.local_commit()
        self.assert_unchanged(update.run(), "is ahead of")

    def test_diverged_refused(self):
        self.local_commit()
        self.advance()
        self.assert_unchanged(update.run(), "has diverged")

    def test_missing_uv_reports_skipped(self):
        real_which = shutil.which("git")
        with patch.object(update.shutil, "which", side_effect=lambda name: real_which if name == "git" else None):
            result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["environment"], {"state": "skipped", "reason": "uv is not available"})
        self.assertIn("Environment sync skipped", update.format_output(result))
        self.assertFalse(self.log.exists())

    def test_new_revision_can_remove_lockfile(self):
        self.git(self.author, "rm", "uv.lock")
        self.git(self.author, "commit", "-m", "remove lock")
        self.git(self.author, "push", "origin", "main")
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["environment"], {"state": "skipped", "reason": "uv.lock is missing"})
        self.assertFalse(self.log.exists())

    def test_check_uses_upstream_lockfile_availability(self):
        self.git(self.author, "rm", "uv.lock")
        self.git(self.author, "commit", "-m", "remove lock")
        self.git(self.author, "push", "origin", "main")
        result = update.run(check=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["environment"]["reason"], "uv.lock is missing")
        self.assertTrue((self.checkout / "uv.lock").exists())
        self.assertFalse(self.log.exists())

    def test_untracked_collision_is_not_overwritten(self):
        (self.author / "new.txt").write_text("upstream\n")
        self.git(self.author, "add", "new.txt")
        self.git(self.author, "commit", "-m", "add file")
        self.git(self.author, "push", "origin", "main")
        (self.checkout / "new.txt").write_text("local\n")
        self.assert_unchanged(update.run(), "Could not fast-forward")
        self.assertEqual((self.checkout / "new.txt").read_text(), "local\n")

    def test_sync_failure_preserves_updated_code_and_explains_retry(self):
        after = self.advance()
        self.uv.write_text("#!/bin/sh\necho 'lockfile problem' >&2\nexit 1\n")
        result = update.run()
        self.assertFalse(result["ok"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), after)
        self.assertEqual(result["after"], after)
        self.assertEqual(result["environment"]["state"], "failed")
        self.assertIn("lockfile problem", result["reason"])
        self.assertIn("uv sync --locked", result["reason"])

    def test_installed_package_and_non_checkout_refused(self):
        for source in (self.root / "site-packages/multiverse/tools/update.py",
                       self.root / "not-a-repo/src/multiverse/tools/update.py",
                       self.checkout / "nested/src/multiverse/tools/update.py"):
            with self.subTest(source=source), patch.object(update, "__file__", str(source)):
                self.assert_unchanged(update.run(), "not running from a Git source checkout")

    def test_detached_head_refused(self):
        self.git(self.checkout, "checkout", "--detach")
        self.assert_unchanged(update.run(), "no current branch")

    def test_current_branch_upstream_not_hardcoded(self):
        self.git(self.checkout, "checkout", "-b", "release", "--track", "origin/main")
        after = self.advance()
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["branch"], "release")
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "release")
        self.assertEqual(result["after"], after)

    def test_json_and_readable_cli_contract_and_failure_exit(self):
        args = cli.parser().parse_args(["update", "--check", "--json"])
        result = cli.run(args)
        self.assertTrue(result["ok"], result)
        self.assertIsInstance(update.format_output(result), dict)
        self.assertNotIn("json", update.format_output(result))
        self.git(self.checkout, "branch", "--unset-upstream")
        for json_mode in (False, True):
            with self.subTest(json=json_mode), contextlib.redirect_stdout(io.StringIO()) as stream:
                with self.assertRaises(SystemExit) as exit_error:
                    output(lambda: update.run(json=json_mode), render=update.format_output)
                self.assertEqual(exit_error.exception.code, 1)
                if json_mode:
                    self.assertFalse(json.loads(stream.getvalue())["ok"])
                else:
                    self.assertIn("has no upstream", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
