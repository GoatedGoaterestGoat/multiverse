"""Updater failure messages and fetch budgets, using temporary repositories only."""
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from multiverse import cli
from multiverse.tools import update
from test_update import UpdateTest
from test_update_plugins import PluginUpdateTest


class FailureMessageTest(UpdateTest):
    def test_failed_fast_forward_explains_why_environment_was_not_synced(self):
        (self.author / "new.txt").write_text("upstream\n")
        self.git(self.author, "add", "new.txt")
        self.git(self.author, "commit", "-m", "add file")
        self.git(self.author, "push", "origin", "main")
        (self.checkout / "new.txt").write_text("mine\n")
        result = update.run()
        self.assert_unchanged(result, "Could not fast-forward")
        self.assertEqual(result["environment"]["state"], "not_run")
        self.assertIn("did not complete", result["environment"]["reason"])
        text = update.format_output(result)
        self.assertIn("Environment not synced: the code update did not complete.", text)
        self.assertNotIn("pending", text)
        self.assertEqual((self.checkout / "new.txt").read_text(), "mine\n")

    def test_invalid_plugin_fetch_budgets_are_refused_before_fetch(self):
        for budget in (0, -1, float("inf"), float("nan")):
            with self.subTest(budget=budget), patch.object(update, "_git", wraps=update._git) as git:
                result = update.run(plugin_fetch_timeout=budget)
                self.assert_unchanged(result, "plugin-fetch-timeout must be a positive finite number")
                self.assertFalse(git.called)
        args = cli.parser().parse_args(["update", "--plugin-fetch-timeout", "240"])
        self.assertEqual(args.plugin_fetch_timeout, 240)


class FetchBudgetTest(PluginUpdateTest):
    def prepare_pin(self):
        revision = self.plugin_commit()
        self.bump()
        return revision

    def test_success_without_the_requested_commit_does_not_fetch_twice(self):
        self.prepare_pin()
        real, fetches = update._git, []

        def empty_fetch(directory, *args, **kwargs):
            if Path(directory) == self.submodule and "fetch" in args:
                fetches.append((args, kwargs))
                return subprocess.CompletedProcess(args, 0, "", "")
            return real(directory, *args, **kwargs)

        with patch.object(update, "_git", side_effect=empty_fetch):
            result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(fetches), 1)
        self.assertEqual(self.entry(result)["state"], "unavailable")
        self.assertEqual(result["environment"]["state"], "synced")

    def test_a_fetch_timeout_never_retries_and_explains_the_budget(self):
        self.prepare_pin()
        real, fetches = update._git, []

        def stalled(directory, *args, **kwargs):
            if Path(directory) == self.submodule and "fetch" in args:
                fetches.append(kwargs["timeout"])
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return real(directory, *args, **kwargs)

        with patch.object(update, "_git", side_effect=stalled):
            result = update.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(fetches), 1)
        self.assertGreater(fetches[0], 0)
        self.assertLessEqual(fetches[0], 120)
        text = update.format_output(result)
        self.assertIn("did not finish within 120 s", text)
        self.assertIn("--plugin-fetch-timeout", text)
        self.assertIn("submodule deinit -f -- plugins/compaction", text)
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
        self.assertEqual(result["environment"]["state"], "synced")

    def test_sha_refusal_falls_back_with_only_the_remaining_budget(self):
        revision = self.prepare_pin()
        real, fetches, clock = update._git, [], [10.0]

        def refuses_sha(directory, *args, **kwargs):
            if Path(directory) == self.submodule and "fetch" in args:
                fetches.append((args, kwargs["timeout"]))
                if args[-1] == revision:
                    clock[0] += 30
                    return subprocess.CompletedProcess(args, 128, "", "server refuses fetching by SHA")
            return real(directory, *args, **kwargs)

        with patch.object(update, "_git", side_effect=refuses_sha), \
                patch.object(update.time, "monotonic", side_effect=lambda: clock[0]):
            result = update.run(plugin_fetch_timeout=45)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(fetches), 2)
        self.assertEqual(fetches[0][0][-1], revision)
        self.assertEqual(fetches[1][0][-1], "origin")
        self.assertEqual([budget for _, budget in fetches], [45, 15])
        self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), revision)

    def test_real_stalled_fetch_is_killed_within_the_custom_budget(self):
        self.prepare_pin()
        real, fetches = update._git, []

        def stalled(directory, *args, **kwargs):
            if Path(directory) == self.submodule and "fetch" in args:
                fetches.append(args)
                return subprocess.run([sys.executable, "-c", "import time; time.sleep(10)"],
                                      timeout=kwargs["timeout"], capture_output=True, text=True)
            return real(directory, *args, **kwargs)

        started = time.monotonic()
        with patch.object(update, "_git", side_effect=stalled):
            result = update.run(plugin_fetch_timeout=0.15)
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(fetches), 1)
        self.assertEqual(self.entry(result)["state"], "unavailable")
        self.assertIn("did not finish within 0.15 s", update.format_output(result))
        self.assertEqual(result["environment"]["state"], "synced")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (FailureMessageTest, FetchBudgetTest)
                              for name in loader.getTestCaseNames(cls) if name in cls.__dict__)
