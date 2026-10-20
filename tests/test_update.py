"""Self-updates against local bare remotes only; never the running source checkout."""
import contextlib
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from multiverse import cli
from multiverse.tools import _common, update

# Keep Git identity and global/system config independent of the user's setup.
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
           "GIT_ALLOW_PROTOCOL": "file"}


class UpdateTest(unittest.TestCase):
    """mv2's checkout as a clone of a local bare remote, with a fake uv; no tests of its own.

    The repositories are built once per class and copied back before each test, at the same paths: the clones
    know their remotes by absolute path. A table restores them again before each row."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name).resolve() / "work"
        cls.snapshot = cls.root.with_name("snapshot")
        cls.remote, cls.author, cls.checkout = cls.root / "remote.git", cls.root / "author", cls.root / "mv2"
        cls.log, cls.uv = cls.root / "uv-called.json", cls.root / "fake-uv"
        cls.root.mkdir()
        templates = cls.root.with_name("templates")  # empty: no sample hooks to copy before every test
        templates.mkdir()
        with patch.dict(os.environ, {**GIT_ENV, "GIT_TEMPLATE_DIR": str(templates)}):
            cls.build()
        cls.root.rename(cls.snapshot)

    @classmethod
    def build(cls):
        cls.git(cls.root, "init", "--bare", "--initial-branch=main", str(cls.remote))
        cls.git(cls.root, "clone", str(cls.remote), str(cls.author))
        (cls.author / "src/multiverse/tools").mkdir(parents=True)
        (cls.author / "src/multiverse/tools/update.py").write_text("# source fixture\n")
        (cls.author / "uv.lock").write_text("fixture\n")
        (cls.author / "version.txt").write_text("one\n")
        cls.git(cls.author, "add", "src/multiverse/tools/update.py", "uv.lock", "version.txt")
        cls.git(cls.author, "commit", "-m", "initial")
        cls.git(cls.author, "push", "origin", "main")
        cls.git(cls.root, "clone", str(cls.remote), str(cls.checkout))
        cls.uv.write_text("#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
                          f"pathlib.Path({str(cls.log)!r}).write_text(json.dumps({{"
                          "'args':sys.argv[1:], 'cwd':os.getcwd(), "
                          "'environment':os.environ.get('UV_PROJECT_ENVIRONMENT'), "
                          "'project':os.environ.get('UV_PROJECT')}))\n")
        cls.uv.chmod(0o755)

    def setUp(self):
        git_env = patch.dict(os.environ, GIT_ENV)
        git_env.start()
        self.addCleanup(git_env.stop)
        # Converting plugin settings runs mv2 on the caller's universe, which here (MV2_UNIVERSE, or this
        # repository's own .multiverse) is a real one: never in these tests. test_update_settings tests it.
        converter = patch.object(update, "_convert_settings", return_value={"state": "none"})
        self.convert_settings = converter.start()
        self.addCleanup(converter.stop)
        self.restore()
        source = patch.object(update, "__file__", str(self.checkout / "src/multiverse/tools/update.py"))
        source.start()
        self.addCleanup(source.stop)
        real_which = shutil.which
        which = patch.object(update.shutil, "which", side_effect=lambda name: str(self.uv) if name == "uv" else real_which(name))
        which.start()
        self.addCleanup(which.stop)

    def restore(self):
        """Put every repository back as setUpClass built it."""
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.copytree(self.snapshot, self.root, symlinks=True)
        self.before = self.git(self.checkout, "rev-parse", "HEAD")

    @staticmethod
    def git(directory, *args):
        env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE"):
            env.pop(key, None)
        process = subprocess.run(["git", "-C", str(directory), *args], env=env, text=True, capture_output=True)
        if process.returncode:
            raise AssertionError(f"git {' '.join(args)} exited {process.returncode}: {process.stderr}")
        return process.stdout.strip()

    def advance(self):
        (self.author / "version.txt").write_text("two\n")
        self.git(self.author, "add", "version.txt")
        self.git(self.author, "commit", "-m", "next version")
        self.git(self.author, "push", "origin", "main")
        return self.git(self.author, "rev-parse", "HEAD")

    def local_commit(self):
        (self.checkout / "local.txt").write_text("local commit\n")
        self.git(self.checkout, "add", "local.txt")
        self.git(self.checkout, "commit", "-m", "local")
        self.before = self.git(self.checkout, "rev-parse", "HEAD")

    def collide(self):
        """An upstream commit adds new.txt, which the checkout already has, untracked."""
        (self.author / "new.txt").write_text("upstream\n")
        self.git(self.author, "add", "new.txt")
        self.git(self.author, "commit", "-m", "add file")
        self.git(self.author, "push", "origin", "main")
        (self.checkout / "new.txt").write_text("local\n")

    def assert_unchanged(self, result, reason):
        self.assertFalse(result["ok"])
        self.assertIn(reason, result["reason"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
        self.assertFalse(self.log.exists())
        self.assertEqual(result["notes"], [])


class PluginUpdateTest(UpdateTest):
    """UpdateTest with mv2 pinning one plugin, plugins/compaction, a submodule from its own local remote."""

    @classmethod
    def build(cls):
        super().build()
        cls.plugin_remote, cls.plugin_author = cls.root / "compaction.git", cls.root / "compaction-author"
        cls.submodule = cls.checkout / "plugins/compaction"
        cls.git(cls.root, "init", "--bare", "--initial-branch=main", str(cls.plugin_remote))
        cls.git(cls.root, "clone", str(cls.plugin_remote), str(cls.plugin_author))
        (cls.plugin_author / "plugin.json").write_text('{"name": "compaction"}\n')
        cls.git(cls.plugin_author, "add", "plugin.json")
        cls.git(cls.plugin_author, "commit", "-m", "plugin one")
        cls.git(cls.plugin_author, "push", "origin", "main")
        cls.git(cls.author, "-c", "protocol.file.allow=always", "submodule", "add", str(cls.plugin_remote),
                "plugins/compaction")
        cls.git(cls.author, "commit", "-m", "pin compaction")
        cls.git(cls.author, "push", "origin", "main")
        cls.git(cls.checkout, "pull", "--ff-only")
        cls.git(cls.checkout, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "plugins/compaction")
        cls.old_pin = cls.git(cls.submodule, "rev-parse", "HEAD")

    def setUp(self):
        super().setUp()
        os.environ["GIT_CONFIG_PARAMETERS"] = "'protocol.file.allow=always'"

    def plugin_commit(self, name="hooks.ts", text="new\n"):
        (self.plugin_author / name).write_text(text)
        self.git(self.plugin_author, "add", name)
        self.git(self.plugin_author, "commit", "-m", f"add {name}")
        self.git(self.plugin_author, "push", "origin", "main")
        return self.git(self.plugin_author, "rev-parse", "HEAD")

    def bump(self, path="plugins/compaction"):
        """Move mv2's pin of a plugin to its newest commit, upstream."""
        self.git(self.author / path, "pull", "--ff-only", "origin", "main")
        self.git(self.author, "add", path)
        self.git(self.author, "commit", "-m", f"bump {path}")
        self.git(self.author, "push", "origin", "main")
        return self.git(self.author / path, "rev-parse", "HEAD")

    def entry(self, result, path="plugins/compaction"):
        return next(item for item in result["plugins"] if item["path"] == path)


class SelfUpdateTest(UpdateTest):
    def test_fast_forward_follows_the_current_upstream_whatever_the_environment(self):
        """update fast-forwards its own checkout along the current branch's upstream whatever GIT_DIR, GIT_WORK_TREE,
        UV_PROJECT or -u say, keeping untracked files; fragile because agents run mv2 from worktrees and hooks where
        those are set, and a break updates another repository while reporting success."""
        for branch in ("main", "release"):
            with self.subTest(branch=branch):
                self.restore()
                if branch == "release":
                    self.git(self.checkout, "checkout", "-b", "release", "--track", "origin/main")
                after = self.advance()
                (self.checkout / "notes.txt").write_text("keep this\n")
                with patch.dict(os.environ, {"GIT_DIR": str(self.remote), "GIT_WORK_TREE": str(self.author),
                                             "UV_PROJECT": str(self.author),
                                             "UV_PROJECT_ENVIRONMENT": str(self.author / ".venv")}):
                    result = update.run(root=self.author)
                self.assertTrue(result["ok"], result)
                self.assertEqual((result["branch"], result["after"], result["commits"]), (branch, after, 1))
                self.assertEqual(self.git(self.checkout, "branch", "--show-current"), branch)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), after)
                self.assertEqual((self.checkout / "notes.txt").read_text(), "keep this\n")
                log = json.loads(self.log.read_text())
                self.assertEqual(log["args"], ["sync", "--locked"])
                self.assertEqual(log["cwd"], str(self.checkout))
                self.assertEqual(log["environment"], str(self.checkout / ".venv"))
                self.assertIsNone(log["project"])
                self.assertEqual(result["notes"], update.NOTES)

    def test_check_never_moves_anything(self):
        """--check fetches and counts but never merges or syncs, also when dirty, without upstream or diverged;
        fragile because check shares the real update's code path, and a break moves the checkout during a dry run."""
        for case in ("clean", "dirty", "dirty without upstream", "diverged"):
            with self.subTest(case):
                self.restore()
                fetched = self.before
                if case == "diverged":
                    self.local_commit()
                if case == "dirty without upstream":
                    self.git(self.checkout, "branch", "--unset-upstream")
                after = self.advance()
                if case.startswith("dirty"):
                    (self.checkout / "version.txt").write_text("local edit\n")
                with patch.object(update, "_git", wraps=update._git) as git:
                    result = update.run(check=True)
                if case == "clean":
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
                    self.assertFalse(self.log.exists())
                    self.assertEqual(result["notes"], [])
                else:
                    self.assert_unchanged(result, "has diverged" if case == "diverged"
                                          else "uncommitted changes to tracked files")
                if case == "dirty without upstream":
                    self.assertIn("has no upstream", result["reason"])
                    self.assertFalse(any(call.args[1] == "fetch" for call in git.call_args_list))
                    self.assertEqual(self.git(self.checkout, "rev-parse", "origin/main"), fetched)
                    self.assertNotIn("available", result)
                else:
                    self.assertEqual(self.git(self.checkout, "rev-parse", "origin/main"), after)
                    self.assertEqual((result["commits"], result["available"]), (1, after))
                if case.startswith("dirty"):
                    self.assertEqual((self.checkout / "version.txt").read_text(), "local edit\n")

    def test_every_unsafe_state_refuses_before_anything_moves(self):
        """Dirty, no upstream, fetch failure, ahead, diverged, an untracked collision, detached, a non-checkout and
        edits during the fetch all refuse with HEAD unchanged and uv never run; fragile because each state is a
        different git query and the during-fetch row is a check-then-act race, and a break loses local commits or
        edits."""
        real = update._git

        def dirty(staged):
            self.advance()
            (self.checkout / "version.txt").write_text("local edit\n")
            if staged:
                self.git(self.checkout, "add", "version.txt")

        def edited_during_fetch(checkout, *args, **kwargs):
            process = real(checkout, *args, **kwargs)
            if args[0] == "fetch":
                (self.checkout / "version.txt").write_text("changed during fetch\n")
            return process

        sources = (self.root / "site-packages/multiverse/tools/update.py",
                   self.root / "not-a-repo/src/multiverse/tools/update.py",
                   self.checkout / "nested/src/multiverse/tools/update.py")
        rows = (("unstaged edit", "uncommitted changes to tracked files", lambda: dirty(staged=False), None),
                ("staged edit", "uncommitted changes to tracked files", lambda: dirty(staged=True), None),
                ("no upstream", "has no upstream", lambda: self.git(self.checkout, "branch", "--unset-upstream"), None),
                ("fetch failure", "Could not fetch",
                 lambda: self.git(self.checkout, "remote", "set-url", "origin", str(self.root / "missing.git")), None),
                ("ahead", "is ahead of", self.local_commit, None),
                ("diverged", "has diverged", lambda: (self.local_commit(), self.advance()), None),
                ("untracked collision", "Could not fast-forward", self.collide, None),
                ("detached", "no current branch", lambda: self.git(self.checkout, "checkout", "--detach"), None),
                *((f"not a checkout: {source}", "not running from a Git source checkout", None,
                   patch.object(update, "__file__", str(source))) for source in sources),
                ("edited during the fetch", "uncommitted changes to tracked files", None,
                 patch.object(update, "_git", side_effect=edited_during_fetch)))
        for case, reason, prepare, meanwhile in rows:
            with self.subTest(case):
                self.restore()
                if prepare:
                    prepare()
                with meanwhile or contextlib.nullcontext():
                    self.assert_unchanged(update.run(), reason)
                if case.endswith("edit"):  # refused before the fetch
                    self.assertEqual(self.git(self.checkout, "rev-parse", "origin/main"), self.before)
                if case == "untracked collision":
                    self.assertEqual((self.checkout / "new.txt").read_text(), "local\n")

    def test_partial_update_outcomes_are_reported_as_partial(self):
        """A failed code step leaves the environment not run, and a failed sync keeps the new code with ok false and
        says to run uv sync --locked; fragile because ok and notes come from two independent steps, and a break
        reports success with a stale venv."""
        with self.subTest("the sync fails"):
            after = self.advance()
            self.uv.write_text("#!/bin/sh\necho 'lockfile problem' >&2\nexit 1\n")
            result = update.run()
            self.assertFalse(result["ok"])
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), after)
            self.assertEqual(result["after"], after)
            self.assertEqual(result["environment"]["state"], "failed")
            self.assertEqual(result["notes"], update.NOTES)
            self.assertIn("lockfile problem", result["reason"])
            self.assertIn("uv sync --locked", result["reason"])
        with self.subTest("the fast-forward fails"):
            self.restore()
            self.collide()
            result = update.run()
            self.assert_unchanged(result, "Could not fast-forward")
            self.assertEqual(result["environment"]["state"], "not_run")
            self.assertIn("did not complete", result["environment"]["reason"])
            self.assertEqual((self.checkout / "new.txt").read_text(), "local\n")

    def dispatch(self, *args, standalone=False):
        """(exit code, stdout) of update through one of mv2's real dispatchers, in this process."""
        argv = ["mv2", *([] if standalone else ["update"]), *args]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as stream:
            try:
                if standalone:
                    _common.main(update.configure, update.run)
                else:
                    cli.main()
            except SystemExit as stopped:
                return stopped.code, stream.getvalue()
        return 0, stream.getvalue()

    def child_cli(self):
        """`mv2 update` in a process of its own, with source discovery pointed at this temporary clone. The only
        possible uv command is our local stub, even in a successful update."""
        code = ("import sys\nfrom multiverse.tools import update\n"
                "update.__file__ = sys.argv.pop(1)\nuv = sys.argv.pop(1)\n"
                "which = update.shutil.which\n"
                "update.shutil.which = lambda name: uv if name == 'uv' else which(name)\n"
                "update._convert_settings = lambda checkout, root: {'state': 'none'}\n"  # never a real universe
                "from multiverse.cli import main\nmain()\n")
        env = {key: value for key, value in os.environ.items() if key != "MV2_UNIVERSE"}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return subprocess.run([sys.executable, "-c", code,
                               str(self.checkout / "src/multiverse/tools/update.py"), str(self.uv), "update"],
                              env=env, cwd=self.root, text=True, capture_output=True, timeout=30)

    def test_refusals_exit_1_and_success_exits_0(self):
        """Through mv2's real entry points a refusal exits 1 in text and JSON, and success exits 0 with the refresh
        note only when commits were pulled; a break goes unnoticed because the text still says refused while
        mv2 update && ... carries on."""
        for standalone in (False, True):
            with self.subTest("up to date", standalone=standalone):
                code, printed = self.dispatch(standalone=standalone)
                self.assertEqual(code, 0, printed)
                self.assertNotIn("refresh --apply", printed)
        self.git(self.checkout, "branch", "--unset-upstream")
        for standalone, check, json_mode in itertools.product((False, True), repeat=3):
            with self.subTest("refused", standalone=standalone, check=check, json=json_mode):
                code, printed = self.dispatch(*(["--check"] if check else []), *(["--json"] if json_mode else []),
                                              standalone=standalone)
                self.assertEqual(code, 1, printed)
                if json_mode:
                    self.assertFalse(json.loads(printed)["ok"])
                else:
                    self.assertIn("has no upstream", printed)
        with self.subTest("refused, in a process of its own"):
            child = self.child_cli()
            self.assertEqual(child.returncode, 1, child.stdout + child.stderr)
            self.assertIn("has no upstream", child.stdout)
        self.restore()
        after = self.advance()
        with self.subTest("check with commits waiting"):
            code, printed = self.dispatch("--check")
            self.assertEqual(code, 0, printed)
            self.assertNotIn("refresh --apply", printed)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
        with self.subTest("updated, in a process of its own"):
            child = self.child_cli()
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertIn("refresh --apply", child.stdout)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), after)


if __name__ == "__main__":
    unittest.main()
