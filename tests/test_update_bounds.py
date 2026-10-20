"""The plugin fetch budget of mv2 update, using temporary repositories only."""
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from multiverse.tools import update
from test_update import PluginUpdateTest


class FetchBudgetTest(PluginUpdateTest):
    def prepare_pin(self):
        revision = self.plugin_commit()
        self.bump()
        return revision

    def test_plugin_fetch_budget_is_split_and_validated(self):
        """With a mocked git and clock, the plugin fetch budget is split between the SHA fetch and its fallback, a
        timeout never retries, and 0, -1, inf and nan are refused before any git call; a break hangs mv2 update only
        on a stalled or refusing remote, which local test remotes never produce."""
        for budget in (0, -1, float("inf"), float("nan")):
            with self.subTest(budget=budget), patch.object(update, "_git", wraps=update._git) as git:
                result = update.run(plugin_fetch_timeout=budget)
                self.assert_unchanged(result, "plugin-fetch-timeout must be a positive finite number")
                self.assertFalse(git.called)
        real = update._git
        for case in ("an answer without the commit", "a timeout", "the SHA refused"):
            with self.subTest(case):
                self.restore()
                revision = self.prepare_pin()
                fetches, clock = [], [10.0]

                def remote(directory, *args, **kwargs):
                    if Path(directory) == self.submodule and "fetch" in args:
                        fetches.append((args[-1], kwargs["timeout"]))
                        if case == "a timeout":
                            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
                        if case == "an answer without the commit":
                            return subprocess.CompletedProcess(args, 0, "", "")
                        if args[-1] == revision:
                            clock[0] += 30
                            return subprocess.CompletedProcess(args, 128, "", "server refuses fetching by SHA")
                    return real(directory, *args, **kwargs)

                with patch.object(update, "_git", side_effect=remote), \
                        patch.object(update.time, "monotonic", side_effect=lambda: clock[0]):
                    result = update.run(plugin_fetch_timeout=45)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["environment"]["state"], "synced")
                if case == "the SHA refused":  # the fallback gets only what the first attempt left
                    self.assertEqual(fetches, [(revision, 45), ("origin", 15)])
                    self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), revision)
                else:  # never fetched twice
                    self.assertEqual(fetches, [(revision, 45)])
                    self.assertEqual(self.entry(result)["state"], "unavailable")
                    self.assertEqual(self.git(self.submodule, "rev-parse", "HEAD"), self.old_pin)
                if case == "a timeout":
                    self.assertIn("--plugin-fetch-timeout", self.entry(result)["reason"])

    def test_real_stalled_fetch_is_killed_within_the_custom_budget(self):
        """A really stalled fetch subprocess is killed at the budget; the only proof that the timeout reaches the
        subprocess, and a break shows only when a real network stalls."""
        self.prepare_pin()
        real, fetches, waited = update._git, [], []

        def stalled(directory, *args, **kwargs):
            if Path(directory) == self.submodule and "fetch" in args:
                fetches.append(args)
                began = time.monotonic()
                try:
                    return subprocess.run([sys.executable, "-c", "import time; time.sleep(10)"],
                                          timeout=kwargs["timeout"], capture_output=True, text=True)
                finally:
                    waited.append(time.monotonic() - began)
            return real(directory, *args, **kwargs)

        started = time.monotonic()
        with patch.object(update, "_git", side_effect=stalled):
            result = update.run(plugin_fetch_timeout=0.15)
        # The stalled fetch itself is cut at its budget; the update's other git calls slow down under load,
        # but nothing in the whole call waits out the 10 s stall.
        self.assertLess(waited[0], 3)
        self.assertLess(time.monotonic() - started, 9)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(fetches), 1)
        self.assertEqual(self.entry(result)["state"], "unavailable")
        self.assertIn("did not finish within 0.15 s", update.format_output(result))
        self.assertEqual(result["environment"]["state"], "synced")


if __name__ == "__main__":
    unittest.main()
