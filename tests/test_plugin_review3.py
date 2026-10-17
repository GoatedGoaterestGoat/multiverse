"""PR #20 review, the remaining should-fix items: each reproduced here first."""
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import api, config, events, host, loader, settings, trust
from multiverse.plugins.testing import PluginTestCase


class OverridesTest(PluginTestCase):
    bundled = True  # typeahead is the bundled plugin a cloned repository's copy would replace

    def test_a_trusted_universe_copy_says_it_overrides_and_an_untrusted_one_is_only_shadowed(self):
        from multiverse.tools import plugins as tool
        copy = self.write_plugin("typeahead", {"version": "9.9.9"}, where=self.root / "plugins")
        item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "typeahead")
        self.assertEqual((item["source"], item["overrides"]), ("bundled", []))  # untrusted: shadowed, nothing more
        trust.trust(copy)
        item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "typeahead")
        self.assertEqual(item["source"], "universe")
        self.assertEqual([entry["source"] for entry in item["overrides"]], ["bundled"])
        listed = tool.text("list", tool.run(self.root, plugin_action="list"))
        self.assertIn("overrides the bundled typeahead", listed)
        self.assertIn("overrides the bundled typeahead", tool.text("info", tool.run(self.root, plugin_action="info",
                                                                                     name="typeahead")))


SCREENED = """
def register(mv):
    def check(values):
        if values.get("level", 1) not in (1, 2):
            raise ValueError("level must be 1 or 2")
    mv.settings.check(check)
    mv.dashboard.settings(screen="screens:Settings")
    def run(root, **arguments):
        return {"ran": True}
    mv.cli.command("picky", lambda parser: None, run)
"""


class UnusableSettingsTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("picky", {"module": "plugin", "extends": ["dashboard.settings", "cli"],
                                    "commands": {"picky": "Show picky's state"}, "settings": {"version": 1}},
                          {"plugin.py": SCREENED})
        self.enable("picky")
        (self.root / "plugin-settings").mkdir(exist_ok=True)
        (self.root / "plugin-settings" / "picky.json").write_text('{"schema": 1, "level": 9}')
        loader.reset()  # a fresh process: nothing loaded yet

    def test_its_own_settings_screen_is_offered_even_before_it_is_loaded(self):
        item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "picky")
        self.assertEqual(item["settings"]["screen"], "screens:Settings")

    def test_it_keeps_its_command_and_says_why(self):
        self.assertIn("picky", plugins.commands(self.root))
        import subprocess
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        result = subprocess.run([sys.executable, "-m", "multiverse.cli", "-u", str(self.root), "picky"],
                                env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("picky: its settings cannot be used: level must be 1 or 2", result.stderr)


class ReservedNameTest(PluginTestCase):
    def test_new_refuses_reserved_and_core_names(self):
        from multiverse.tools import plugins as tool
        parent = self.work / "new-plugins"
        parent.mkdir()
        for name in ("core", "plugins", "list"):
            with self.subTest(name), self.assertRaises(ValueError):
                tool.run(self.root, plugin_action="new", name=name, directory=str(parent))
        self.assertEqual(list(parent.iterdir()), [])
        tool.run(self.root, plugin_action="new", name="fresh", directory=str(parent))
        self.assertTrue((parent / "fresh" / "plugin.json").exists())


class MalformedSchemaTest(PluginTestCase):
    def test_a_malformed_schema_makes_only_that_plugin_unusable(self):
        self.write_plugin("odd", {"settings": {"version": 1, "schema": "schema.json"}},
                          {"schema.json": json.dumps({"type": "object", "properties": 3}),
                           "defaults.json": "{}"})
        self.write_plugin("rex", {"settings": {"version": 1, "schema": "schema.json"}},
                          {"schema.json": json.dumps({"type": "object", "properties": {"name": {"type": "string",
                                                                                                "pattern": "(["}}})})
        self.write_plugin("fine", {})
        for name in ("odd", "rex", "fine"):
            self.enable(name)
        states = {entry["name"]: entry for entry in plugins.listing(self.root)}  # never raises
        self.assertIn("schema", states["odd"]["problem"])
        self.assertIn("pattern", states["rex"]["problem"])
        self.assertIsNone(states["fine"]["problem"])
        with self.assertRaisesRegex(ValueError, "pattern"):
            plugins.edit_settings(self.root, "rex", {"name": "x"})


class JournalReadTest(PluginTestCase):
    def test_nothing_new_reads_no_segment(self):
        self.write_plugin("listener", {"extends": ["events"], "events": ["*"]})
        self.enable("listener")
        for index in range(3):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": index})
        found, _ = events.read(self.root, 0)
        self.assertEqual(len(found), 3)
        real = Path.open
        opened = []

        def counting(path, *arguments, **options):
            if path.suffix == ".jsonl":
                opened.append(path)
            return real(path, *arguments, **options)
        with mock.patch.object(Path, "open", counting):
            self.assertEqual(events.read(self.root, 3), ([], None))
        self.assertEqual(opened, [])


class LogRotationTest(PluginTestCase):
    def test_plugin_and_events_logs_rotate(self):
        self.write_plugin("chatty", {"module": "plugin"}, {"plugin.py": "def register(mv): pass\n"})
        self.enable("chatty")
        mv = api.Plugin(loader.load(self.entry("chatty")))
        with mock.patch.object(loader, "LOG_BYTES", 500):
            for index in range(40):
                mv.log("x" * 40, self.root)
                events._log(self.root, "y" * 40)
                host._log(self.root, "z" * 40)
        for log in (self.root / "state" / "plugins" / "chatty" / "plugin.log",
                    self.root / "state" / "plugins" / "events.log", host.host_directory(self.root) / "host.log"):
            self.assertTrue(log.with_name(log.name + ".1").exists(), log)
            self.assertLess(log.stat().st_size, 1000)


class CursorAnchorTest(PluginTestCase):
    def test_events_after_enabling_reach_a_host_that_starts_later(self):
        self.write_plugin("early", {"extends": ["events"], "events": ["*"]})
        self.enable("early")  # someone else subscribes, so the journal is written
        for index in range(3):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": index})
        self.write_plugin("late", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                          {"plugin.py": "SEEN = []\ndef register(mv):\n    mv.events.on('agent.status', lambda e: SEEN.append(e.seq))\n"})
        plugins.set_enabled(self.root, "late", True)  # mv2 plugins enable: the cursor starts here
        for index in range(2):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": index})  # the host is down
        loaded = loader.load(self.entry("late"))
        host._deliver(loaded, api.Plugin(loaded), self.root, "late")  # the host starts
        self.assertEqual(loaded.module.SEEN, [4, 5])
