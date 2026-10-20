"""The plugin core: manifests, discovery, trust, plugins.json, settings, loading and the API."""
import contextlib
import errno
import io
import json
import threading
import time
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import cli, plugins
from multiverse.plugins import __main__ as helper, api, config, loader, manifest, settings, trust
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import doctor, plugins as tool
from multiverse.universe import catalog

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "greeting": {"type": "string", "minLength": 1},
    "limits": {"type": "object", "properties": {"count": {"type": "integer", "minimum": 0, "maximum": 10}}}}}
HARMFUL = ("import os\nopen(os.environ['MARKER'], 'w').write('ran')\n"
           "def register(mv):\n    pass\n")


def run_quietly(call, stdin=""):
    """(exit code, stdout, stderr) of `call` run as a command's main: SystemExit is its exit code."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        try:
            code = call()
        except SystemExit as exit:
            code = exit.code
    code = 0 if code is None else code if isinstance(code, int) else 1
    return code, out.getvalue(), err.getvalue()


class ManifestTest(PluginTestCase):
    def test_manifest_and_discovery_refusals(self):
        """Bad or reserved names, unknown extension points, permissions or harness entries, a schema or extension
        path outside the plugin, unknown fields and bad JSON make a manifest invalid, a newer API reads as update
        mv2 and an enabled absent plugin as missing; fragile because the newer-API check must come before the
        unknown-name checks and the path checks keep a plugin's files inside it."""
        cases = {
            "name": ({"name": "Hello_World"}, "name must be"),
            "reserved": ({"name": "launches"}, "reserved"),
            "api": ({"api": 1}, "api must be"),
            "point": ({"extends": ["teleport"]}, "does not have: teleport"),
            "permission": ({"permissions": ["root"]}, "unknown permissions: root"),
            "commands without cli": ({"commands": {"hello": "x"}}, 'needs "cli"'),
            "field": ({"colour": "red"}, "unknown fields: colour"),
            "escape": ({"settings": {"version": 1, "schema": "../x.json"}}, "inside the plugin"),
            "default": ({"default": "maybe"}, 'default must be'),
            "harness permission": ({"harnesses": {"claude": [{"dir": "c"}]}}, 'needs the permission "harness.claude"'),
            "harness family": ({"permissions": ["harness.claude"], "harnesses": {"emacs": []}},
                               "unknown harness family"),
            "harness version": ({"permissions": ["harness.claude"],
                                 "harnesses": {"claude": [{"dir": "c", "min_version": "2.1"}]}}, "MAJOR.MINOR.PATCH"),
            "harness escape": ({"permissions": ["harness.pi"], "harnesses": {"pi": [{"extension": "/etc/x.ts"}]}},
                               "inside the plugin"),
            "core status item": ({"extends": ["statusline"], "statusline": {"inbox": {}}}, "one mv2 draws itself"),
            "undeclared status item": ({"statusline": {"trees": {}}}, 'needs "statusline" in extends'),
            "a name unknown at its own api": ({"permissions": ["transcript.read"]}, "unknown permissions: transcript"),
        }
        for label, (fields, expected) in cases.items():
            with self.subTest(label):
                directory = self.write_plugin("case", fields)
                with self.assertRaisesRegex(manifest.ManifestError, expected):
                    manifest.load(directory)
        (self.plugins_dir / "broken").mkdir()
        (self.plugins_dir / "broken" / "plugin.json").write_text("{")
        with self.assertRaisesRegex(manifest.ManifestError, "not valid JSON"):
            manifest.load(self.plugins_dir / "broken")
        with self.subTest("harness versions it does not fit"):
            found = manifest.load(self.write_plugin("guard", {
                "extends": ["launch", "statusline"], "permissions": ["harness.claude", "harness.pi", "harness.codex"],
                "harnesses": {"claude": [{"dir": "claude", "min_version": "2.1.290", "needs_bridge": True}],
                              "pi": [{"extension": "pi/extension.ts"}],
                              "codex": [{"min_version": "0.162.1", "max_version": "0.162.1",
                                         "config": {"model_verbosity": "low"}}]},
                "statusline": {"trees": {"claude": True, "tmux": True}}}))
            [claude] = found.harnesses["claude"]
            self.assertEqual((claude.path, claude.min_version, claude.needs_bridge), ("claude", (2, 1, 290), True))
            self.assertIsNone(claude.fits((2, 1, 296)))
            self.assertIn("2.1.290", claude.fits((2, 1, 289)))
            self.assertIsNotNone(claude.fits(None))
            [codex] = found.harnesses["codex"]
            self.assertEqual(codex.fields, {"config": {"model_verbosity": "low"}})
            self.assertIn("0.162.1 or older", codex.fits((0, 162, 2)))
            self.assertEqual(found.statusline, (manifest.Item("trees", True, True),))
        with self.subTest("states in a universe"):
            self.enable("ghost")
            self.write_plugin("future", {"api": "1.999"})
            self.enable("future")
            # Written for a newer API, with names this mv2 does not know yet: incompatible, never invalid.
            self.write_plugin("newer", {"api": "1.999", "permissions": ["transcripts.read", "models.claude"],
                                        "extends": ["transcripts"], "transcripts": {"rows": 10}})
            self.enable("newer")
            (self.plugins_dir / "bad").mkdir()
            (self.plugins_dir / "bad" / "plugin.json").write_text("[]")
            self.write_plugin("typo", {"permissions": ["transcript.read"]})
            states = {found.name: found for found in plugins.entries(self.root)}
            self.assertEqual(states["ghost"].state, "missing")
            have = ".".join(map(str, loader.API_VERSION))
            for name in ("future", "newer"):
                self.assertEqual(states[name].state, "incompatible")
                self.assertIn(f"needs plugin API 1.999; this mv2 has {have}; update mv2", states[name].problem)
            self.assertIn("transcripts.read", states["newer"].manifest.permission_texts())
            self.assertEqual(states["bad"].state, "invalid")
            self.assertEqual(states["typo"].state, "invalid")
        with self.subTest("mv2 plugins new"):
            parent = self.work / "new-plugins"
            parent.mkdir()
            for name in ("core", "plugins", "list"):
                with self.subTest(name), self.assertRaises(ValueError):
                    tool.run(self.root, plugin_action="new", name=name, directory=str(parent))
            self.assertEqual(list(parent.iterdir()), [])
            tool.run(self.root, plugin_action="new", name="fresh", directory=str(parent))
            self.assertTrue((parent / "fresh" / "plugin.json").exists())


class DiscoveryTest(PluginTestCase):
    bundled = True  # typeahead is the bundled plugin a cloned repository's copy would replace

    def test_which_copy_runs_and_whether_it_is_on(self):
        """Path, universe, personal and bundled copies take precedence in that order, an untrusted copy never hides
        another, and only bundled plugins default on; a security boundary, since a cloned repository's
        plugins/NAME or a plugin's own default must never run code the user did not choose."""
        with self.subTest("only bundled plugins default on"):
            self.write_plugin("quiet", {"default": "on"})
            found = self.entry("quiet")
            self.assertEqual((found.state, found.on, found.source), ("off", False, "path"))
            self.enable("quiet")
            self.assertEqual(self.entry("quiet").state, "on")
            self.assertEqual((self.entry("typeahead").source, self.entry("typeahead").on), ("bundled", True))
        with self.subTest("precedence"):
            self.write_plugin("twin", {"version": "1.0.0"})
            universe_copy = self.write_plugin("twin", {"version": "2.0.0"}, where=self.root / "plugins")
            personal = self.write_plugin("twin", {"version": "3.0.0"}, where=self.home / ".multiverse" / "plugins")
            found = self.entry("twin")
            self.assertEqual((found.source, found.manifest.version), ("path", "1.0.0"))
            self.assertEqual(found.shadowed, [("universe", universe_copy), ("personal", personal)])
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": ""}):
                # An untrusted universe copy never hides another source's plugin; trusted, it comes first.
                found = self.entry("twin")
                self.assertEqual((found.source, found.shadowed), ("personal", [("universe", universe_copy)]))
                trust.trust(universe_copy)
                self.assertEqual(self.entry("twin").source, "universe")
        with self.subTest("a universe copy of a bundled plugin"):
            marker = self.work / "marker"
            os.environ["MARKER"] = str(marker)
            copy = self.write_plugin("typeahead", {"version": "9.9.9", "module": "plugin"}, {"plugin.py": HARMFUL},
                                     where=self.root / "plugins")
            found = self.entry("typeahead")
            self.assertEqual(found.source, "bundled")
            self.assertIn("universe", [source for source, _ in found.shadowed])
            item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "typeahead")
            self.assertEqual((item["source"], item["overrides"]), ("bundled", []))  # untrusted: shadowed, no more
            self.assertFalse(marker.exists())
            trust.trust(copy)
            item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "typeahead")
            self.assertEqual(item["source"], "universe")
            self.assertEqual([entry["source"] for entry in item["overrides"]], ["bundled"])

    def test_safe_mode_loads_nothing(self):
        """MV2_PLUGINS=off loads no plugin; a break goes unnoticed because safe mode is used only after a plugin
        has broken mv2, the worst time to find it broken."""
        self.write_plugin("hello", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv): mv.bootstrap.line('hi')\n"})
        self.enable("hello")
        with mock.patch.dict(os.environ, {"MV2_PLUGINS": "off"}):
            self.assertEqual(self.entry("hello").state, "safe-mode")
            self.assertEqual(loader.active(self.root), [])
            self.assertEqual(plugins.bootstrap_lines(self.root), [])
        self.assertEqual(plugins.bootstrap_lines(self.root), ["hi"])


class ConfigTest(PluginTestCase):
    def test_plugins_json_keeps_unknown_keys_and_survives_damage(self):
        """Writing plugins.json keeps keys from newer and older mv2, and an unusable file reads as defaults and is
        never rewritten; a break goes unnoticed because older runners sharing the universe silently turn every
        plugin off or the next write drops the newer fields."""
        path = config.path(self.root)
        with self.subTest("an older mv2's keys"):
            # "keys" is from before plugins lost their dashboard keys: still read, and kept.
            path.write_text(json.dumps({"schema": 1, "theme": "dusk", "keys": {"u": None}, "enabled": {"a": True}}))
            config.set_enabled(self.root, "b", True)
            self.assertEqual(json.loads(path.read_text()),
                             {"schema": 1, "theme": "dusk", "keys": {"u": None}, "enabled": {"a": True, "b": True}})
            self.assertIsNone(config.load(self.root).problem)
        with self.subTest("a newer mv2's keys"):
            self.write_plugin("tool")
            config.set_enabled(self.root, "tool", True)
            path.write_text(json.dumps({**json.loads(path.read_text()), "access": {"tool": "spend"},
                                        "access_ceiling": "observe"}))
            loaded = config.load(self.root)
            self.assertIsNone(loaded.problem)
            self.assertTrue(loaded.is_on("tool"))
            self.assertEqual(plugins.entry(self.root, "tool").state, "on")
            self.assertTrue(any("access, access_ceiling" in note for note in plugins.notes(self.root)))
            config.set_enabled(self.root, "tool", False)  # writing it keeps what a newer mv2 put there
            self.assertEqual(json.loads(path.read_text())["access"], {"tool": "spend"})
        with self.subTest("unusable"):
            for text in ('{"enabled": {"a": "yes"}}', '{"schema": 1, "access": {}, "enabled": {"a": "yes"}}'):
                path.write_text(text)
                found = config.load(self.root)
                self.assertEqual(found.enabled, {})
                self.assertIn("enabled.a must be true or false", found.problem)
                with self.assertRaisesRegex(ValueError, "enabled.a"):
                    config.set_enabled(self.root, "b", True)
                self.assertEqual(path.read_text(), text)
                self.assertIn("plugins.json cannot be used", plugins.problems(self.root)[0])


CHECKED = """
def register(mv):
    def check(values):
        if values.get("level", 1) not in (1, 2):
            raise ValueError("level must be 1 or 2")
    mv.settings.check(check)
"""
SCREENED = """
def register(mv):
    def check(values):
        if values.get("level", 1) not in (1, 2):
            raise ValueError("level must be 1 or 2")
    mv.settings.check(check)
    mv.dashboard.settings(screen="screens:Settings")
    def run(root, **arguments):
        return {"ran": True}
    mv.cli.command("screened", lambda parser: None, run)
"""
LEVELS = {"type": "object", "properties": {"schema": {"type": "integer"}, "level": {"type": "integer"}}}
LEVEL_SETTINGS = {"version": 1, "schema": "settings.schema.json", "defaults": "defaults/settings.json"}


class SettingsTest(PluginTestCase):
    def greeter(self, name, check=None):
        self.write_plugin(name, {"module": "plugin", "settings": {
            "version": 2, "schema": "schema.json", "defaults": "defaults.json", "legacy": f"{name}.json"}},
            {"plugin.py": "def register(mv):\n    pass\n" if check is None else check,
             "schema.json": json.dumps(SCHEMA), "defaults.json": json.dumps({"schema": 2, "greeting": "hello"})})
        return self.entry(name).manifest

    def leveled(self, name, code=CHECKED):
        self.write_plugin(name, {"module": "plugin", "settings": LEVEL_SETTINGS}, {
            "plugin.py": code, "settings.schema.json": json.dumps(LEVELS),
            "defaults/settings.json": json.dumps({"schema": 1, "level": 1})})
        self.enable(name)
        return settings.path(self.root, name)

    def mv2_settings(self, name, *assignments):
        """`mv2 plugins settings NAME --set ...` as a new process runs it: nothing loaded yet."""
        loader.reset()
        return tool.run(self.root, plugin_action="settings", name=name, assignments=assignments)

    def state(self, name):
        loader.reset()
        return next(item for item in plugins.listing(self.root) if item["name"] == name)["state"]

    def test_settings_edit_writes_only_valid_named_changes(self):
        """A settings edit changes only named keys, DELETE removes one, a schema refusal or the plugin's own check
        (plugin on or off) writes nothing, a refused file reads unusable while the plugin keeps its command, a plugin's
        own settings screen is offered where it is off too (or why it can't be), and enabling twice never reinstalls
        defaults; fragile because the check and the screen need the plugin's code even where it is off
        (COMPACTION-2), and a break quietly writes a bad file or says "no schema" on the Plugins screen."""
        with self.subTest("only the named keys"):
            manifest_ = self.greeter("greeter")
            path = settings.path(self.root, "greeter")
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"schema": 2, "limits": {"count": 3}, "greeting": "hey"}')
            settings.edit(self.root, manifest_, {"limits.count": 4})
            self.assertEqual(json.loads(path.read_text()), {"schema": 2, "limits": {"count": 4}, "greeting": "hey"})
            settings.edit(self.root, manifest_, {("limits", "count"): settings.DELETE})
            self.assertEqual(json.loads(path.read_text()), {"schema": 2, "greeting": "hey"})
            with self.assertRaisesRegex(ValueError, "at most 10"):
                settings.edit(self.root, manifest_, {"limits.count": 11})
            with self.assertRaisesRegex(ValueError, "unknown setting 'colour'"):
                settings.edit(self.root, manifest_, {"colour": "red"})
            self.assertEqual(json.loads(path.read_text()), {"schema": 2, "greeting": "hey"})
        with self.subTest("defaults are installed once"):
            self.greeter("fresh")
            result = plugins.set_enabled(self.root, "fresh", True)
            self.assertEqual(result["installed"], [str(settings.path(self.root, "fresh"))])
            self.assertEqual(plugins.set_enabled(self.root, "fresh", True)["installed"], [])
            self.assertEqual(plugins.settings_of(self.root, "fresh")[0], {"schema": 2, "greeting": "hello"})
        with self.subTest("its own check, once loaded"):
            self.greeter("scary", "def register(mv):\n"
                                  "    @mv.settings.check\n"
                                  "    def check(values):\n"
                                  "        if values.get('greeting') == 'boo':\n"
                                  "            raise ValueError('greeting: too scary')\n")
            self.enable("scary")
            loader.load(self.entry("scary"))
            with self.assertRaisesRegex(ValueError, "too scary"):
                plugins.edit_settings(self.root, "scary", {"greeting": "boo"})
            settings.path(self.root, "scary").write_text('{"schema": 2, "greeting": "boo"}')
            found = self.entry("scary")
            self.assertEqual(found.state, "unusable")
            self.assertFalse(found.usable)
            self.assertNotIn("scary", [loaded.name for loaded in loader.active(self.root)])
        with self.subTest("its own check refuses an edit"):
            path = self.leveled("picky")
            self.mv2_settings("picky", "level=2")
            before = path.read_text()
            with self.assertRaisesRegex(ValueError, "level must be 1 or 2"):
                self.mv2_settings("picky", "level=3")
            self.assertEqual(path.read_text(), before)
            self.assertIn(self.state("picky"), ("on", "loaded"))
        with self.subTest("its own check runs where the plugin is off"):
            path = self.leveled("dormant")
            self.enable("dormant", on=False)
            with self.assertRaisesRegex(ValueError, "level must be 1 or 2"):
                self.mv2_settings("dormant", "level=3")
            self.assertFalse(path.exists())
        with self.subTest("a file its check refuses"):
            path = self.leveled("refused")
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"schema": 1, "level": 9}))
            self.assertEqual(self.state("refused"), "unusable")
            loader.reset()
            self.assertEqual(tool.run(self.root, plugin_action="info", name="refused")["state"], "unusable")
            self.assertIn("level must be 1 or 2", self.mv2_settings("refused")["problem"])
            loader.reset()
            self.assertTrue(any("refused" in text and "its settings cannot be used" in text
                                for _, text in doctor.check(self.root)))
            self.mv2_settings("refused", "level=1")  # and it can be fixed from the command line
            self.assertIn(self.state("refused"), ("on", "loaded"))
        with self.subTest("code that cannot run: its schema alone"):
            path = self.leveled("broken", "raise RuntimeError('cannot load')\n")
            written = self.mv2_settings("broken", "level=3")
            self.assertIn("failed to load: RuntimeError: cannot load", written["unchecked"])
            self.assertEqual(json.loads(path.read_text())["level"], 3)
            with self.assertRaises(ValueError):
                self.mv2_settings("broken", "level=x")
        with self.subTest("a refused file keeps its command and screen"):
            self.write_plugin("screened", {"module": "plugin", "extends": ["dashboard.settings", "cli"],
                                           "commands": {"screened": "Show its state"}, "settings": {"version": 1}},
                              {"plugin.py": SCREENED})
            self.enable("screened")
            settings.path(self.root, "screened").write_text('{"schema": 1, "level": 9}')
            loader.reset()  # a fresh process: nothing loaded yet
            item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "screened")
            self.assertEqual(item["settings"]["screen"], "screens:Settings")
            self.assertIn("screened", plugins.commands(self.root))
            loader.reset()
            with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), "screened"]), \
                    contextlib.chdir(self.work):
                code, out, err = run_quietly(cli.main)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out), {"ran": True})
            self.assertIn("plugin screened: its settings cannot be used: level must be 1 or 2", err)
        with self.subTest("off here: its own screen is still offered, or it says why not"):
            settings.path(self.root, "screened").write_text('{"schema": 1, "level": 1}')
            self.enable("screened", on=False)
            loader.reset()  # a fresh dashboard: nothing loaded yet
            item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "screened")
            self.assertEqual((item["state"], item["settings"]["screen"]), ("off", "screens:Settings"))
            loader.reset()
            with mock.patch.dict(os.environ, {"MV2_PLUGINS": "off"}):
                item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "screened")
            self.assertIsNone(item["settings"]["screen"])
            self.assertIn("MV2_PLUGINS=off", item["settings"]["problem"] or "")


class LoadingTest(PluginTestCase):
    def test_bootstrap_lines_are_bounded(self):
        """A plugin's bootstrap lines are bounded in count and length and refused if they hold any line separator;
        a break goes unnoticed because the extra text silently enters every agent's bootstrap, costing context or
        injecting lines into the prompt."""
        self.write_plugin("chatty", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv):\n    for n in range(3): mv.bootstrap.line(f'line {n}')\n"})
        self.enable("chatty")
        self.assertEqual(self.entry("chatty").state, "on")
        self.assertEqual(plugins.bootstrap_lines(self.root), [])
        self.assertIn("at most 2 bootstrap lines", self.entry("chatty").problem)
        self.enable("chatty", on=False)
        for text in ("one\rtwo", "one two", "one\ntwo", "one\x85two", " "):
            with self.subTest(text=text):
                self.write_plugin("split", {"module": "plugin", "extends": ["bootstrap"]},
                                  {"plugin.py": f"def register(mv): mv.bootstrap.line({text!r})\n"})
                self.enable("split")
                loader.reset()
                self.assertEqual(catalog._plugin_lines(self.root), [])
                self.assertIn("one line of text", self.entry("split").problem)
        self.enable("split", on=False)
        self.write_plugin("long", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv):\n    mv.bootstrap.line('a' * 200)\n"
                                        "    mv.bootstrap.line('b' * 101)\n"})
        self.enable("long")
        self.assertEqual(catalog._plugin_lines(self.root), [])
        self.assertIn(f"{api.BOOTSTRAP_CHARACTERS} characters", self.entry("long").problem)


class ApiTest(PluginTestCase):
    def test_api_calls_need_their_declared_permission(self):
        """An API call without its declared permission raises (including agents in a panel's context without
        agents.read), a registration outside extends leaves the plugin out, and an import error leaves only it
        out; a break goes unnoticed because the plugin simply gets access the user never approved."""
        with self.subTest("registrations and imports"):
            self.write_plugin("crash", {"module": "plugin"}, {"plugin.py": "raise RuntimeError('boom')\n"})
            self.write_plugin("sneaky", {"module": "plugin"},
                              {"plugin.py": "def register(mv): mv.bootstrap.line('x')\n"})
            self.write_plugin("good", {"module": "plugin", "extends": ["bootstrap"]},
                              {"plugin.py": "def register(mv): mv.bootstrap.line('fine')\n"})
            for name in ("crash", "sneaky", "good"):
                self.enable(name)
            self.assertEqual([loaded.name for loaded in loader.active(self.root)], ["good"])
            states = {found.name: (found.state, found.problem) for found in plugins.entries(self.root)}
            self.assertEqual(states["crash"], ("failed", "crash failed to load: RuntimeError: boom"))
            self.assertIn('does not list it in "extends"', states["sneaky"][1])
            self.assertTrue(any("crash failed to load" in problem for problem in plugins.problems(self.root)))
            self.assertEqual(plugins.bootstrap_lines(self.root), ["fine"])
        with self.subTest("calls"):
            self.write_plugin("probe", {"module": "plugin"}, {"plugin.py": "def register(mv): pass\n"})
            self.enable("probe")
            universe = api.Plugin(loader.load(self.entry("probe"))).universe(self.root)
            with self.assertRaisesRegex(api.Error, "did not ask for permission 'agents.read'"):
                universe.agents()
            with self.assertRaisesRegex(api.Error, "agents.control"):
                universe.spawn("worker")
            self.assertEqual(universe.name, "test")
            self.assertTrue(any(item["name"] == "coordination" for item in universe.catalog()))
        with self.subTest("a panel's agents"):
            panel = ("def register(mv):\n"
                     "    @mv.dashboard.panel('board', title='Board')\n"
                     "    def board(context):\n"
                     "        return []\n")
            self.write_plugin("boards", {"module": "plugin", "extends": ["dashboard.panel"],
                                         "permissions": ["agents.read"]}, {"plugin.py": panel})
            self.write_plugin("blind", {"module": "plugin", "extends": ["dashboard.panel"]}, {"plugin.py": panel})
            self.enable("boards")
            self.enable("blind")
            rows = [{"id": "a1", "name": "worker", "universe": str(self.root), "live": "alive"}]
            found = {loaded.name: loaded for loaded, _, _ in plugins.dashboard_panels(self.root)}
            self.assertEqual([agent.name for agent in plugins.panel_context(found["boards"], self.root, {}, rows,
                                                                            set()).agents], ["worker"])
            self.assertEqual(plugins.panel_context(found["blind"], self.root, {}, rows, set()).agents, ())

    def test_an_agents_harness_home_is_its_recorded_storage(self):
        """mv.harness_info(..., agent=) gives the home the agent's launches use, from its records (session home,
        else storage), never this process's environment, which only an agent with neither falls back to; a break
        goes unnoticed because tests run every process with one environment, while the dashboard or the host may
        run with another and then read another store."""
        self.write_plugin("reader", {"module": "plugin", "permissions": ["agents.read"]},
                          {"plugin.py": "def register(mv): pass\n"})
        self.enable("reader")
        mv = api.Plugin(loader.load(self.entry("reader")))
        mine = Path(os.environ["CLAUDE_CONFIG_DIR"])  # this process's Claude home
        recorded = self.work / "launch-claude"
        rows = {  # name: (properties, expected home)
            "session-home": ({"native_session_home": str(recorded)}, recorded),
            "storage": ({"native_storage_environment": {"CLAUDE_CONFIG_DIR": str(recorded)}}, recorded),
            "storage-unset": ({"native_storage_environment": {"CLAUDE_CONFIG_DIR": None}},
                              Path(os.environ["HOME"]) / ".claude"),
            "nothing-recorded": ({}, mine),
            "damaged": ({"native_storage_environment": ["not", "a", "record"]}, None)}
        for name, (properties, expected) in rows.items():
            with self.subTest(name):
                self.agent(name, **properties)
                agent = mv.universe(self.root).agent(name)
                self.assertEqual(mv.harness_info(self.root, "claude", agent=agent).home, expected)
        with self.subTest("without an agent: this process's environment, as before"):
            self.assertEqual(mv.harness_info(self.root, "claude").home, mine)
        with self.subTest("an agent of another harness is refused"):
            self.agent("coder", harness="codex")
            with self.assertRaisesRegex(api.Error, "coder runs harness 'codex', not 'claude'"):
                mv.harness_info(self.root, "claude", agent=mv.universe(self.root).agent("coder"))

    def test_a_filesystem_failure_is_an_oserror_too(self):
        """A filesystem failure in an API call (EACCES, ENOSPC) is an api.Error and also the OSError it was, errno
        kept, as state_dir and agent_state_dir raised in 1.9; a break goes unnoticed because tests rarely fill a
        disk, and a 1.9 plugin's except OSError then crashes on the first full one."""
        self.write_plugin("keeper", {"module": "plugin", "permissions": ["agents.read"]},
                          {"plugin.py": "def register(mv): pass\n"})
        self.enable("keeper")
        mv = api.Plugin(loader.load(self.entry("keeper")))
        self.agent("worker")
        worker = mv.universe(self.root).agent("worker")
        for failure, kind in ((PermissionError(errno.EACCES, "Permission denied"), PermissionError),
                              (OSError(errno.ENOSPC, "No space left on device"), OSError)):
            for name, call in (("state_dir", lambda: mv.state_dir(self.root)),
                               ("agent_state_dir", lambda: mv.agent_state_dir(worker)),
                               ("files.ensure_dir", lambda: api.files.ensure_dir(self.work / "elsewhere" / "a"))):
                with self.subTest(name, errno=errno.errorcode[failure.errno]), \
                        mock.patch.object(Path, "mkdir", side_effect=failure):
                    with self.assertRaises(api.Error) as caught:
                        call()
                    self.assertIsInstance(caught.exception, kind)
                    self.assertEqual(caught.exception.errno, failure.errno)
                    self.assertIn(failure.strerror, str(caught.exception))


HELPERS = """
import json, sys

def register(mv):
    mv.helper.action("refuse", lambda arguments: 2)
    mv.helper.action("crash", lambda arguments: 1 / 0)

    @mv.helper.action("status")
    def status(arguments):
        print("status", *arguments)
        return 3

    @mv.helper.action("guard")
    def guard(arguments):
        request = json.load(sys.stdin)
        for n in range(3):
            print(json.dumps({"event": "progress", "n": n}), flush=True)
        return 2 if request.get("refuse") else 0
"""


class HelperExitTest(PluginTestCase):
    def test_helper_exit_codes_and_streams(self):
        """python -m multiverse.plugins run exits 1 with empty stdout when mv2 refuses and 2 only for the action's
        own refusal, with the action's own stdin and stdout; a break goes unnoticed in mv2 because GOAT's Claude
        hook reads these codes, so a stray print or a crash exiting 2 changes what Claude does."""
        self.write_plugin("guardian", {"module": "plugin", "extends": ["helper"]}, {"plugin.py": HELPERS})
        self.write_plugin("broken", {"module": "plugin", "extends": ["helper"]}, {"plugin.py": "raise SystemExit(2)\n"})
        self.enable("guardian")
        self.enable("broken")
        root = str(self.root)
        # One real process: the exit code, stdin and stdout of `python -m` itself.
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        result = subprocess.run([sys.executable, "-m", "multiverse.plugins", "-u", root, "run", "guardian", "guard"],
                                input='{"refuse": true}', env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual([json.loads(line)["n"] for line in result.stdout.splitlines()], [0, 1, 2])
        cases = (
            (("-u", root, "run", "guardian", "refuse"), 2, ""),  # deliberate
            (("-u", root, "run", "guardian", "status", "a", "--b"), 3, "status a --b\n"),
            (("-u", root, "run", "guardian", "guard"), 0, "".join(f'{{"event": "progress", "n": {n}}}\n'
                                                                    for n in range(3))),
            ((), 1, ""), (("run",), 1, ""), (("--nope",), 1, ""),
            (("-u", root, "run", "ghost", "refuse"), 1, ""), (("-u", root, "run", "broken", "refuse"), 1, ""),
            (("-u", str(self.work / "none"), "run", "a", "b"), 1, ""),
            (("-u", root, "run", "guardian", "missing"), 1, ""), (("-u", root, "run", "guardian", "crash"), 1, ""),
        )
        for arguments, code, stdout in cases:
            with self.subTest(arguments):
                loader.reset()  # each a new process
                self.assertEqual(run_quietly(lambda: helper.main(list(arguments)), stdin="{}")[:2], (code, stdout))
        self.assertIn("ZeroDivisionError", (self.root / "state" / "plugins" / "guardian" / "errors.log").read_text())


class GuardedTest(PluginTestCase):
    def test_guarded_calls_are_bounded_contained_and_lock_free(self):
        """loader.guarded(), the wrapper around every call into plugin code, times out, never starts a second
        thread for a busy site, contains SystemExit, records errors without raising and holds no global lock
        during the call; fragile because holding loader._lock deadlocks a plugin call that loads another plugin,
        and a break shows only with a slow plugin, as a frozen dashboard or killed host."""
        self.write_plugin("calls", {"module": "plugin"}, {"plugin.py": "def register(mv): pass\n"})
        self.enable("calls")
        loaded = loader.load(self.entry("calls"))
        with self.subTest("an error is recorded, never raised"):
            self.assertEqual(loader.guard(loaded, "column z", lambda: 1 / 0, root=self.root, default="—"), "—")
            self.assertEqual(loader.errors()["calls"], ["column z: ZeroDivisionError: division by zero"])
            log = (self.root / "state" / "plugins" / "calls" / "errors.log").read_text()
            self.assertIn("ZeroDivisionError", log)
            self.assertEqual(plugins.listing(self.root)[0]["errors"], ["column z: ZeroDivisionError: division by zero"])
        with self.subTest("time, busy sites and keys"):
            release = threading.Event()
            self.addCleanup(release.set)

            def hang():
                release.wait(10)
                return "late"
            before = len(loaded.errors)
            first = loader.guarded(loaded, "column x", hang, root=self.root, timeout=0.1)
            self.assertEqual(first.state, "timeout")
            busy = loader.guarded(loaded, "column x", hang, root=self.root, timeout=0.1)
            self.assertEqual((busy.state, busy.value), ("busy", None))
            other = loader.guarded(loaded, "column y", lambda: 3, root=self.root, timeout=0.1)
            self.assertEqual((other.state, other.value), ("ok", 3))
            keyed = loader.guarded(loaded, "column x", lambda: 4, root=self.root, timeout=0.1, key="row 2")
            self.assertEqual((keyed.state, keyed.value), ("ok", 4))  # a key of its own is not busy
            self.assertEqual(loaded.errors[before:], ["column x: TimeoutError: took longer than 0.1 s"])  # once
            release.set()

        def leave():
            raise SystemExit(3)
        for timeout in (None, 1):
            with self.subTest("SystemExit", timeout=timeout):
                outcome = loader.guarded(loaded, "action", leave, root=self.root, timeout=timeout)
                self.assertEqual(outcome.state, "error")
                self.assertIn("SystemExit", outcome.error)
                self.assertIsNone(loader.guard(loaded, "action", leave, root=self.root, timeout=timeout, default=None))
        with self.subTest("no global lock"):
            held = threading.Event()
            holder = threading.Thread(target=lambda: (loader._lock.acquire(), held.wait(10), loader._lock.release()))
            holder.start()
            self.addCleanup(holder.join, 10)
            self.addCleanup(held.set)
            started = time.monotonic()
            outcome = loader.guarded(loaded, "fact", lambda: 5, root=self.root, timeout=1)
            self.assertEqual((outcome.state, outcome.value), ("ok", 5))
            self.assertLess(time.monotonic() - started, 1)
            held.set()
