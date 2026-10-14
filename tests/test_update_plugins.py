"""mv2 update with plugin submodules: temporary bare remotes, file protocol and a fake uv only."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from multiverse.tools import update
from test_update import UpdateTest


class PluginUpdateTest(UpdateTest):
    def setUp(self):
        super().setUp()
        self.plugin_remote = self.root / "compaction.git"
        self.plugin_author = self.root / "compaction-author"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.plugin_remote))
        self.git(self.root, "clone", str(self.plugin_remote), str(self.plugin_author))
        (self.plugin_author / "plugin.json").write_text('{"name": "compaction"}\n')
        self.git(self.plugin_author, "add", "plugin.json")
        self.git(self.plugin_author, "commit", "-m", "plugin one")
        self.git(self.plugin_author, "push", "origin", "main")
        self.git(self.author, "-c", "protocol.file.allow=always", "submodule", "add", str(self.plugin_remote),
                 "plugins/compaction")
        self.git(self.author, "commit", "-m", "pin compaction")
        self.git(self.author, "push", "origin", "main")
        self.git(self.checkout, "pull", "--ff-only")
        self.git(self.checkout, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "plugins/compaction")
        self.before = self.git(self.checkout, "rev-parse", "HEAD")
        self.submodule = self.checkout / "plugins/compaction"
        self.old_pin = self.git(self.submodule, "rev-parse", "HEAD")
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

    def test_a_clean_pin_move_updates_the_plugin(self):
        new = self.plugin_commit()
        self.bump()
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), new)
        entry = self.entry(result)
        self.assertEqual((entry["state"], entry["before"], entry["after"]), ("updated", self.old_pin, new))
        text = update.format_output(result)
        self.assertIn(f"Updated plugins/compaction from {self.old_pin[:7]} to {new[:7]}.", text)
        self.assertIn("reload the Claude code of plugins/compaction", text)
        self.assertEqual(json.loads(self.log.read_text())["args"], ["sync", "--locked"])

    def test_check_plans_without_moving_anything(self):
        new = self.plugin_commit()
        self.bump()
        result = update.run(check=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.entry(result)["state"], "would_update")
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
        self.assertIn(f"would move from {self.old_pin[:7]} to {new[:7]}; running agents would reload", update.format_output(result))
        self.assertFalse(self.log.exists())

    def test_local_changes_or_another_commit_refuse_before_the_merge(self):
        self.plugin_commit()
        self.bump()
        for case in ("unstaged", "staged", "elsewhere"):
            with self.subTest(case):
                if case == "elsewhere":
                    self.git(self.submodule, "fetch", "origin")
                    self.git(self.submodule, "checkout", "-q", "origin/main")
                else:
                    (self.submodule / "plugin.json").write_text('{"name": "edited"}\n')
                    if case == "staged":
                        self.git(self.submodule, "add", "plugin.json")
                result = update.run()
                self.assertFalse(result["ok"])
                self.assertIn("plugins/compaction", result["reason"])
                self.assertIn("has local commits" if case == "elsewhere" else "has local changes", result["reason"])
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
                checked = update.run(check=True)
                self.assertFalse(checked["ok"])
                self.assertIn("plugins/compaction", checked["reason"])
                self.git(self.submodule, "checkout", "-q", "-f", self.old_pin)
        self.assertFalse(self.log.exists())

    def test_untracked_files_are_allowed_and_a_collision_is_preserved(self):
        (self.submodule / "notes.txt").write_text("mine\n")
        self.plugin_commit("other.ts")
        self.bump()
        self.assertTrue(update.run()["ok"])
        self.assertEqual((self.submodule / "notes.txt").read_text(), "mine\n")
        self.before = self.git(self.checkout, "rev-parse", "HEAD")
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
        self.assertNotEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)  # the code moved on
        self.assertEqual(result["environment"]["state"], "synced")  # and its environment was synced
        self.assertIn(f"git -C {self.checkout} submodule update --checkout -- plugins/compaction", result["reason"])
        self.assertIn("Environment synced.", update.format_output(result))

    def test_a_plugin_failure_and_a_sync_failure_are_both_reported(self):
        (self.submodule / "hooks.ts").write_text("untracked\n")
        self.plugin_commit("hooks.ts", "theirs\n")
        self.bump()
        self.uv.write_text("#!/bin/sh\necho 'sync broke' >&2\nexit 1\n")
        result = update.run()
        self.assertFalse(result["ok"])
        self.assertIn("plugins/compaction could not be updated", result["reason"])
        self.assertIn("environment sync failed: sync broke", result["reason"])
        self.assertIn("Environment sync failed", update.format_output(result))

    def test_an_uninitialized_plugin_is_never_fetched_or_initialized(self):
        self.git(self.checkout, "submodule", "deinit", "-f", "plugins/compaction")
        self.plugin_commit()
        self.bump()
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.entry(result)["state"], "not_installed")
        self.assertEqual(list(self.submodule.iterdir()), [])

    def test_new_and_removed_plugins_are_reported_and_left_alone(self):
        remote = self.root / "remote-plugin.git"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
        seed = self.root / "remote-seed"
        self.git(self.root, "clone", str(remote), str(seed))
        (seed / "plugin.json").write_text('{"name": "remote"}\n')
        self.git(seed, "add", "plugin.json")
        self.git(seed, "commit", "-m", "remote")
        self.git(seed, "push", "origin", "main")
        self.git(self.author, "-c", "protocol.file.allow=always", "submodule", "add", str(remote), "plugins/remote")
        self.git(self.author, "rm", "-q", "plugins/compaction")
        self.git(self.author, "commit", "-m", "add remote, drop compaction")
        self.git(self.author, "push", "origin", "main")
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.entry(result, "plugins/remote")["state"], "available")
        self.assertEqual(self.entry(result)["state"], "removed")
        self.assertFalse((self.checkout / "plugins/remote/plugin.json").exists())
        self.assertTrue((self.submodule / "plugin.json").exists())
        text = update.format_output(result)
        self.assertIn("New optional plugin remote: install it with mv2 plugins install remote.", text)
        self.assertIn("plugins/compaction is no longer part of mv2; it was left on disk.", text)

    def test_update_mode_config_is_overridden_and_nothing_nested_is_touched(self):
        marker = self.root / "custom-ran"
        self.git(self.checkout, "config", "submodule.plugins/compaction.update", f"!touch {marker}")
        self.git(self.submodule, "config", "fetch.recurseSubmodules", "true")
        new = self.plugin_commit()
        self.bump()
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertFalse(marker.exists())
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), new)

    def test_changes_made_during_the_update_are_preserved(self):
        self.plugin_commit()
        self.bump()
        real = update._git

        def during_fetch(checkout, *args, **kwargs):
            result = real(checkout, *args, **kwargs)
            if args[:1] == ("fetch",) and Path(checkout) == self.checkout:
                (self.submodule / "plugin.json").write_text('{"name": "edited meanwhile"}\n')
            return result
        with patch.object(update, "_git", side_effect=during_fetch):
            result = update.run()
        self.assertFalse(result["ok"])
        self.assertIn("plugins/compaction changed while mv2 fetched its upstream", result["reason"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
        self.git(self.submodule, "checkout", "-q", "-f", self.old_pin)

        def during_merge(checkout, *args, **kwargs):
            result = real(checkout, *args, **kwargs)
            if args[:1] == ("merge",):
                (self.submodule / "plugin.json").write_text('{"name": "edited after merge"}\n')
            return result
        with patch.object(update, "_git", side_effect=during_merge):
            result = update.run()
        self.assertFalse(result["ok"])
        self.assertEqual(self.entry(result)["reason"], "it changed during the update and was left as it is")
        self.assertEqual((self.submodule / "plugin.json").read_text(), '{"name": "edited after merge"}\n')

    def test_a_changed_url_is_shown_not_applied(self):
        mirror = self.root / "mirror.git"
        self.git(self.root, "clone", "--bare", str(self.plugin_remote), str(mirror))
        self.git(self.author, "config", "-f", ".gitmodules", "submodule.plugins/compaction.url", str(mirror))
        self.git(self.author, "add", ".gitmodules")
        self.git(self.author, "commit", "-m", "move compaction")
        self.git(self.author, "push", "origin", "main")
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.entry(result)["url_changed"]["after"], str(mirror))
        self.assertIn("submodule sync -- plugins/compaction", update.format_output(result))
        self.assertEqual(self.git(self.checkout, "config", "submodule.plugins/compaction.url"), str(self.plugin_remote))

    def test_a_non_plugin_submodule_is_not_planned(self):
        self.git(self.author, "-c", "protocol.file.allow=always", "submodule", "add", str(self.plugin_remote), "vendor/lib")
        self.git(self.author, "commit", "-m", "vendor")
        self.git(self.author, "push", "origin", "main")
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual([item["path"] for item in result["plugins"]], ["plugins/compaction"])
        self.assertFalse((self.checkout / "vendor/lib/plugin.json").exists())


class RetryTest(PluginUpdateTest):
    def test_an_unavailable_remote_warns_and_retries_until_access_returns(self):
        new = self.plugin_commit()
        self.bump()
        offline = self.root / "offline.git"
        self.plugin_remote.rename(offline)
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result["after"], self.before)
        self.assertEqual(self.entry(result)["state"], "unavailable")
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
        text = update.format_output(result)
        self.assertIn(f"Warning: plugins/compaction was left at {self.old_pin[:7]}", text)
        self.assertIn("environment synced", text)
        self.assertIn(f"git -C {self.checkout} submodule deinit -f -- plugins/compaction", text)
        self.assertIn("save any local files first", text)
        real = update._git

        def no_plugin_fetch(directory, *args, **kwargs):
            if Path(directory) == self.submodule:
                self.assertNotIn("fetch", args)  # --check never contacts this remote
            return real(directory, *args, **kwargs)
        with patch.object(update, "_git", side_effect=no_plugin_fetch):
            checked = update.run(check=True)
        self.assertTrue(checked["ok"], checked)
        self.assertEqual(self.entry(checked)["state"], "would_update")
        self.assertIn("would stay at", update.format_output(checked))
        # Further mv2 commits can update even while the optional plugin remains unavailable.
        self.advance()
        again = update.run()
        self.assertTrue(again["ok"], again)
        self.assertEqual(self.entry(again)["state"], "unavailable")
        offline.rename(self.plugin_remote)
        recovered = update.run()
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["commits"], 0)  # retry even with no new mv2 commit
        self.assertEqual(self.entry(recovered)["state"], "updated")
        self.assertEqual(self.entry(recovered)["before"], self.old_pin)
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), new)
        self.assertTrue(self.log.exists())

    def test_a_clean_behind_checkout_with_the_pin_cached_moves_without_a_fetch(self):
        new = self.plugin_commit()
        self.bump()
        self.git(self.submodule, "fetch", "origin")
        self.git(self.checkout, "pull", "--ff-only")  # simulate a main update whose plugin was left behind
        self.plugin_remote.rename(self.root / "offline.git")
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["commits"], 0)
        self.assertEqual(self.entry(result)["state"], "updated")
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), new)

    def test_the_manual_deinit_advice_works_for_a_behind_checkout(self):
        self.plugin_commit()
        self.bump()
        self.plugin_remote.rename(self.root / "offline.git")
        result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertIn(f"git -C {self.checkout} submodule deinit -f -- plugins/compaction",
                      update.format_output(result))
        self.git(self.checkout, "submodule", "deinit", "-f", "--", "plugins/compaction")
        later = update.run()
        self.assertTrue(later["ok"], later)
        self.assertEqual(self.entry(later)["state"], "not_installed")
        self.assertFalse((self.submodule / "plugin.json").exists())

    def test_missing_pin_never_allows_local_commits_to_be_overwritten(self):
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
        self.assertIn("local commits", result["reason"])
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), local)
        self.assertNotEqual(local, new)
        self.assertEqual((self.submodule / "mine.txt").read_text(), "mine\n")
        # With the pin now fetched the next preflight refuses too, before syncing or moving anything.
        self.log.unlink()
        self.assertFalse(update.run()["ok"])
        self.assertFalse(self.log.exists())

    def test_a_local_remote_fetch_works_without_ambient_file_protocol_permission(self):
        new = self.plugin_commit()
        self.bump()
        self.git(self.submodule, "remote", "set-url", "origin", self.plugin_remote.as_uri())
        real = update._git
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
        config = self.git(self.submodule, "config", "--local", "--list")
        self.assertNotIn("protocol.file.allow", config)

    def test_changes_during_a_plugin_fetch_are_preserved(self):
        self.plugin_commit()
        self.bump()
        real = update._git

        def edited(directory, *args, **kwargs):
            result = real(directory, *args, **kwargs)
            if Path(directory) == self.submodule and "fetch" in args:
                (self.submodule / "plugin.json").write_text("edited during fetch\n")
            return result
        with patch.object(update, "_git", side_effect=edited):
            result = update.run()
        self.assertFalse(result["ok"], result)
        self.assertIn("changed during the update", self.entry(result)["reason"])
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
        self.assertEqual((self.submodule / "plugin.json").read_text(), "edited during fetch\n")

    def test_a_remote_fetch_never_enables_file_transport(self):
        self.plugin_commit()
        self.bump()
        real = update._git
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
        self.assertEqual(len(fetches), 1)
        self.assertNotIn("protocol.file.allow=always", fetches[0])
        self.assertIn("access revoked", update.format_output(result))


class HardeningTest(PluginUpdateTest):
    def other_commit(self):
        return self.plugin_commit("staged.ts")

    def test_a_staged_pin_is_the_checkouts_own_change(self):
        other = self.other_commit()
        self.git(self.checkout, "update-index", "--cacheinfo", f"160000,{other},plugins/compaction")
        for check in (True, False):
            result = update.run(check=check)
            self.assertFalse(result["ok"])
            self.assertIn("uncommitted changes to tracked files", result["reason"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)
        self.assertFalse(self.log.exists())

    def test_the_index_pin_is_checked_again_before_moving(self):
        new = self.plugin_commit()
        self.bump()
        other = self.plugin_commit("later.ts")
        real = update._git

        def after_merge(checkout, *args, **kwargs):
            result = real(checkout, *args, **kwargs)
            if args[:1] == ("merge",):
                real(checkout, "update-index", "--cacheinfo", f"160000,{other},plugins/compaction")
            return result
        with patch.object(update, "_git", side_effect=after_merge):
            result = update.run()
        self.assertFalse(result["ok"])
        self.assertIn("pins for it changed during the update", self.entry(result)["reason"])
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
        self.assertNotEqual(new, other)

    def test_a_failing_read_after_the_merge_fails_only_that_plugin(self):
        self.plugin_commit()
        self.bump()
        real = update._git
        merged = []

        def timing_out(checkout, *args, **kwargs):
            if args[:1] == ("merge",):
                merged.append(True)
            elif merged and Path(checkout) == self.submodule and args[:2] == ("rev-parse", "HEAD"):
                raise subprocess.TimeoutExpired(["git"], 120)
            return real(checkout, *args, **kwargs)
        with patch.object(update, "_git", side_effect=timing_out):
            result = update.run()
        self.assertFalse(result["ok"])
        self.assertEqual(self.entry(result)["state"], "failed")
        self.assertIn("did not finish within 120 s", result["reason"])
        self.assertEqual(result["environment"]["state"], "synced")
        self.assertTrue(self.log.exists())

    def test_an_unreadable_plugin_refuses_before_anything_moves(self):
        self.plugin_commit()
        self.bump()
        real = update._git

        def unreadable(checkout, *args, **kwargs):
            if Path(checkout) == self.submodule and args[:1] == ("status",):
                return subprocess.CompletedProcess(args, 128, "", "fatal: index file corrupt")
            return real(checkout, *args, **kwargs)
        with patch.object(update, "_git", side_effect=unreadable):
            result = update.run()
        self.assertFalse(result["ok"])
        self.assertIn("plugins/compaction could not be checked", result["reason"])
        self.assertIn("index file corrupt", result["reason"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)

    def test_a_uv_that_times_out_or_cannot_start_keeps_the_plugin_failure(self):
        (self.submodule / "hooks.ts").write_text("untracked\n")
        self.plugin_commit("hooks.ts", "theirs\n")
        self.bump()
        real = update.subprocess.run
        for error in (subprocess.TimeoutExpired(["uv"], 600), OSError("exec format error")):
            with self.subTest(error=type(error).__name__):
                self.git(self.checkout, "reset", "-q", "--hard", self.before)

                def failing_uv(argv, *args, error=error, **kwargs):
                    if argv[:2] == [str(self.uv), "sync"]:
                        raise error
                    return real(argv, *args, **kwargs)
                with patch.object(update.subprocess, "run", side_effect=failing_uv):
                    result = update.run()
                self.assertFalse(result["ok"])
                self.assertIn("plugins/compaction could not be updated", result["reason"])
                self.assertIn("environment sync failed", result["reason"])
                self.assertEqual(result["environment"]["state"], "failed")
                text = update.format_output(result)
                self.assertIn("plugins/compaction could not be updated", text)
                self.assertIn("uv sync --locked", text)

    def test_a_symlinked_plugins_directory_is_refused(self):
        import shutil
        elsewhere = self.root / "elsewhere"
        shutil.move(str(self.checkout / "plugins"), str(elsewhere))
        (self.checkout / "plugins").symlink_to(elsewhere)
        self.plugin_commit()
        self.bump()
        result = update.run()
        self.assertFalse(result["ok"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)

    def test_a_repository_swapped_in_at_the_same_path_is_not_updated(self):
        import shutil
        self.plugin_commit()
        self.bump()
        real = update._git

        def swap(checkout, *args, **kwargs):
            result = real(checkout, *args, **kwargs)
            if args[:1] == ("merge",):
                shutil.move(str(self.submodule), str(self.root / "moved-away"))
                real(self.root, "clone", "-q", str(self.plugin_remote), str(self.submodule))
                real(self.submodule, "checkout", "-q", self.old_pin)
            return result
        with patch.object(update, "_git", side_effect=swap):
            result = update.run()
        self.assertFalse(result["ok"])
        self.assertEqual(self.entry(result)["reason"], "it changed during the update and was left as it is")
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)

    def test_check_words_a_removal_as_planned(self):
        self.git(self.author, "rm", "-q", "plugins/compaction")
        self.git(self.author, "commit", "-m", "drop compaction")
        self.git(self.author, "push", "origin", "main")
        self.assertIn("plugins/compaction would no longer be part of mv2; it would be left on disk.",
                      update.format_output(update.run(check=True)))


class ConflictTest(PluginUpdateTest):
    def test_a_conflicted_plugin_path_refuses(self):
        other = self.plugin_commit()
        entries = "".join(f"160000 {commit} {stage}\tplugins/compaction\n"
                          for commit, stage in ((self.old_pin, 1), (self.old_pin, 2), (other, 3)))
        subprocess.run(["git", "-C", str(self.checkout), "update-index", "--index-info"], input=entries, text=True,
                       check=True, capture_output=True)
        result = update.run()
        self.assertFalse(result["ok"])
        self.assertIn("plugins/compaction has a merge conflict", result["reason"])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)


class NoPluginsTest(UpdateTest):
    def test_no_plugin_submodules_keeps_the_original_shape(self):
        self.advance()
        for check in (True, False):
            result = update.run(check=check)
            self.assertTrue(result["ok"], result)
            self.assertNotIn("plugins", result)
            self.assertNotIn("plugins/", update.format_output(result))


def load_tests(test_loader, tests, pattern):
    """Only this module's tests: the imported fixture's own run in test_update."""
    suite = unittest.TestSuite()
    for case in (PluginUpdateTest, RetryTest, HardeningTest, ConflictTest, NoPluginsTest):
        for name in test_loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite
