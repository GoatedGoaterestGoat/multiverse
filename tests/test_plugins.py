"""The plugin core: manifests, discovery, trust, plugins.json, settings, loading and the API."""
import importlib
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import api, config, loader, manifest, settings, trust
from multiverse.plugins.testing import PluginTestCase

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "greeting": {"type": "string", "minLength": 1},
    "limits": {"type": "object", "properties": {"count": {"type": "integer", "minimum": 0, "maximum": 10}}}}}

HELLO = """
from multiverse.plugins import api

def register(mv):
    mv.cli.command("hello", lambda parser: parser.add_argument("--who", default="world"),
                   lambda root=None, who="world": {"greeting": f"hello {who}"})
    mv.bootstrap.line("Say hello with mv2 hello.")
    mv.doctor.check(lambda universe: ["hello is lonely"] if not universe.agents() else [])
    mv.dashboard.column("hello", "Hi", 6, lambda row: "hi " + row["name"])
"""


class ManifestTest(PluginTestCase):
    def test_reads_a_complete_manifest(self):
        directory = self.write_plugin("hello", {"description": " Greets. ", "module": "plugin",
            "extends": ["cli", "bootstrap"], "permissions": ["agents.read", "models.claude"],
            "commands": {"hello": "Say hello"}, "settings": {"version": 2, "schema": "schema.json"}})
        found = manifest.load(directory)
        self.assertEqual((found.name, found.version, found.api, found.module, found.default),
                         ("hello", "0.1.0", (1, 0), "plugin", "off"))
        self.assertEqual(found.description, "Greets.")
        self.assertEqual(found.commands, {"hello": "Say hello"})
        self.assertEqual(found.settings, manifest.Settings(2, "schema.json"))
        self.assertIn("use claude models (this spends tokens)", found.permission_texts())

    def test_refuses_what_it_cannot_use(self):
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


class DiscoveryTest(PluginTestCase):
    def test_off_unless_enabled_and_only_bundled_plugins_default_on(self):
        self.write_plugin("quiet", {"default": "on"})
        found = self.entry("quiet")
        self.assertEqual((found.state, found.on, found.source), ("off", False, "path"))
        self.enable("quiet")
        self.assertEqual(self.entry("quiet").state, "on")

    def test_precedence_records_what_is_shadowed(self):
        self.write_plugin("twin", {"version": "1.0.0"})
        universe_copy = self.write_plugin("twin", {"version": "2.0.0"}, where=self.root / "plugins")
        personal = self.write_plugin("twin", {"version": "3.0.0"}, where=self.home / ".multiverse" / "plugins")
        found = self.entry("twin")
        self.assertEqual((found.source, found.manifest.version), ("path", "1.0.0"))
        self.assertEqual(found.shadowed, [("universe", universe_copy), ("personal", personal)])
        with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": ""}):
            self.assertEqual(self.entry("twin").source, "universe")

    def test_universe_plugins_need_trust_at_their_content(self):
        directory = self.write_plugin("local", {"module": "plugin"}, {"plugin.py": "def register(mv): pass\n"},
                                      where=self.root / "plugins")
        self.enable("local")
        found = self.entry("local")
        self.assertEqual(found.state, "untrusted")
        self.assertIn("mv2 plugins trust local", found.problem)
        with self.assertRaises(plugins.PluginError):
            loader.load(found)
        trust.trust(directory)
        self.assertEqual(self.entry("local").state, "on")
        self.assertTrue((self.home / ".multiverse" / trust.FILE).is_file())
        (directory / "plugin.py").write_text("def register(mv): print('changed')\n")
        self.assertEqual(self.entry("local").state, "changed")
        (directory / "link").symlink_to(self.home)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            trust.fingerprint(directory)

    def test_missing_invalid_and_incompatible_plugins_say_why(self):
        self.enable("ghost")
        self.write_plugin("future", {"api": "1.999"})
        self.enable("future")
        (self.plugins_dir / "bad").mkdir()
        (self.plugins_dir / "bad" / "plugin.json").write_text("[]")
        states = {found.name: (found.state, found.problem) for found in plugins.entries(self.root)}
        self.assertEqual(states["ghost"][0], "missing")
        self.assertEqual(states["future"][0], "incompatible")
        have = ".".join(map(str, loader.API_VERSION))
        self.assertIn(f"needs plugin API 1.999; this mv2 has {have}; update mv2", states["future"][1])
        self.assertEqual(states["bad"][0], "invalid")

    def test_safe_mode_loads_nothing(self):
        self.write_plugin("hello", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv): mv.bootstrap.line('hi')\n"})
        self.enable("hello")
        with mock.patch.dict(os.environ, {"MV2_PLUGINS": "off"}):
            self.assertEqual(self.entry("hello").state, "safe-mode")
            self.assertEqual(loader.active(self.root), [])
            self.assertEqual(plugins.bootstrap_lines(self.root), [])
        self.assertEqual(plugins.bootstrap_lines(self.root), ["hi"])

    def test_no_inheritance_between_universe_and_subverse(self):
        self.write_plugin("hello")
        child = self.subverse("child")
        self.enable("hello")
        self.assertTrue(self.entry("hello").on)
        self.assertFalse(self.entry("hello", child).on)


class ConfigTest(PluginTestCase):
    def test_enabling_keeps_the_rest_of_the_file(self):
        # "keys" is from before plugins lost their dashboard keys: still read, and kept.
        config.path(self.root).write_text(json.dumps({"schema": 1, "theme": "dusk", "keys": {"u": None},
                                                      "enabled": {"a": True}}, indent=1))
        config.set_enabled(self.root, "b", True)
        data = json.loads(config.path(self.root).read_text())
        self.assertEqual(data, {"schema": 1, "theme": "dusk", "keys": {"u": None}, "enabled": {"a": True, "b": True}})
        self.assertIsNone(config.load(self.root).problem)

    def test_an_unusable_file_reads_as_defaults_and_is_never_rewritten(self):
        config.path(self.root).write_text('{"enabled": {"a": "yes"}}')
        found = config.load(self.root)
        self.assertEqual(found.enabled, {})
        self.assertIn("enabled.a must be true or false", found.problem)
        with self.assertRaisesRegex(ValueError, "enabled.a"):
            config.set_enabled(self.root, "b", True)
        self.assertEqual(config.path(self.root).read_text(), '{"enabled": {"a": "yes"}}')
        self.write_plugin("b")
        self.assertIn("plugins.json cannot be used", plugins.problems(self.root)[0])


class SettingsTest(PluginTestCase):
    def plugin_with_settings(self, check=None):
        code = "def register(mv):\n    pass\n" if check is None else check
        self.write_plugin("greeter", {"module": "plugin", "settings": {
            "version": 2, "schema": "schema.json", "defaults": "defaults.json", "legacy": "greeter.json"}},
            {"plugin.py": code, "schema.json": json.dumps(SCHEMA),
             "defaults.json": json.dumps({"schema": 2, "greeting": "hello"})})
        return self.entry("greeter").manifest

    def test_defaults_file_legacy_and_problems(self):
        manifest_ = self.plugin_with_settings()
        found = settings.read(self.root, manifest_)
        self.assertEqual((found.values, found.source, found.problem), ({"schema": 2, "greeting": "hello"}, "defaults", None))
        (self.root / "greeter.json").write_text('{"old": true}')
        self.assertEqual(settings.read(self.root, manifest_).source, "legacy")
        settings.path(self.root, "greeter").parent.mkdir(exist_ok=True)
        settings.path(self.root, "greeter").write_text('{"schema": 1, "greeting": "x"}')
        self.assertIn("run mv2 plugins migrate", settings.read(self.root, manifest_).problem)
        settings.path(self.root, "greeter").write_text('{"schema": 2, "greeting": "", "extra": 1}')
        self.assertIn("greeter.json: greeting must have at least 1 characters", settings.read(self.root, manifest_).problem)

    def test_edit_changes_only_named_keys_and_refuses_bad_results(self):
        manifest_ = self.plugin_with_settings()
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

    def test_the_plugins_own_check_runs_once_it_is_loaded(self):
        self.plugin_with_settings(
            "def register(mv):\n"
            "    @mv.settings.check\n"
            "    def check(values):\n"
            "        if values.get('greeting') == 'boo':\n"
            "            raise ValueError('greeting: too scary')\n")
        self.enable("greeter")
        loader.load(self.entry("greeter"))
        with self.assertRaisesRegex(ValueError, "too scary"):
            plugins.edit_settings(self.root, "greeter", {"greeting": "boo"})
        settings.path(self.root, "greeter").parent.mkdir(exist_ok=True)
        settings.path(self.root, "greeter").write_text('{"schema": 2, "greeting": "boo"}')
        found = self.entry("greeter")
        self.assertEqual(found.state, "unusable")
        self.assertFalse(found.usable)
        self.assertEqual(loader.active(self.root), [])

    def test_unsupported_schema_keywords_are_refused(self):
        with self.assertRaisesRegex(ValueError, "unsupported keywords: oneOf"):
            settings.check_schema({"oneOf": []})

    def test_enabling_installs_default_settings_once(self):
        self.plugin_with_settings()
        result = plugins.set_enabled(self.root, "greeter", True)
        self.assertEqual(result["installed"], [str(settings.path(self.root, "greeter"))])
        self.assertEqual(plugins.set_enabled(self.root, "greeter", True)["installed"], [])
        self.assertEqual(plugins.settings_of(self.root, "greeter")[0], {"schema": 2, "greeting": "hello"})


class LoadingTest(PluginTestCase):
    def test_register_records_contributions_and_imports_once(self):
        self.write_plugin("hello", {"module": "plugin", "extends": ["cli", "bootstrap", "doctor", "dashboard.column"],
                                    "permissions": ["agents.read"], "commands": {"hello": "Say hello"}},
                          {"plugin.py": HELLO})
        self.enable("hello")
        loaded = loader.load(self.entry("hello"))
        self.assertIs(loader.load(self.entry("hello")), loaded)
        self.assertEqual(self.entry("hello").state, "loaded")
        self.assertEqual(set(loaded.registrations.commands), {"hello"})
        self.assertEqual(loaded.registrations.columns["hello"].value({"name": "w"}), "hi w")
        self.assertTrue(loaded.package.startswith("mv2_plugins.hello_"))
        self.assertEqual(plugins.bootstrap_lines(self.root), ["Say hello with mv2 hello."])
        command = plugins.command(self.root, "hello")
        self.assertEqual(command.run(who="you"), {"greeting": "hello you"})
        universe = api.Universe(self.root, api.Plugin(loaded))
        self.assertEqual(plugins.doctor(self.root, lambda plugin: universe), ["hello: hello is lonely"])

    def test_plugin_modules_import_their_own_package(self):
        self.write_plugin("layered", {"module": "plugin", "extends": ["bootstrap"]}, {
            "plugin.py": "from .parts.words import LINE\ndef register(mv): mv.bootstrap.line(LINE)\n",
            "parts/__init__.py": "", "parts/words.py": "LINE = 'from a subpackage'\n"})
        self.enable("layered")
        loaded = loader.load(self.entry("layered"))
        self.assertEqual(loaded.registrations.bootstrap, ["from a subpackage"])
        self.assertEqual(loaded.resolve("parts.words:LINE"), "from a subpackage")

    def test_a_failing_plugin_is_left_out_and_says_why(self):
        self.write_plugin("crash", {"module": "plugin"}, {"plugin.py": "raise RuntimeError('boom')\n"})
        self.write_plugin("sneaky", {"module": "plugin"}, {"plugin.py": "def register(mv): mv.bootstrap.line('x')\n"})
        self.write_plugin("good", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv): mv.bootstrap.line('fine')\n"})
        for name in ("crash", "sneaky", "good"):
            self.enable(name)
        self.assertEqual([loaded.name for loaded in loader.active(self.root)], ["good"])
        states = {found.name: (found.state, found.problem) for found in plugins.entries(self.root)}
        self.assertEqual(states["crash"], ("failed", "crash failed to load: RuntimeError: boom"))
        self.assertIn('does not list it in "extends"', states["sneaky"][1])
        problems = plugins.problems(self.root)
        self.assertTrue(any("crash failed to load" in problem for problem in problems))
        self.assertEqual(plugins.bootstrap_lines(self.root), ["fine"])

    def test_bootstrap_lines_are_bounded(self):
        self.write_plugin("chatty", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv):\n    for n in range(3): mv.bootstrap.line(f'line {n}')\n"})
        self.enable("chatty")
        self.assertEqual(self.entry("chatty").state, "on")
        self.assertEqual(plugins.bootstrap_lines(self.root), [])
        self.assertIn("at most 2 bootstrap lines", self.entry("chatty").problem)

    def test_command_conflicts_and_reserved_names(self):
        for name in ("one", "two"):
            self.write_plugin(name, {"extends": ["cli"], "commands": {"shared": "x", f"own-{name}": "y", "list": "z"}})
            self.enable(name)
        self.assertEqual(set(plugins.commands(self.root, reserved={"list"})), {"own-one", "own-two"})
        problems = plugins.problems(self.root, reserved={"list"})
        self.assertIn("one and two both declare mv2 shared; neither is used", problems)
        self.assertIn("one, two declares mv2 list, which mv2 itself uses; it is ignored", problems)

    def test_guard_records_errors_without_raising(self):
        self.write_plugin("hello", {"module": "plugin"}, {"plugin.py": "def register(mv): pass\n"})
        self.enable("hello")
        loaded = loader.load(self.entry("hello"))
        self.assertEqual(loader.guard(loaded, "column hello", lambda: 1 / 0, root=self.root, default="—"), "—")
        self.assertEqual(loader.errors(), {"hello": ["column hello: ZeroDivisionError: division by zero"]})
        log = (self.root / "state" / "plugins" / "hello" / "errors.log").read_text()
        self.assertIn("ZeroDivisionError", log)
        self.assertEqual(plugins.listing(self.root)[0]["errors"], ["column hello: ZeroDivisionError: division by zero"])


class ApiTest(PluginTestCase):
    def make(self, permissions=()):
        self.write_plugin("probe", {"module": "plugin", "permissions": list(permissions)},
                          {"plugin.py": "def register(mv): pass\n"})
        self.enable("probe")
        return api.Plugin(loader.load(self.entry("probe")))

    def test_permissions_are_enforced(self):
        mv = self.make()
        universe = mv.universe(self.root)
        with self.assertRaisesRegex(api.Error, "did not ask for permission 'agents.read'"):
            universe.agents()
        with self.assertRaisesRegex(api.Error, "agents.control"):
            universe.spawn("worker")
        self.assertEqual(universe.name, "test")
        self.assertTrue(any(item["name"] == "coordination" for item in universe.catalog()))

    def test_universe_and_state(self):
        mv = self.make(["agents.read"])
        child = self.subverse("child")
        universe = mv.universe(self.root)
        self.assertEqual(universe.agents(), [])
        self.assertEqual(universe.subverses(), [mv.universe(child)])
        self.assertEqual(mv.universe(child).parent, universe)
        self.assertEqual(mv.state_dir(universe), self.root / "state" / "plugins" / "probe")
        mv.log("hello", universe)
        self.assertIn("hello", (self.root / "state" / "plugins" / "probe" / "plugin.log").read_text())
        self.assertEqual(mv.process, "cli")
        with self.assertRaisesRegex(api.Error, "No agent named nobody"):
            universe.agent("nobody")


class ListingTest(PluginTestCase):
    def test_listing_describes_every_plugin(self):
        self.write_plugin("hello", {"description": "Greets", "permissions": ["messages.send"],
                                    "settings": {"version": 1}})
        self.enable("hello")
        [item] = plugins.listing(self.root)
        self.assertEqual({key: item[key] for key in ("name", "on", "state", "source", "version", "description")},
                         {"name": "hello", "on": True, "state": "on", "source": "path", "version": "0.1.0",
                          "description": "Greets"})
        self.assertEqual(item["permissions"], ["send messages to any agent in this universe, as this plugin"])
        self.assertEqual(item["settings"]["values"], {"schema": 1})
        plugins.set_enabled(self.root, "hello", False)
        self.assertFalse(plugins.listing(self.root)[0]["on"])


class InspectorFactsTest(PluginTestCase):
    def test_facts_come_in_plugin_name_order(self):
        for name in ("two", "one"):
            self.write_plugin(name, {"module": "plugin", "extends": ["dashboard.inspector"]}, {
                "plugin.py": f"def register(mv):\n    mv.dashboard.inspector(lambda row: '{name}')\n"})
            self.enable(name)
        self.assertEqual([fact(None) for _, fact in plugins.inspector_facts(self.root)], ["one", "two"])


class ApiOneThreeTest(PluginTestCase):
    def make(self):
        self.write_plugin("probe", {"module": "plugin", "extends": ["helper"], "permissions": ["agents.read"],
                                    "settings": {"version": 1, "schema": "schema.json"}},
                          {"plugin.py": "def register(mv):\n    mv.helper.action('status', lambda arguments: 0)\n"
                                        "    @mv.settings.check\n    def check(values, universe):\n"
                                        "        if values.get('needs') and not (universe / values['needs']).exists():\n"
                                        "            raise ValueError('needs ' + values['needs'])\n",
                           "schema.json": '{"type": "object"}'})
        self.enable("probe")
        return api.Plugin(loader.load(self.entry("probe")))

    def test_agents_sessions_and_harnesses(self):
        mv = self.make()
        directory = self.agent("worker", native_id="1b4e28ba-2fa1-11d2-883f-0016d3cca427",
                               runtime={"native_mode": {"mode": "bridge"}})
        agent = mv.agent_at(directory)
        self.assertEqual(agent.name, "worker")
        session = agent.session()
        self.assertEqual((session.family, session.native_id, session.launch_id, session.native_mode),
                         ("claude", "1b4e28ba-2fa1-11d2-883f-0016d3cca427", "launch-1", {"mode": "bridge"}))
        with mock.patch.dict(os.environ, {"MV2_AGENT_DIR": str(directory)}):
            self.assertEqual(mv.current_agent().id, agent.id)
        info = mv.harness_info(self.root, "claude")
        self.assertEqual((info.family, info.executable, info.bridge), ("claude", "claude", True))
        self.assertEqual(info.home, Path(os.environ["CLAUDE_CONFIG_DIR"]))  # PluginTestCase's temporary one
        with self.assertRaisesRegex(api.Error, "not an agent's directory"):
            mv.agent_at(self.root)

    def test_harness_homes_and_text(self):
        self.assertEqual(api._harness_home("claude", {"CLAUDE_CONFIG_DIR": "/c"}), Path("/c"))
        self.assertEqual(api._harness_home("claude", {"HOME": "/h"}), Path("/h/.claude"))
        self.assertEqual(api._harness_home("codex", {"CODEX_HOME": "/x"}), Path("/x"))
        self.assertEqual(api._harness_home("pi", {}), None)
        self.assertEqual(api._harness_home("pi", {"PI_CODING_AGENT_DIR": "/p"}), Path("/p"))
        self.assertEqual(api._harness_home("", {}), None)
        self.assertEqual((api.text.tokens(82400), api.text.tokens(8200), api.text.clean("a\x1bb")), ("82k", "8.2k", "a b"))

    def test_files_helper_command_and_a_check_that_sees_the_universe(self):
        mv = self.make()
        path = self.work / "state.json"
        api.files.write_json(path, {"a": 1})
        self.assertEqual(api.files.read_json(path), {"a": 1})
        with api.files.lock(self.work / "x.lock"):
            pass
        self.assertIn("T", api.files.now())
        self.assertEqual(mv.helper.command("status", "--fast", universe=self.root)[-6:],
                         ["-u", str(self.root), "run", "probe", "status", "--fast"])
        with self.assertRaisesRegex(ValueError, "needs marker"):
            plugins.edit_settings(self.root, "probe", {"needs": "marker"})
        (self.root / "marker").write_text("")
        plugins.edit_settings(self.root, "probe", {"needs": "marker"})

    def test_testing_helpers(self):
        self.executable("fake-claude", "echo 2.1.296")
        import subprocess
        self.assertEqual(subprocess.run(["fake-claude"], capture_output=True, text=True).stdout.strip(), "2.1.296")
        path = self.claude_transcript("abc", [{"type": "user"}])
        self.assertTrue(path.is_relative_to(self.claude_config))

    def test_a_helper_owns_stdin_stdout_and_its_exit_code(self):
        import subprocess, sys as system
        self.write_plugin("pipe", {"module": "plugin", "extends": ["helper"]}, {"plugin.py": (
            "import json, sys\n"
            "def register(mv):\n"
            "    @mv.helper.action('guard')\n"
            "    def guard(arguments):\n"
            "        request = json.load(sys.stdin)\n"
            "        for n in range(3):\n"
            "            print(json.dumps({'event': 'progress', 'n': n}), flush=True)\n"
            "        return 2 if request.get('refuse') else 0\n")})
        self.enable("pipe")
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                       "MV2_UNIVERSE": str(self.root)}
        result = subprocess.run([system.executable, "-m", "multiverse.plugins", "run", "pipe", "guard"],
                                input='{"refuse": true}', env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual([json.loads(line)["n"] for line in result.stdout.splitlines()], [0, 1, 2])


class NoDashboardKeysTest(PluginTestCase):
    def test_plugins_get_no_dashboard_keys(self):
        """API 1.6: a plugin's way into the dashboard is its settings (under P) and the command palette."""
        self.write_plugin("old", {"module": "plugin", "extends": ["dashboard.key"]},
                          {"plugin.py": "def register(mv): pass\n"})
        self.assertIn("extension points this mv2 does not have: dashboard.key", self.entry("old").problem or "")
        self.write_plugin("caller", {"module": "plugin", "extends": ["dashboard.screen"]},
                          {"plugin.py": "def register(mv):\n    mv.dashboard.key('c', 'C', screen='ui:S')\n"})
        self.enable("caller")
        self.assertIn("has no attribute 'key'", str(self._load("caller")))
        self.assertFalse(hasattr(api.SettingsAccess, "screen"))

    def _load(self, name):
        try:
            loader.load(self.entry(name))
        except plugins.PluginError as error:
            return error


class TestingHelpersTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.directory = self.write_plugin("deep", {"module": "plugin"}, {
            "plugin.py": "from .engine import store\ndef register(mv): pass\n",
            "engine/__init__.py": "",
            "engine/store.py": "from .model import VALUE\ndef later():\n    from .model import VALUE as again\n    return again\n",
            "engine/model.py": "VALUE = 42\n"})

    def test_plugin_module_needs_no_universe_and_is_the_one_mv2_loads(self):
        from multiverse.plugins.testing import plugin_module
        store = plugin_module(self.directory, "engine.store")  # the plugin is not on anywhere
        self.assertEqual((store.VALUE, store.later()), (42, 42))
        self.assertIs(plugin_module(self.directory, "engine.store"), store)  # one copy per process
        self.enable("deep")
        self.assertIs(importlib.import_module(f"{loader.load(self.entry('deep')).package}.engine.store"), store)
        with self.assertRaises(FileNotFoundError):
            plugin_module(self.work / "nowhere", "engine.store")

    def test_module_of_the_plugin_under_test(self):
        self.plugin = self.directory
        self.enable("deep")
        self.assertEqual(self.module("engine.model").VALUE, 42)
