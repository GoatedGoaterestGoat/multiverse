"""While an old settings file waits to be converted, nothing puts defaults in front of it (COMPACTION-7)."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import loader, manifest, settings as settings_files
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write
from multiverse.tools import doctor, plugins as tool, refresh
from multiverse.universe import templates

SCHEMA = {"type": "object", "properties": {"schema": {"type": "integer"}, "level": {"type": "integer"}}}
SETTINGS = {"version": 1, "schema": "settings.schema.json", "defaults": "defaults/settings.json"}


class WaitingOldFileTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.directory = self.write_plugin(
            "veteran", {"module": "plugin", "settings": {**SETTINGS, "legacy": "veteran.json"}}, {
                "plugin.py": "def register(mv):\n    pass\n", "settings.schema.json": json.dumps(SCHEMA),
                "defaults/settings.json": json.dumps({"schema": 1, "level": 1})})
        self.enable("veteran")
        (self.root / "veteran.json").write_text('{"level": tru')  # cannot be converted as it is

    def listed(self):
        return next(item for item in plugins.listing(self.root, wait=None) if item["name"] == "veteran")

    def test_nothing_puts_defaults_in_front_of_a_waiting_old_file(self):
        """While an unconverted settings file waits, the listing, info and settings say unusable, doctor reports it
        once, a settings edit refuses, no defaults file is written, refresh defers the template's plugin-settings
        and plugins.json until the universe writes its own, after migrate --apply the plugin is on, and an old file
        found again after its conversion is reported by doctor as ignored while the plugin keeps its converted
        settings; fragile because every writer must check for the old file, and a break runs the plugin on template
        defaults instead of the user's settings (COMPACTION-7), or says nothing of an old file nothing reads (qa
        spec gap 2)."""
        for when in ("before its conversion is tried", "after it failed"):
            with self.subTest(when):
                loader.reset()  # each view was a command of its own
                self.assertEqual(self.listed()["state"], "unusable")
                info = tool.run(self.root, plugin_action="info", name="veteran")
                self.assertEqual(info["state"], "unusable")
                self.assertIn("off until veteran.json is converted: run mv2 plugins migrate --apply", info["problem"])
                shown = tool.run(self.root, plugin_action="settings", name="veteran")
                self.assertIn("veteran.json is not converted yet: run mv2 plugins migrate --apply", shown["problem"])
                self.assertFalse(settings_files.path(self.root, "veteran").exists())  # no defaults written
                plugins.migrate_on_first_use(self.root)  # what mv2 doctor runs first
                loader.reset()
                found = doctor.run(self.root)
                self.assertFalse(found["ok"])
                said = [line for line in found["problems"] + found.get("notes", []) if "veteran.json" in line]
                self.assertEqual(len(said), 1, found)  # said once
        self.assertTrue((self.root / "veteran.json").is_file())
        with self.subTest("a settings edit"):
            with self.assertRaisesRegex(ValueError, "veteran.json is still here and not converted"):
                settings_files.edit(self.root, manifest.load(self.directory), {"level": 2})
            self.assertFalse(settings_files.path(self.root, "veteran").exists())
        with self.subTest("refresh"):
            self.refresh_waits_for_the_old_files()
        with self.subTest("once converted it is on"):
            (self.root / "veteran.json").write_text(json.dumps({"level": 2}))
            loader.reset()
            self.write_plugin("veteran", {"module": "plugin", "settings": {**SETTINGS, "legacy": "veteran.json"}}, {
                "plugin.py": ("def register(mv):\n    @mv.settings.migrate\n    def migrate(data, source):\n"
                              "        return {'schema': 1, 'level': data['level']}\n")})
            self.assertNotEqual(tool.run(self.root, plugin_action="migrate", apply=True).get("ok"), False)
            loader.reset()
            self.assertIn(self.listed()["state"], ("on", "loaded"))
        with self.subTest("an old file found again after its conversion"):
            converted = settings_files.path(self.root, "veteran").read_text()
            self.assertEqual(doctor.run(self.root).get("notes", []), [])
            (settings_files.path(self.root, "compaction")).write_text('{"schema": 2}')  # a known plugin's, too
            for legacy in ("veteran.json", "compaction.json"):  # back from an old branch, say
                (self.root / legacy).write_text(json.dumps({"level": 1}))
            plugins.migrate_on_first_use(self.root)  # what mv2 doctor runs first
            loader.reset()
            found = doctor.run(self.root)
            self.assertTrue(found["ok"], found)
            for legacy in ("veteran.json", "compaction.json"):
                self.assertEqual(len([note for note in found.get("notes", [])
                                      if legacy in note and "ignored" in note]), 1, found)
            self.assertEqual(settings_files.path(self.root, "veteran").read_text(), converted)
            self.assertIn(self.listed()["state"], ("on", "loaded"))

    def refresh_waits_for_the_old_files(self):
        """A universe made before plugins, from a template that gains plugin settings and a plugins.json later."""
        template = self.work / "template"
        write(template / "universe.json", {"schema": 1, "name": "template", "workspace": ".."})
        (template / "bootstrap.md").write_text("Bootstrap\n")
        root = templates.create(self.work / "old" / ".multiverse", "old", str(template), self.work / "old")

        def plan(**options):
            return {change["path"]: change for change in refresh.run(root, **options)["universes"][0]["changes"]}
        (root / "veteran.json").write_text('{"level": 2}')
        # A known plugin's old file waits too (core knows compaction's, installed or not).
        (root / "compaction.json").write_text('{"enabled": false}\n')
        for path, text in (("plugin-settings/veteran.json", '{"schema": 1}\n'),
                           ("plugin-settings/compaction.json", '{"schema": 2}\n'),
                           ("plugins.json", '{"schema": 1, "enabled": {"compaction": true}}\n'),
                           ("plugin-settings/absent.json", '{"schema": 2}\n')):
            (template / path).parent.mkdir(parents=True, exist_ok=True)
            (template / path).write_text(text)
        changes = plan()
        self.assertEqual(changes["plugin-settings/veteran.json"]["action"], "deferred")
        self.assertIn("run mv2 plugins migrate", changes["plugin-settings/veteran.json"]["reason"])
        for path in ("plugin-settings/compaction.json", "plugins.json"):
            self.assertEqual(changes[path]["action"], "deferred", changes)
            self.assertIn("compaction.json", changes[path]["reason"])
            self.assertIn("is still here and not converted", changes[path]["reason"])
        # Without an old file there is nothing to protect: a plugin that is not here is no reason to hold back.
        self.assertEqual(changes["plugin-settings/absent.json"]["action"], "add")
        refresh.run(root, apply=True)
        for path in ("plugin-settings/veteran.json", "plugin-settings/compaction.json", "plugins.json"):
            self.assertFalse((root / path).exists(), path)  # nothing in front of the real settings
        self.assertEqual(plan(take=["plugin-settings/compaction.json"])["plugin-settings/compaction.json"]["action"],
                         "add")
        # Converted: the universe wrote its own, with no install record: kept as its own from then on.
        (root / "veteran.json").unlink()
        (root / "compaction.json").unlink()
        (root / "plugin-settings" / "veteran.json").write_text('{"schema": 1, "level": 2}\n')
        (root / "plugin-settings" / "compaction.json").write_text('{"schema": 2, "budget": 50000}\n')
        (root / "plugins.json").write_text('{"schema": 1, "enabled": {"compaction": false}}\n')
        changes = plan()
        for path in ("plugin-settings/veteran.json", "plugin-settings/compaction.json", "plugins.json"):
            self.assertEqual(changes[path]["action"], "local", path)
        for path in ("plugin-settings/veteran.json", "plugins.json"):  # take still overrides
            self.assertEqual(plan(take=[path])[path]["action"], "update", path)
