"""lifecycle.listing(only=...): one agent's row, built by the listing's own code, for api.Universe.agent."""
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
        for patcher in (mock.patch.object(lifecycle, "observe", observing),
                        mock.patch.object(lifecycle.native, "usage_snapshot", measuring)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return seen

    def test_the_selected_agent_alone_with_the_same_row(self):
        full = {row["id"]: row for row in lifecycle.listing(self.root)}
        seen = self.observed()
        [row] = lifecycle.listing(self.root, only=self.second.name)
        self.assertEqual(row, full[self.second.name])
        self.assertEqual(seen, [("observe", self.second.name), ("usage", self.second.name)])

    def test_absent_removed_and_malformed_targets(self):
        self.assertEqual(lifecycle.listing(self.root, only="0" * 8 + "-0000-0000-0000-" + "0" * 12), [])
        shutil.rmtree(self.third)
        self.assertEqual(lifecycle.listing(self.root, only=self.third.name), [])
        (self.first / "properties.json").write_text("{not json")
        full = {row["id"]: row for row in lifecycle.listing(self.root)}
        self.assertEqual(lifecycle.listing(self.root, only=self.first.name), [full[self.first.name]])
        self.assertEqual(full[self.first.name]["live"], "unknown")  # today's error row

    def test_missing_properties_in_an_existing_folder_is_damaged_not_removed(self):
        (self.third / 'properties.json').unlink()
        [row] = lifecycle.listing(self.root, only=self.third.name)
        self.assertEqual(row['id'], self.third.name)
        self.assertIn('properties.json', row['damaged']['file'])
        self.assertIn('No such file', row['damaged']['reason'])
        self.assertIn(row, lifecycle.listing(self.root))

    def test_only_is_one_directory_name(self):
        for value in ("", ".", "..", "../x", "a/b", "/abs", "a\\b", 3):
            with self.subTest(value):
                with self.assertRaisesRegex(ValueError, "Not an agent ID"):
                    lifecycle.listing(self.root, only=value)

    def test_the_default_listing_is_unchanged(self):
        child = self.subverse("child")
        self.agent("below", root=child)
        self.assertEqual(lifecycle.listing(self.root), lifecycle.listing(self.root, only=None))
        self.assertEqual([row["name"] for row in lifecycle.listing(self.root, recursive=True)].count("below"), 1)
        self.assertEqual(lifecycle.listing(child, only=None), lifecycle.listing(child))

    def test_api_agent_observes_only_the_agent_it_returns(self):
        self.write_plugin("reader", {"permissions": ["agents.read"]})
        self.enable("reader")
        universe = api.Plugin(loader.load(self.entry("reader"))).universe(self.root)
        seen = self.observed()
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
