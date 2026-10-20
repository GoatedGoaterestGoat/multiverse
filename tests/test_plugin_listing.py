"""lifecycle.listing(only=...): one agent's row, built by the listing's own code, for api.Universe.agent."""
import contextlib
from pathlib import Path
import sys
import shutil
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import lifecycle


class OneAgentListingTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.agent("first")
        self.second = self.agent("second", harness="codex")
        self.third = self.agent("third")

    @contextlib.contextmanager
    def observed(self):
        """Patch observe and usage_snapshot to record which agents they ran for."""
        seen = []
        observe, usage = lifecycle.observe, lifecycle.native.usage_snapshot

        def observing(properties, facts, snapshots=None):
            seen.append(("observe", properties["id"]))
            return observe(properties, facts, snapshots)

        def measuring(scope, properties, facts, **options):
            seen.append(("usage", properties["id"]))
            return usage(scope, properties, facts, **options)
        with mock.patch.object(lifecycle, "observe", observing), \
             mock.patch.object(lifecycle.native, "usage_snapshot", measuring):
            yield seen

    def test_one_agent_lookup_validates_the_name_and_matches_the_listing(self):
        """The one-agent lookup plugins use accepts only a single directory name (no .., / or absolute path), keeps
        the full listing's damage and identity checks, and builds the same row; fragile because it is a second path
        that takes untrusted input to the filesystem, and a break gives plugins the wrong agent while the dashboard's
        listing still works."""
        with self.subTest("the selected agent alone, with the same row"):
            full = {row["id"]: row for row in lifecycle.listing(self.root)}
            with self.observed() as seen:
                [row] = lifecycle.listing(self.root, only=self.second.name)
            self.assertEqual(row, full[self.second.name])
            self.assertEqual(seen, [("observe", self.second.name), ("usage", self.second.name)])
        with self.subTest("api.Universe.agent observes only the agent it returns"):
            self.write_plugin("reader", {"permissions": ["agents.read"]})
            self.enable("reader")
            universe = api.Plugin(loader.load(self.entry("reader"))).universe(self.root)
            with self.observed() as seen:
                self.assertEqual(universe.agent("second").id, self.second.name)
                self.assertEqual(universe.agent(self.third.name[:8]).name, "third")
            self.assertEqual({identity for _, identity in seen}, {self.second.name, self.third.name})
            with self.assertRaisesRegex(api.Error, "No agent named ghost"):
                universe.agent("ghost")
            # A record whose own ID names another agent is never returned as that agent.
            data = (self.first / "properties.json").read_text().replace(self.first.name, self.second.name)
            (self.first / "properties.json").write_text(data)
            with self.assertRaisesRegex(api.Error, "No agent named first here"):
                universe.agent("first")
            self.agent("second")  # a name now shared by two agents
            with self.assertRaisesRegex(api.Error, "Expected one agent matching 'second'"):
                universe.agent("second")
        with self.subTest("only is one directory name"):
            for value in ("", ".", "..", "../x", "a/b", "/abs", "a\\b", 3):
                with self.subTest(value):
                    with self.assertRaisesRegex(ValueError, "Not an agent ID"):
                        lifecycle.listing(self.root, only=value)
        with self.subTest("absent, removed and malformed targets"):
            self.assertEqual(lifecycle.listing(self.root, only="0" * 8 + "-0000-0000-0000-" + "0" * 12), [])
            shutil.rmtree(self.third)
            self.assertEqual(lifecycle.listing(self.root, only=self.third.name), [])
            (self.first / "properties.json").write_text("{not json")
            full = {row["id"]: row for row in lifecycle.listing(self.root)}
            self.assertEqual(lifecycle.listing(self.root, only=self.first.name), [full[self.first.name]])
            self.assertEqual(full[self.first.name]["live"], "unknown")  # today's error row
        with self.subTest("missing properties in an existing folder is damaged, not removed"):
            fourth = self.agent("fourth")
            (fourth / "properties.json").unlink()
            [row] = lifecycle.listing(self.root, only=fourth.name)
            self.assertEqual(row["id"], fourth.name)
            self.assertIn("properties.json", row["damaged"]["file"])
            self.assertIn("No such file", row["damaged"]["reason"])
            self.assertIn(row, lifecycle.listing(self.root))
