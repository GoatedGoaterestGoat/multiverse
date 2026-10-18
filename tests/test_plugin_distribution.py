"""Where plugins come from beyond a universe: the checkout's submodules and installed packages."""
import contextlib
import io
import json
import os
from importlib.metadata import EntryPoint
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import cli, plugins
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.universe import templates


class SourcesTest(PluginTestCase):
    def test_installed_packages_are_located_without_importing_them(self):
        """Discovery finds installed packages through entry points and the manifest without importing them, finds
        checkout plugins but not empty submodules, and ranks every source; fragile because EntryPoint.load() is the
        obvious call, and a break runs third-party package code in every mv2 process with identical results."""
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
        with self.subTest("checkout plugins and empty submodules"):
            checkout = self.work / "checkout-plugins"
            self.write_plugin("compaction", {"version": "2.0.0"}, where=checkout)
            (checkout / "remote").mkdir()  # an uninitialized submodule: an empty directory
            loader.reset()
            with mock.patch.object(loader, "CHECKOUT", checkout):
                found = {entry.name: entry for entry in plugins.entries(self.root)}
            self.assertEqual((found["compaction"].source, found["compaction"].manifest.version), ("checkout", "2.0.0"))
            self.assertNotIn("remote", found)
            self.assertEqual(found["compaction"].state, "off")  # trusted, but off until plugins.json turns it on
        with self.subTest("precedence across every source"):
            checkout = self.work / "checkout-twin"
            self.write_plugin("twin", {"version": "checkout"}, where=checkout)
            installed = self.write_plugin("twin", {"version": "installed"}, where=self.work / "site")
            loader.reset()
            with mock.patch.object(loader, "CHECKOUT", checkout), mock.patch.object(loader, "_installed", [installed]):
                self.assertEqual(self.entry("twin").source, "installed")
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

    def universe(self, name):
        """A universe tree of its own: a first use converts the whole tree it is called for."""
        return templates.create(self.work / name / ".multiverse", name, workdir=self.work / name)

    def mv2(self, root, *arguments):
        """mv2's own command line, run in this process: (exit code, stdout, stderr)."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", ["mv2", "-u", str(root), *arguments]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_legacy_settings_move_to_the_plugin_and_turn_it_on(self):
        """Converting a pre-plugin settings file, by the first command or by migrate --apply, happens once and is
        said on stderr, nothing turns on or reads defaults before it, the old file's on or off wins over a switch
        plugins.json already has, an agreeing switch is left alone, a not-installed known plugin's file still takes
        the full path, and schema 1 is upgraded to 2; fragile because the old file must override the template, and a
        break silently turns a plugin such as compaction on or off against the user's choice (PR #20 review item 6)."""
        from multiverse.tools import plugins as tool
        (self.root / "tree.json").write_text(json.dumps({"enabled": True, "budget": 5}))
        self.assertFalse(self.entry("tree").on)  # no switch yet, and its settings are not read
        # Never read, and the defaults never stand in for it: no settings until it is converted.
        self.assertEqual(plugins.settings_of(self.root, "tree")[:2],
                         ({}, "tree.json is not converted yet: run mv2 plugins migrate --apply"))
        plan = tool.run(self.root, plugin_action="migrate")
        self.assertEqual([item["kind"] for item in plan["planned"]], ["legacy"])
        self.assertTrue((self.root / "tree.json").exists())
        done = tool.run(self.root, plugin_action="migrate", apply=True)
        self.assertEqual(done["applied"][0]["on"], True)
        self.assertFalse((self.root / "tree.json").exists())
        self.assertEqual(json.loads((self.root / "plugin-settings/tree.json").read_text()), {"schema": 2, "budget": 5})
        self.assertTrue(self.entry("tree").on)
        self.assertTrue(Path(done["applied"][0]["backup"]).is_file())
        self.assertEqual(tool.run(self.root, plugin_action="migrate")["planned"], [])
        with self.subTest("the first command converts once and says so on stderr"):
            root = self.universe("first-command")
            (root / "tree.json").write_text(json.dumps({"enabled": True, "budget": 5}))
            code, out, err = self.mv2(root, "list", "--json")
            self.assertEqual(code, 0, err)
            json.loads(out)  # --json output is untouched
            [line] = [line for line in err.splitlines() if "tree" in line]
            self.assertIn("mv2: tree: converted tree.json to plugin-settings/tree.json, turned it on in plugins.json",
                          line)
            self.assertEqual(json.loads((root / "plugin-settings/tree.json").read_text()), {"schema": 2, "budget": 5})
            self.assertFalse((root / "tree.json").exists())
            backups = sorted((root / "trash").glob("*-plugins-migrate/tree.json"))
            self.assertEqual(len(backups), 1)
            self.assertTrue(json.loads((root / "plugins.json").read_text())["enabled"]["tree"])
            code, out, err = self.mv2(root, "list")
            self.assertNotIn("tree", err)
            self.assertEqual(sorted((root / "trash").glob("*-plugins-migrate/tree.json")), backups)
        with self.subTest("the old file's on or off wins over a switch plugins.json already has"):
            root = self.universe("switch-on")
            self.enable("tree", on=True, root=root)  # a template's plugins.json may name it before the conversion
            (root / "tree.json").write_text(json.dumps({"enabled": False, "budget": 5}))
            [line] = plugins.migrate_on_first_use(root)
            self.assertIn("turned it off in plugins.json", line)
            self.assertFalse(json.loads((root / "plugins.json").read_text())["enabled"]["tree"])
        with self.subTest("a switch that already agrees is left alone and not claimed"):
            root = self.universe("switch-agrees")
            self.enable("tree", on=True, root=root)
            (root / "tree.json").write_text(json.dumps({"enabled": True, "budget": 5}))
            [line] = plugins.migrate_on_first_use(root)
            self.assertIn("converted tree.json to plugin-settings/tree.json", line)
            self.assertNotIn("turned it", line)
            self.assertTrue(json.loads((root / "plugins.json").read_text())["enabled"]["tree"])
        with self.subTest("an old file of a plugin that is not installed still takes the full path"):
            root = self.universe("not-installed")
            (root / "compaction.json").write_text("{}")
            with mock.patch.object(plugins, "_say_not_installed", return_value=["said"]) as said:
                self.assertEqual(plugins.migrate_on_first_use(root)[:1], ["said"])
            said.assert_called_once()
        with self.subTest("an older settings version is upgraded"):
            root = self.universe("schema-1")
            (root / "plugin-settings").mkdir(exist_ok=True)  # the base template ships it
            (root / "plugin-settings/tree.json").write_text(json.dumps({"schema": 1, "budget": 3}))
            self.enable("tree", root=root)
            self.assertIn("run mv2 plugins migrate", self.entry("tree", root).problem)
            tool.run(root, plugin_action="migrate", apply=True)
            self.assertEqual(json.loads((root / "plugin-settings/tree.json").read_text()),
                             {"schema": 2, "budget": 3, "upgraded_from": 1})
            self.assertIn(self.entry("tree", root).state, ("on", "loaded"))


class InstallTest(PluginTestCase):
    def git(self, *args, cwd, check=True):
        import subprocess
        environment = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                       "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(self.home), "PATH": os.environ["PATH"],
                       "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, env=environment,
                              capture_output=True, text=True, check=check)

    def test_a_checkout_submodule_is_listed_and_installed(self):
        """mv2 plugins install adds a submodule allowing the file protocol only for local URLs and only for that
        command, never in .git/config; fragile because git's protocol.file.allow default changed (2.38.1), and
        install runs too rarely for a break to be noticed."""
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
            # compaction is a known optional plugin: on but not installed, with no old file waiting, is a note.
            self.assertIn("mv2 plugins install compaction", plugins.notes(self.root)[0])
            result = plugins.install("compaction")
            self.assertTrue(result["installed"])
            self.assertEqual(self.entry("compaction").source, "checkout")
            with self.assertRaisesRegex(plugins.PluginError, "No plugin named ghost"):
                plugins.install("ghost")
        with mock.patch.object(loader, "CHECKOUT", None), self.assertRaisesRegex(plugins.PluginError, "with pip"):
            plugins.install("compaction")
        self.assertNotIn("protocol", (checkout / ".git" / "config").read_text())  # never written to config
        for url, local in (("/Users/me/GOAT", True), ("../GOAT", True), ("file:///x", True), ("~/GOAT", True),
                           ("https://github.com/o/GOAT.git", False), ("git@github.com:o/GOAT.git", False), (None, False)):
            with self.subTest(url):
                self.assertEqual(plugins._local_url(url), local)
