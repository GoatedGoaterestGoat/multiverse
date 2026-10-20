"""mv2 update with plugin submodules: temporary bare remotes, file protocol and a fake uv only."""
import contextlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest
from unittest.mock import patch

from multiverse.tools import update
from test_update import PluginUpdateTest


class PluginsTest(PluginUpdateTest):
    def plugin_place(self):
        """The plugin checkout's commit, branch and changes."""
        return self.git(self.submodule, "status", "--porcelain=v2", "--branch")

    def test_an_offline_plugin_remote_never_blocks_mv2s_update(self):
        """Against real git, an offline plugin remote still lets mv2 update with a warning and the pin kept, --check
        does not fetch it, and when access returns the plugin updates and syncs once; fragile because git's
        submodule behaviour changes across versions, and an optional private remote must never block mv2's
        update."""
        real = update._git

        def no_plugin_fetch(directory, *args, **kwargs):
            if Path(directory) == self.submodule:
                self.assertNotIn("fetch", args)  # --check never contacts this remote
            return real(directory, *args, **kwargs)

        offline = self.root / "offline.git"
        deinit = f"git -C {shlex.quote(str(self.checkout))} submodule deinit -f -- plugins/compaction"
        with self.subTest("online"):
            new = self.plugin_commit()
            self.bump()
            with patch.object(update, "_git", side_effect=no_plugin_fetch):
                checked = update.run(check=True)
            self.assertTrue(checked["ok"], checked)
            self.assertEqual(self.entry(checked)["state"], "would_update")
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
            self.assertFalse(self.log.exists())
            result = update.run()
            self.assertTrue(result["ok"], result)
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), new)
            entry = self.entry(result)
            self.assertEqual((entry["state"], entry["before"], entry["after"]), ("updated", self.old_pin, new))
            self.assertIn("plugins/compaction", result["notes"][-1])  # running agents reload its code
            self.assertEqual(json.loads(self.log.read_text())["args"], ["sync", "--locked"])
        with self.subTest("offline, then back"):
            self.restore()
            new = self.plugin_commit()
            self.bump()
            self.plugin_remote.rename(offline)
            result = update.run()
            self.assertTrue(result["ok"], result)
            self.assertNotEqual(result["after"], self.before)
            self.assertEqual(self.entry(result)["state"], "unavailable")
            self.assertEqual(result["environment"]["state"], "synced")
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
            self.assertIn(f"Warning: plugins/compaction was left at {self.old_pin[:7]}", update.format_output(result))
            with patch.object(update, "_git", side_effect=no_plugin_fetch):
                checked = update.run(check=True)
            self.assertTrue(checked["ok"], checked)
            self.assertEqual(self.entry(checked)["state"], "would_update")
            # Further mv2 commits can update even while the optional plugin remains unavailable.
            self.advance()
            again = update.run()
            self.assertTrue(again["ok"], again)
            self.assertEqual(self.entry(again)["state"], "unavailable")
            offline.rename(self.plugin_remote)
            self.log.unlink()
            recovered = update.run()
            self.assertTrue(recovered["ok"], recovered)
            self.assertEqual(recovered["commits"], 0)  # retry even with no new mv2 commit
            self.assertEqual(self.entry(recovered)["state"], "updated")
            self.assertEqual(self.entry(recovered)["before"], self.old_pin)
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), new)
            self.assertTrue(self.log.exists())
        with self.subTest("offline, with the pin already fetched"):
            self.restore()
            new = self.plugin_commit()
            self.bump()
            self.git(self.submodule, "fetch", "origin")
            self.git(self.checkout, "pull", "--ff-only")  # a main update whose plugin was left behind
            self.plugin_remote.rename(offline)
            result = update.run()
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["commits"], 0)
            self.assertEqual(self.entry(result)["state"], "updated")
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), new)
        with self.subTest("offline, then uninstalled with the printed command"):
            self.restore()
            self.plugin_commit()
            self.bump()
            self.plugin_remote.rename(offline)
            result = update.run()
            self.assertTrue(result["ok"], result)
            self.assertIn(deinit, update.format_output(result))
            subprocess.run(shlex.split(deinit), check=True, capture_output=True, text=True)
            later = update.run()
            self.assertTrue(later["ok"], later)
            self.assertEqual(self.entry(later)["state"], "not_installed")
            self.assertFalse((self.submodule / "plugin.json").exists())

    def test_local_work_in_a_plugin_checkout_is_never_overwritten(self):
        """Each plugin checkout state (unstaged, staged, a dirty branch, detached ahead, local commits with the pin
        missing, a colliding untracked file, a staged pin, a corrupt index, a conflict) refuses or is preserved with
        HEADs unchanged, and the printed switch commands really repair it; fragile because it rests on git status,
        index and merge-base semantics, and a break silently destroys plugin work."""
        real = update._git

        def pin_moved():
            self.plugin_commit()
            self.bump()

        def edit(staged):
            pin_moved()
            (self.submodule / "plugin.json").write_text('{"name": "edited"}\n')
            if staged:
                self.git(self.submodule, "add", "plugin.json")

        def at_incoming_pin():
            pin_moved()
            self.git(self.submodule, "fetch", "origin")
            self.git(self.submodule, "checkout", "-q", "origin/main")

        def detached_ahead():
            published = self.plugin_commit()
            self.git(self.submodule, "fetch", "origin")
            self.git(self.submodule, "switch", "-q", "--detach", published)
            return shlex.join(["git", "-C", str(self.submodule), "switch", "--detach", self.old_pin])

        def dirty_branch():
            self.git(self.submodule, "checkout", "-q", "-b", "my-plugin", self.old_pin)
            (self.submodule / "plugin.json").write_text("edited\n")
            self.advance()

        def staged_pin():
            other = self.plugin_commit("staged.ts")
            self.git(self.checkout, "update-index", "--cacheinfo", f"160000,{other},plugins/compaction")

        def conflict():
            other = self.plugin_commit()
            entries = "".join(f"160000 {commit} {stage}\tplugins/compaction\n"
                              for commit, stage in ((self.old_pin, 1), (self.old_pin, 2), (other, 3)))
            subprocess.run(["git", "-C", str(self.checkout), "update-index", "--index-info"], input=entries,
                           text=True, check=True, capture_output=True)

        def unreadable(checkout, *args, **kwargs):
            if Path(checkout) == self.submodule and args[:1] == ("status",):
                return subprocess.CompletedProcess(args, 128, "", "fatal: index file corrupt")
            return real(checkout, *args, **kwargs)

        refusals = (  # (case, prepare, what the refusal says, a stand-in for git)
            ("unstaged", lambda: edit(staged=False), ("plugins/compaction has local changes",), None),
            ("staged", lambda: edit(staged=True), ("plugins/compaction has local changes",), None),
            ("detached at the incoming pin", at_incoming_pin, ("plugins/compaction is detached at", "is ahead of"),
             None),
            ("detached ahead of the pin", detached_ahead, ("plugins/compaction is detached at", "is ahead of"), None),
            ("dirty branch", dirty_branch, ("plugins/compaction has local changes",), None),
            ("staged pin", staged_pin, ("uncommitted changes to tracked files",), None),
            ("conflict", conflict, ("plugins/compaction has a merge conflict",), None),
            ("unreadable", pin_moved, ("plugins/compaction could not be checked", "index file corrupt"), unreadable))
        for case, prepare, reasons, git in refusals:
            with self.subTest(case):
                self.restore()
                repair = prepare()
                place = self.plugin_place()
                for check in (True, False):
                    with patch.object(update, "_git", side_effect=git) if git else contextlib.nullcontext():
                        result = update.run(check=check)
                    self.assertFalse(result["ok"], result)
                    for reason in reasons:
                        self.assertIn(reason, result["reason"])
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
                    self.assertEqual(self.plugin_place(), place)
                self.assertFalse(self.log.exists())
                if isinstance(repair, str):
                    self.assertIn(repair, update.format_output(result))
                    subprocess.run(shlex.split(repair), check=True, capture_output=True, text=True)
                    self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
                    self.assertTrue(update.run()["ok"])
        with self.subTest("untracked files"):
            self.restore()
            (self.submodule / "notes.txt").write_text("mine\n")
            self.plugin_commit("other.ts")
            self.bump()
            self.assertTrue(update.run()["ok"])
            self.assertEqual((self.submodule / "notes.txt").read_text(), "mine\n")
            before = self.git(self.checkout, "rev-parse", "HEAD")
            previous = self.git(self.submodule, "rev-parse", "HEAD")
            (self.submodule / "hooks.ts").write_text("untracked, mine\n")
            self.plugin_commit("hooks.ts", "theirs\n")
            self.bump()
            self.log.unlink()
            result = update.run()
            self.assertFalse(result["ok"])
            self.assertEqual(self.entry(result)["state"], "failed")
            self.assertEqual((self.submodule / "hooks.ts").read_text(), "untracked, mine\n")
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), previous)
            self.assertNotEqual(self.git(self.checkout, "rev-parse", "HEAD"), before)  # the code moved on
            self.assertEqual(result["environment"]["state"], "synced")  # and its environment was synced
            self.assertIn(f"git -C {self.checkout} submodule update --checkout -- plugins/compaction", result["reason"])
        with self.subTest("local commits and the pin not fetched"):
            self.restore()
            new = self.plugin_commit()
            self.bump()
            self.git(self.checkout, "fetch", "--no-recurse-submodules", "origin")
            self.git(self.checkout, "-c", "submodule.recurse=false", "merge", "--ff-only", "origin/main")
            self.assertFalse(update._has_commit(self.submodule, new))
            (self.submodule / "mine.txt").write_text("mine\n")
            self.git(self.submodule, "add", "mine.txt")
            self.git(self.submodule, "commit", "-m", "local work")
            local = self.git(self.submodule, "rev-parse", "HEAD")
            result = update.run()
            self.assertFalse(result["ok"], result)
            self.assertIn("has diverged from", result["reason"])
            repair = shlex.join(["git", "-C", str(self.submodule), "switch", "--detach", new])
            self.assertIn(repair, self.entry(result)["reason"])
            self.assertNotIn("Repair it with:", result["reason"])
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), local)
            self.assertEqual((self.submodule / "mine.txt").read_text(), "mine\n")
            # With the pin now fetched the next preflight refuses too, before syncing or moving anything.
            self.log.unlink()
            refused = update.run()
            self.assertFalse(refused["ok"])
            self.assertIn(repair, refused["reason"])
            self.assertFalse(self.log.exists())
            subprocess.run(shlex.split(repair), check=True, capture_output=True, text=True)
            self.assertTrue(update.run()["ok"])

    def test_changes_made_during_the_update_are_preserved(self):
        """A change made at each git step of an update (an edit, a new branch, a swapped repository, a failing read)
        leaves that plugin as it is and fails only it; fragile because each step opens a window, and edits made
        during an update are overwritten silently."""
        real = update._git

        def mv2_fetch(directory, args, merged):
            return args[:1] == ("fetch",) and Path(directory) == self.checkout

        def plugin_fetch(directory, args, merged):
            return Path(directory) == self.submodule and "fetch" in args

        def merge(directory, args, merged):
            return args[:1] == ("merge",)

        def plugin_read_after_merge(directory, args, merged):
            return merged and Path(directory) == self.submodule and args[:2] == ("rev-parse", "HEAD")

        written = []

        def edit(text):
            def write():
                (self.submodule / "plugin.json").write_text(text)
                written.append(text)
            return write

        def swap():
            shutil.move(str(self.submodule), str(self.root / "moved-away"))
            self.git(self.root, "clone", "-q", str(self.plugin_remote), str(self.submodule))
            self.git(self.submodule, "checkout", "-q", self.old_pin)

        def time_out():
            raise subprocess.TimeoutExpired(["git"], 120)

        rows = (  # (case, when, what happens then, what the failure says)
            ("an edit during mv2's fetch", mv2_fetch, edit("edited during mv2's fetch\n"),
             "plugins/compaction changed while mv2 fetched its upstream"),
            ("an edit during the plugin's fetch", plugin_fetch, edit("edited during the plugin's fetch\n"),
             "it changed during the update and was left as it is"),
            ("a branch made during the plugin's fetch", plugin_fetch,
             lambda: self.git(self.submodule, "checkout", "-q", "-b", "chosen-during-fetch", self.old_pin),
             "it changed during the update and was left as it is"),
            ("an edit after the merge", merge, edit("edited after the merge\n"),
             "it changed during the update and was left as it is"),
            ("a pin staged after the merge", merge, lambda: self.git(
                self.checkout, "update-index", "--cacheinfo", f"160000,{self.old_pin},plugins/compaction"),
             "pins for it changed during the update"),
            ("a repository swapped in after the merge", merge, swap,
             "it changed during the update and was left as it is"),
            ("a read failing after the merge", plugin_read_after_merge, time_out, "git did not finish within 120 s"))
        for case, when, then, says in rows:
            with self.subTest(case):
                self.restore()
                self.plugin_commit()
                self.bump()
                upstream = self.git(self.remote, "rev-parse", "main")
                merged = []
                written.clear()

                def meanwhile(directory, *args, **kwargs):
                    process = real(directory, *args, **kwargs)
                    if args[:1] == ("merge",):
                        merged.append(True)
                    if when(directory, args, merged):
                        then()
                    return process
                with patch.object(update, "_git", side_effect=meanwhile):
                    result = update.run()
                self.assertFalse(result["ok"], result)
                self.assertIn(says, result["reason"])
                self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
                if when is mv2_fetch:  # refused before anything moved
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
                    self.assertFalse(self.log.exists())
                else:  # only the plugin failed: mv2's code and environment were updated
                    self.assertEqual(self.entry(result)["state"], "failed")
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), upstream)
                    self.assertEqual(result["environment"]["state"], "synced")
                if written:
                    self.assertEqual((self.submodule / "plugin.json").read_text(), written[-1])
                if "branch" in case:
                    self.assertEqual(self.git(self.submodule, "symbolic-ref", "--short", "HEAD"), "chosen-during-fetch")

    def test_a_failed_sync_keeps_an_earlier_plugin_failure(self):
        """A uv sync that fails, times out or cannot start after a plugin has already failed keeps both reasons and
        the retry advice in the result and the readable output, with the environment failed; a break goes unnoticed
        because the uv exception path once replaced the plugin's failure, and only the JSON entry still said
        failed."""
        real = update.subprocess.run
        failures = (("exits 1", "sync broke"), ("times out", "600 s"), ("cannot start", "exec format error"))
        for failure, detail in failures:
            with self.subTest(failure):
                self.restore()
                (self.submodule / "hooks.ts").write_text("untracked\n")  # the plugin's move collides with it
                self.plugin_commit("hooks.ts", "theirs\n")
                self.bump()

                def uv(argv, *args, **kwargs):
                    if argv[:2] == [str(self.uv), "sync"]:
                        if failure == "times out":
                            raise subprocess.TimeoutExpired(argv, 600)
                        if failure == "cannot start":
                            raise OSError("exec format error")
                        return subprocess.CompletedProcess(argv, 1, "", "sync broke")
                    return real(argv, *args, **kwargs)
                with patch.object(update.subprocess, "run", side_effect=uv):
                    result = update.run()
                self.assertFalse(result["ok"])
                self.assertEqual(self.entry(result)["state"], "failed")
                self.assertEqual(result["environment"]["state"], "failed")
                for said in (result["reason"], update.format_output(result)):
                    self.assertIn(self.entry(result)["reason"].rstrip("."), said)  # the plugin's failure
                    self.assertIn("submodule update --checkout -- plugins/compaction", said)  # and its repair
                    self.assertIn(detail, said)  # the sync's failure
                    self.assertIn(f"uv sync --locked in {self.checkout}", said)  # and its retry

    def test_git_config_cannot_make_update_run_something_else(self):
        """submodule.*.update = !cmd never runs, nested recursion stays off, protocol.file.allow is given only to
        local remotes and a symlinked plugins/ is refused; fragile because git's defaults move (CVE-2022-39253), and
        the update still succeeds while a command runs."""
        real = update._git
        with self.subTest("an update command and nested recursion in the config"):
            marker = self.root / "custom-ran"
            self.git(self.checkout, "config", "submodule.plugins/compaction.update", f"!touch {marker}")
            self.git(self.submodule, "config", "fetch.recurseSubmodules", "true")
            new = self.plugin_commit()
            self.bump()
            result = update.run()
            self.assertTrue(result["ok"], result)
            self.assertFalse(marker.exists())
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), new)
        with self.subTest("a local remote without ambient file protocol permission"):
            self.restore()
            new = self.plugin_commit()
            self.bump()
            self.git(self.submodule, "remote", "set-url", "origin", self.plugin_remote.as_uri())
            fetches = []

            def restricted(directory, *args, **kwargs):
                if Path(directory) == self.submodule and "fetch" in args:
                    fetches.append(args)
                    # Git's submodule machinery sets this. No global or inherited transport allowance.
                    with patch.dict(os.environ, {"GIT_PROTOCOL_FROM_USER": "0"}):
                        return real(directory, *args, **kwargs)
                return real(directory, *args, **kwargs)
            with patch.dict(os.environ):
                os.environ.pop("GIT_CONFIG_PARAMETERS", None)
                os.environ.pop("GIT_ALLOW_PROTOCOL", None)
                with patch.object(update, "_git", side_effect=restricted):
                    result = update.run()
            self.assertTrue(result["ok"], result)
            self.assertTrue(fetches)
            self.assertTrue(all(args[:2] == ("-c", "protocol.file.allow=always") for args in fetches))
            self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), new)
            self.assertNotIn("protocol.file.allow", self.git(self.submodule, "config", "--local", "--list"))
        with self.subTest("a remote that is not local"):
            self.restore()
            self.plugin_commit()
            self.bump()
            fetches = []

            def unavailable(directory, *args, **kwargs):
                if Path(directory) == self.submodule:
                    if args[:2] == ("remote", "get-url"):
                        return subprocess.CompletedProcess(args, 0, "https://example.invalid/private.git\n", "")
                    if "fetch" in args:
                        fetches.append(args)
                        return subprocess.CompletedProcess(args, 128, "", "access revoked")
                return real(directory, *args, **kwargs)
            with patch.object(update, "_git", side_effect=unavailable):
                result = update.run()
            self.assertTrue(result["ok"], result)
            self.assertEqual(self.entry(result)["state"], "unavailable")
            self.assertEqual(len(fetches), 2)  # refused SHA, then ordinary refs within the same budget
            self.assertTrue(all("protocol.file.allow=always" not in args for args in fetches))
            self.assertIn("access revoked", self.entry(result)["reason"])
        with self.subTest("a symlinked plugins directory"):
            self.restore()
            elsewhere = self.root / "elsewhere"
            shutil.move(str(self.checkout / "plugins"), str(elsewhere))
            (self.checkout / "plugins").symlink_to(elsewhere)
            self.plugin_commit()
            self.bump()
            result = update.run()
            self.assertFalse(result["ok"])
            self.assertIn("symbolic link", result["reason"])
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)

    def test_update_never_installs_removes_or_repoints_on_its_own(self):
        """update never clones, deletes, repoints or plans a submodule the user did not ask for; a break goes
        unnoticed because an upstream .gitmodules URL change silently repoints the user's remote."""
        mirror = self.root / "mirror.git"

        def uninitialized():
            self.git(self.checkout, "submodule", "deinit", "-f", "plugins/compaction")
            self.plugin_commit()
            self.bump()

        def added_and_removed():
            remote, seed = self.root / "remote-plugin.git", self.root / "remote-seed"
            self.git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
            self.git(self.root, "clone", str(remote), str(seed))
            (seed / "plugin.json").write_text('{"name": "remote"}\n')
            self.git(seed, "add", "plugin.json")
            self.git(seed, "commit", "-m", "remote")
            self.git(seed, "push", "origin", "main")
            self.git(self.author, "-c", "protocol.file.allow=always", "submodule", "add", str(remote), "plugins/remote")
            self.git(self.author, "rm", "-q", "plugins/compaction")
            self.git(self.author, "commit", "-m", "add remote, drop compaction")
            self.git(self.author, "push", "origin", "main")

        def repointed():
            self.git(self.root, "clone", "--bare", str(self.plugin_remote), str(mirror))
            self.git(self.author, "config", "-f", ".gitmodules", "submodule.plugins/compaction.url", str(mirror))
            self.git(self.author, "add", ".gitmodules")
            self.git(self.author, "commit", "-m", "move compaction")
            self.git(self.author, "push", "origin", "main")

        def outside_plugins():
            self.git(self.author, "-c", "protocol.file.allow=always", "submodule", "add", str(self.plugin_remote),
                     "vendor/lib")
            self.git(self.author, "commit", "-m", "vendor")
            self.git(self.author, "push", "origin", "main")

        def left_alone(case, result):
            if case == "uninitialized":
                self.assertEqual(list(self.submodule.iterdir()), [])
            else:
                self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
                self.assertEqual(self.git(self.checkout, "config", "submodule.plugins/compaction.url"),
                                 str(self.plugin_remote))
            self.assertFalse((self.checkout / "plugins/remote/plugin.json").exists())
            self.assertFalse((self.checkout / "vendor/lib/plugin.json").exists())
            if case == "repointed":
                self.assertEqual(self.entry(result)["url_changed"]["after"], str(mirror))

        rows = (("uninitialized", uninitialized, {"plugins/compaction": "not_installed"}),
                ("added and removed", added_and_removed,
                 {"plugins/compaction": "removed", "plugins/remote": "available"}),
                ("repointed", repointed, {"plugins/compaction": "unchanged"}),
                ("outside plugins/", outside_plugins, {"plugins/compaction": "unchanged"}))
        for case, prepare, states in rows:
            with self.subTest(case):
                self.restore()
                prepare()
                upstream = self.git(self.remote, "rev-parse", "main")
                for check in (True, False):
                    result = update.run(check=check)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual({entry["path"]: entry["state"] for entry in result["plugins"]}, states)
                    left_alone(case, result)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), upstream)


if __name__ == "__main__":
    unittest.main()
