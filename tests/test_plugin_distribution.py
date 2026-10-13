"""Where plugins come from beyond a universe: the checkout's submodules and installed packages."""
import json
import os
from importlib.metadata import EntryPoint
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase


class SourcesTest(PluginTestCase):
    def test_checkout_plugins_and_empty_submodules(self):
        checkout = self.work / "checkout-plugins"
        self.write_plugin("compaction", {"version": "2.0.0"}, where=checkout)
        (checkout / "remote").mkdir()  # an uninitialized submodule: an empty directory
        with mock.patch.object(loader, "CHECKOUT", checkout):
            found = {entry.name: entry for entry in plugins.entries(self.root)}
        self.assertEqual((found["compaction"].source, found["compaction"].manifest.version), ("checkout", "2.0.0"))
        self.assertNotIn("remote", found)
        self.assertEqual(found["compaction"].state, "off")  # trusted, but off until plugins.json turns it on

    def test_installed_packages_are_located_without_importing_them(self):
        site = self.work / "site"
        package = site / "mv2_example_plugin"
        self.write_plugin("mv2_example_plugin", where=site)
        (package / "plugin.json").write_text(json.dumps(
            {"schema": 1, "name": "example", "version": "1.2.3", "api": "1.0"}))
        (package / "__init__.py").write_text(f"open({str(self.work / 'imported')!r}, 'w')\n")
        points = [EntryPoint("example", "mv2_example_plugin", loader.ENTRY_POINTS),
                  EntryPoint("broken", "no_such_package", loader.ENTRY_POINTS)]
        sys.path.insert(0, str(site))
        self.addCleanup(sys.path.remove, str(site))
        with mock.patch.object(loader, "_installed", None), \
             mock.patch("importlib.metadata.entry_points", return_value=points):
            found = {entry.name: entry for entry in plugins.entries(self.root)}
        self.assertEqual((found["example"].source, found["example"].manifest.version), ("installed", "1.2.3"))
        self.assertFalse((self.work / "imported").exists())

    def test_precedence_across_every_source(self):
        checkout = self.work / "checkout-plugins"
        self.write_plugin("twin", {"version": "checkout"}, where=checkout)
        installed = self.write_plugin("twin", {"version": "installed"}, where=self.work / "site")
        with mock.patch.object(loader, "CHECKOUT", checkout), mock.patch.object(loader, "_installed", [installed]):
            self.assertEqual(self.entry("twin").source, "path" if (self.plugins_dir / "twin").exists() else "installed")
            self.assertEqual([source for source, _ in self.entry("twin").shadowed], ["checkout"])


MIGRATING = """
def register(mv):
    @mv.settings.migrate
    def migrate(data, source):
        if source == "legacy":
            on = bool(data.pop("enabled", False))
            return {"schema": 2, **data}, on
        return {**data, "schema": 2, "upgraded_from": source}
"""


class MigrateTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("tree", {"module": "plugin", "settings": {"version": 2, "legacy": "tree.json"}},
                          {"plugin.py": MIGRATING})

    def test_a_legacy_settings_file_keeps_the_plugin_on_until_plugins_json_says(self):
        self.assertFalse(self.entry("tree").on)
        (self.root / "tree.json").write_text(json.dumps({"enabled": False}))
        self.assertTrue(self.entry("tree").on)  # the plugin's own settings decide what it does
        self.enable("tree", on=False)
        self.assertFalse(self.entry("tree").on)
        child = self.subverse("child")
        self.assertFalse(self.entry("tree", child).on)  # no legacy file there

    def test_refresh_can_tell_which_settings_files_await_migration(self):
        self.assertEqual(plugins.awaiting_migration(self.root), {})
        (self.root / "tree.json").write_text("{}")
        self.assertEqual(plugins.awaiting_migration(self.root), {"plugin-settings/tree.json": "tree.json"})
        (self.root / "plugin-settings").mkdir(exist_ok=True)  # the base template ships it
        (self.root / "plugin-settings/tree.json").write_text('{"schema": 2}')
        self.assertEqual(plugins.awaiting_migration(self.root), {})

    def test_a_legacy_file_with_its_plugin_off_is_reported(self):
        (self.root / "tree.json").write_text("{}")
        self.assertEqual(plugins.problems(self.root), [])
        self.enable("tree", on=False)
        [problem] = plugins.problems(self.root)
        self.assertIn("tree.json is still here, but the tree plugin is not used: plugins.json turns it off", problem)
        self.enable("tree")
        with mock.patch.dict(os.environ, {"MV2_PLUGINS": "off"}):
            [problem] = plugins.problems(self.root)
        self.assertIn("MV2_PLUGINS=off loads no plugins", problem)

    def test_legacy_settings_move_to_the_plugin_and_turn_it_on(self):
        from multiverse.tools import plugins as tool
        (self.root / "tree.json").write_text(json.dumps({"enabled": True, "budget": 5}))
        self.assertEqual(plugins.settings_of(self.root, "tree")[0], {"enabled": True, "budget": 5})  # read until migrated
        plan = tool.run(self.root, plugin_action="migrate")
        self.assertEqual([item["kind"] for item in plan["planned"]], ["legacy"])
        self.assertIn("Would migrate tree", tool.text("migrate", plan))
        self.assertTrue((self.root / "tree.json").exists())
        done = tool.run(self.root, plugin_action="migrate", apply=True)
        self.assertEqual(done["applied"][0]["on"], True)
        self.assertFalse((self.root / "tree.json").exists())
        self.assertEqual(json.loads((self.root / "plugin-settings/tree.json").read_text()), {"schema": 2, "budget": 5})
        self.assertTrue(self.entry("tree").on)
        self.assertTrue(Path(done["applied"][0]["backup"]).is_file())
        self.assertEqual(tool.text("migrate", tool.run(self.root, plugin_action="migrate")), "Nothing to migrate.")

    def test_an_older_settings_version_is_upgraded(self):
        from multiverse.tools import plugins as tool
        (self.root / "plugin-settings").mkdir(exist_ok=True)  # the base template ships it
        (self.root / "plugin-settings/tree.json").write_text(json.dumps({"schema": 1, "budget": 3}))
        self.enable("tree")
        self.assertIn("run mv2 plugins migrate", self.entry("tree").problem)
        tool.run(self.root, plugin_action="migrate", apply=True)
        self.assertEqual(json.loads((self.root / "plugin-settings/tree.json").read_text()),
                         {"schema": 2, "budget": 3, "upgraded_from": 1})
        self.assertIn(self.entry("tree").state, ("on", "loaded"))

    def test_a_plugin_without_a_migration_says_so(self):
        from multiverse.tools import plugins as tool
        self.write_plugin("plain", {"settings": {"version": 1, "legacy": "plain.json"}})
        (self.root / "plain.json").write_text("{}")
        planned = tool.run(self.root, plugin_action="migrate", apply=True)["planned"]
        self.assertIn("offers no migration", next(item for item in planned if item["plugin"] == "plain")["problem"])


class InstallTest(PluginTestCase):
    def git(self, *args, cwd, check=True):
        import subprocess
        environment = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                       "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(self.home), "PATH": os.environ["PATH"],
                       "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, env=environment,
                              capture_output=True, text=True, check=check)

    def test_a_checkout_submodule_is_listed_and_installed(self):
        upstream = self.write_plugin("compaction", where=self.work / "upstream")
        self.git("init", "-q", cwd=upstream)
        self.git("add", ".", cwd=upstream)
        self.git("commit", "-qm", "plugin", cwd=upstream)
        checkout = self.work / "mv2"
        (checkout / "src/multiverse/plugins").mkdir(parents=True)
        self.git("init", "-q", cwd=checkout)
        self.git("submodule", "add", "-q", str(upstream), "plugins/compaction", cwd=checkout)
        self.git("commit", "-qm", "pin", cwd=checkout)
        self.git("submodule", "deinit", "-f", "plugins/compaction", cwd=checkout)
        self.assertFalse((checkout / "plugins/compaction/plugin.json").exists())
        # A local path URL (no hosted copy yet): install allows Git's file protocol itself, for that command only.
        os.environ.pop("GIT_CONFIG_PARAMETERS", None)
        with mock.patch.object(loader, "CHECKOUT", checkout / "plugins"):
            self.assertEqual(list(plugins.submodules()), ["compaction"])
            self.enable("compaction")
            self.assertIn("run mv2 plugins install compaction", plugins.problems(self.root)[0])
            result = plugins.install("compaction")
            self.assertTrue(result["installed"])
            self.assertEqual(self.entry("compaction").source, "checkout")
            with self.assertRaisesRegex(plugins.PluginError, "No plugin named ghost"):
                plugins.install("ghost")
        with mock.patch.object(loader, "CHECKOUT", None), self.assertRaisesRegex(plugins.PluginError, "with pip"):
            plugins.install("compaction")
        self.assertNotIn("protocol", (checkout / ".git" / "config").read_text())  # never written to config

    def test_which_submodule_urls_are_local(self):
        for url, local in (("/Users/me/GOAT", True), ("../GOAT", True), ("file:///x", True), ("~/GOAT", True),
                           ("https://github.com/o/GOAT.git", False), ("git@github.com:o/GOAT.git", False), (None, False)):
            with self.subTest(url):
                self.assertEqual(plugins._local_url(url), local)
