"""Plugins in the command line, the catalog, the bootstrap and doctor; `mv2 plugins` itself."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import cli
from multiverse.plugins import trust
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import doctor
from multiverse.universe import catalog

PROJECT = Path(__file__).resolve().parents[1]

HELLO = """
from pathlib import Path
import os
Path(os.environ["HELLO_MARKER"]).write_text("imported")

def register(mv):
    def configure(parser):
        parser.add_argument("--who", default="world")
    def run(root=None, who="world"):
        return {"greeting": f"hello {who}", "universe": str(root)}
    def cli_run(root=None, who="world"):
        return f"hello {who} from {root.name if root.name != '.multiverse' else 'root'}"
    mv.cli.command("hello", configure, run, cli_run)
    mv.bootstrap.line("Greet people with mv2 hello.")
    @mv.doctor.check
    def lonely(universe):
        return ["nobody to greet"]
"""


class PluginCliTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.marker = self.work / "imported"
        os.environ["HELLO_MARKER"] = str(self.marker)
        self.write_plugin("hello", {"module": "plugin", "extends": ["cli", "bootstrap", "doctor", "catalog"],
                                    "commands": {"hello": "Say hello"}},
                          {"plugin.py": HELLO,
                           "skills/greeting/entry.json": json.dumps({"description": "How to greet"}),
                           "skills/greeting/SKILL.md": "Greet warmly.",
                           "workflows/review/entry.json": json.dumps({"description": "A plugin review"}),
                           "workflows/review/prompt.md": "Review like a plugin."})

    def mv2(self, *args, code=0):
        environment = {**os.environ, "PYTHONPATH": str(PROJECT / "src")}
        result = subprocess.run([sys.executable, str(PROJECT / "bin" / "mv2"), *args], env=environment,
                                text=True, capture_output=True, timeout=60, cwd=self.work)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout

    def test_nothing_changes_while_no_plugin_is_on(self):
        own = catalog._own(self.root)
        self.assertEqual(catalog.catalog(self.root), own)
        properties = {"universe_id": "u", "name": "worker", "id": "a"}
        text = catalog.bootstrap(self.root, properties)
        self.assertTrue(text.endswith(f'Your state: {self.root / "state" / "agents" / "a"}\n'))
        self.assertEqual(doctor.check(self.root), [])
        self.assertFalse(self.marker.exists())
        self.assertNotIn("hello", self.mv2("-u", str(self.root), "--help"))

    def test_command_help_lists_it_without_importing_plugin_code(self):
        self.enable("hello")
        listing = self.mv2("-u", str(self.root), "--help")
        self.assertIn("Say hello (plugin hello)", listing)
        self.assertFalse(self.marker.exists())
        probe = subprocess.run(
            [sys.executable, "-c", "import sys; from multiverse import cli; cli.parser(['-u', sys.argv[1]]);"
             "print('textual' in sys.modules, any(m.startswith('mv2_plugins') for m in sys.modules))", str(self.root)],
            env={**os.environ, "PYTHONPATH": str(PROJECT / "src")}, text=True, capture_output=True, timeout=60)
        self.assertEqual(probe.stdout.split(), ["False", "False"], probe.stderr)
        self.assertEqual(self.mv2("-u", str(self.root), "hello", "--who", "you").strip(), "hello you from root")
        self.assertTrue(self.marker.exists())

    def test_the_explicit_universe_decides_never_only_the_environment(self):
        child = self.subverse("child")
        self.enable("hello", root=child)
        os.environ["MV2_UNIVERSE"] = str(self.root)
        self.assertEqual(self.mv2("-u", str(child), "hello").strip(), "hello world from child")
        self.mv2("hello", code=2)
        os.environ["MV2_UNIVERSE"] = str(child)
        self.assertEqual(self.mv2("hello").strip(), "hello world from child")

    def test_core_commands_and_aliases_stay_core(self):
        self.write_plugin("thief", {"extends": ["cli"], "commands": {"list": "steal", "status": "steal"}})
        self.enable("thief")
        parser = cli.parser(["-u", str(self.root), "list"])
        self.assertEqual(cli.tool("list").__name__, "multiverse.tools.list_agents")
        self.assertEqual(parser.parse_args(["list"]).action, "list")
        self.assertIn("thief declares mv2 list, which mv2 itself uses", self.mv2("-u", str(self.root), "plugins"))

    def test_catalog_entries_shadowing_and_qualified_lookup(self):
        self.enable("hello")
        entries = catalog.catalog(self.root)
        plugin_entries = [entry for entry in entries if entry.get("plugin")]
        # The universe's own workflows/review shadows the plugin's; skills/greeting is the plugin's alone.
        self.assertEqual([(entry["kind"], entry["name"]) for entry in plugin_entries], [("skills", "greeting")])
        self.assertEqual(catalog.command(self.root, "greeting"), "Greet warmly.")
        self.assertNotIn("plugin", catalog.command(self.root, "workflows/review"))
        self.assertEqual(catalog.command(self.root, "workflows/review@hello"), "Review like a plugin.")
        self.assertEqual(catalog.command(self.root, "review@hello"), "Review like a plugin.")
        with self.assertRaisesRegex(ValueError, "Expected one exact match for 'review@nobody'"):
            catalog.command(self.root, "review@nobody")
        self.write_plugin("other", {"extends": ["catalog"]}, {
            "skills/greeting/entry.json": "{}", "skills/greeting/SKILL.md": "Other greeting."})
        self.enable("other")
        with self.assertRaisesRegex(ValueError, "found 2"):
            catalog.command(self.root, "greeting")
        self.assertEqual(catalog.command(self.root, "greeting@other"), "Other greeting.")
        listed = json.loads(self.mv2("-u", str(self.root), "catalog", "--json"))
        self.assertEqual(sum(1 for entry in listed if entry["name"] == "greeting"), 2)

    def test_bootstrap_lines_follow_identity(self):
        self.enable("hello")
        text = catalog.bootstrap(self.root, {"universe_id": "u", "name": "worker", "id": "a"})
        self.assertTrue(text.endswith(f'Your state: {self.root / "state" / "agents" / "a"}\n\nGreet people with mv2 hello.\n'))

    def test_doctor_reports_plugins_and_survives_their_failures(self):
        self.enable("hello")
        self.write_plugin("broken", {"module": "plugin", "extends": ["doctor"]}, {"plugin.py": (
            "def register(mv):\n    mv.doctor.check(lambda universe: 1 / 0)\n")})
        self.enable("broken")
        self.enable("ghost")
        problems = [text for _, text in doctor.check(self.root)]
        self.assertIn("Plugin ghost is on in " + str(self.root / "plugins.json") + " but is not installed", problems)
        self.assertIn("Plugin hello: nobody to greet", problems)
        self.assertIn("Plugin broken: its doctor check failed: ZeroDivisionError: division by zero", problems)
        result = doctor.run(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["problems"]), 3)


class PluginsCommandTest(PluginTestCase):
    def mv2(self, *args, code=0):
        environment = {**os.environ, "PYTHONPATH": str(PROJECT / "src")}
        result = subprocess.run([sys.executable, str(PROJECT / "bin" / "mv2"), "-u", str(self.root), *args],
                                env=environment, text=True, capture_output=True, timeout=60, cwd=self.work)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout

    def test_list_enable_info_settings_disable(self):
        self.write_plugin("greeter", {"description": "Greets", "permissions": ["messages.send"],
                                      "settings": {"version": 1, "defaults": "defaults.json"}},
                          {"defaults.json": json.dumps({"schema": 1, "greeting": "hi"})})
        self.assertRegex(self.mv2("plugins"), r"(?m)^greeter +off ")  # beside the bundled ones
        enabled = self.mv2("plugins", "enable", "greeter")
        self.assertIn("Turned greeter on", enabled)
        self.assertIn("- send messages to any agent in this universe, as this plugin", enabled)
        self.assertIn("Installed its default settings: " + str(self.root / "plugin-settings" / "greeter.json"), enabled)
        self.assertRegex(self.mv2("plugins", "list"), r"(?m)^greeter +on ")
        self.assertIn("not a sandbox", self.mv2("plugins", "info", "greeter"))
        changed = self.mv2("plugins", "settings", "greeter", "--set", "greeting=hello there", "--set", "count=3")
        self.assertIn("Changed: count, greeting", changed)
        data = json.loads((self.root / "plugin-settings" / "greeter.json").read_text())
        self.assertEqual(data, {"schema": 1, "greeting": "hello there", "count": 3})
        self.mv2("plugins", "settings", "greeter", "--unset", "count")
        self.assertNotIn("count", json.loads((self.root / "plugin-settings" / "greeter.json").read_text()))
        self.assertIn("Turned greeter off", self.mv2("plugins", "disable", "greeter"))
        self.assertEqual(json.loads((self.root / "plugins.json").read_text())["enabled"], {"greeter": False})
        listed = json.loads(self.mv2("plugins", "--json"))
        self.assertIn("greeter", [item["name"] for item in listed["plugins"]])

    def test_trust_and_check(self):
        directory = self.write_plugin("local", {"module": "plugin", "extends": ["bootstrap"]},
                                      {"plugin.py": "def register(mv): mv.bootstrap.line('local plugin')\n"},
                                      where=self.root / "plugins")
        self.mv2("plugins", "enable", "local", code=1)
        self.assertIn("Trusted local", self.mv2("plugins", "trust", "local"))
        self.assertIn("Turned local on", self.mv2("plugins", "enable", "local"))
        self.assertEqual(trust.state(directory), "trusted")
        self.assertIn("ready.", self.mv2("plugins", "check", str(directory)))
        broken = self.write_plugin("broken", {"module": "plugin", "extends": ["cli"], "commands": {"go": "Go"}},
                                   {"plugin.py": "def register(mv): pass\n"})
        result = self.mv2("plugins", "check", str(broken), code=1)
        self.assertIn("declares commands it does not register: go", result)
        self.assertIn("local is no longer trusted", self.mv2("plugins", "untrust", "local"))
        self.assertEqual(trust.state(directory), "untrusted")


ECHO = """
def register(mv):
    def configure(parser):
        parser.add_argument("--universe", "-u", dest="named")
        parser.add_argument("words", nargs="*")
    def run(root=None, named=None, words=()):
        return {"root": root.parent.name if root.name == ".multiverse" else root.name, "named": named, "words": words}
    mv.cli.command("echo", configure, run)
    def bad(parser):
        raise RuntimeError("bad options")
    mv.cli.command("bad", bad, run)
    mv.cli.command("crash", lambda parser: None, lambda root=None: 1 / 0)
    mv.cli.command("refuse", lambda parser: None, lambda root=None: (_ for _ in ()).throw(ValueError("not today")))
"""


class SelectionTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("echo", {"module": "plugin", "extends": ["cli"], "commands": {
            "echo": "Echo", "bad": "Bad options", "crash": "Crash", "refuse": "Refuse"}}, {"plugin.py": ECHO})

    def mv2(self, *args, code=0):
        environment = {**os.environ, "PYTHONPATH": str(PROJECT / "src")}
        result = subprocess.run([sys.executable, str(PROJECT / "bin" / "mv2"), *args], env=environment,
                                text=True, capture_output=True, timeout=60, cwd=self.work)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_only_global_options_before_the_action_select_the_universe(self):
        self.assertEqual(cli._selection(["-u", "a", "command", "help", "--universe"]), ("a", "command", False))
        self.assertEqual(cli._selection(["--universe=b", "echo", "-u", "c"]), ("b", "echo", False))
        self.assertEqual(cli._selection(["-ud", "list"]), ("d", "list", False))
        self.assertEqual(cli._selection(["send", "x", "--universe", "e"]), (None, "send", False))
        self.assertEqual(cli._selection(["--uni", "f", "echo"]), ("f", "echo", False))
        self.assertEqual(cli._selection(["--u=g", "--he", "echo"]), ("g", "echo", True))
        self.assertEqual(cli._selection(["-h", "echo", "--help"]), (None, "echo", True))
        self.assertEqual(cli._selection(["echo", "-h"]), (None, "echo", False))
        self.assertEqual(cli._selection(["-u=h", "echo"]), ("h", "echo", False))
        self.assertEqual(cli._selection(["-hui", "bad"]), ("i", "bad", True))
        self.assertEqual(cli._selection(["-u"]), (None, None, False))
        # A built-in command keeps literal -u/--universe arguments as its own.
        self.assertIn("User arguments:\n--universe", self.mv2("-u", str(self.root), "command", "help", "--universe"))
        child = self.subverse("child")
        self.enable("echo", root=child)
        result = json.loads(self.mv2("-u", str(child), "echo", "--universe", "elsewhere", "-u", "again", "w"))
        self.assertEqual(result, {"root": "child", "named": "again", "words": ["w"]})

    def test_abbreviated_universe_selects_the_same_scope_everywhere(self):
        child = self.subverse("child")
        self.enable("echo", root=child)
        os.environ["MV2_UNIVERSE"] = str(self.root)
        result = json.loads(self.mv2("--uni", str(child), "echo", "--universe", "x", "w"))
        self.assertEqual(result, {"root": "child", "named": "x", "words": ["w"]})
        result = json.loads(self.mv2(f"--univ={child}", "echo"))
        self.assertEqual(result["root"], "child")
        self.assertEqual(json.loads(self.mv2(f"-u={child}", "echo"))["root"], "child")

    def test_help_before_the_action_is_top_level_help(self):
        self.enable("echo")
        for flag in ("-h", "--help", "--he"):
            with self.subTest(flag):
                # `bad` has a configure that raises: top-level help must not run it.
                text = self.mv2("-u", str(self.root), flag, "bad")
                self.assertIn("Bad options (plugin echo)", text)
        os.environ["MV2_UNIVERSE"] = str(self.root)
        self.assertIn("Bad options (plugin echo)", self.mv2(f"-hu{self.root}", "bad"))
        self.assertFalse((self.root / "state" / "plugins" / "echo" / "errors.log").exists())

    def test_plugin_failures_are_plain_errors(self):
        self.enable("echo")
        self.assertIn("mv2: plugin echo: mv2 bad options: RuntimeError: bad options", self.mv2("-u", str(self.root), "bad", code=1))
        self.assertIn("mv2: plugin echo: mv2 crash failed: ZeroDivisionError: division by zero",
                      self.mv2("-u", str(self.root), "crash", code=1))
        self.assertEqual(self.mv2("-u", str(self.root), "refuse", code=1).strip(), "mv2: not today")
        log = (self.root / "state" / "plugins" / "echo" / "errors.log").read_text()
        self.assertIn("RuntimeError: bad options", log)
        self.assertNotIn("Traceback", self.mv2("-u", str(self.root), "bad", code=1))

    def test_detailed_help_of_a_plugin_command_runs_its_configure(self):
        # The one help that imports plugin code: a command's own options come from its configure.
        self.enable("echo")
        self.assertIn("--universe NAMED", self.mv2("-u", str(self.root), "echo", "--help"))

    def test_unknown_plugins_action_is_refused(self):
        from multiverse.tools import plugins as tool
        with self.assertRaisesRegex(ValueError, "Unknown plugins action 'untrst'"):
            tool.run(self.root, plugin_action="untrst", name="echo")


class BoundsTest(PluginTestCase):
    def test_bootstrap_lines_refuse_every_line_separator(self):
        from multiverse.plugins import api
        for text in ("one\rtwo", "one two", "one\ntwo", "one\x85two", " "):
            with self.subTest(text=text):
                self.write_plugin("split", {"module": "plugin", "extends": ["bootstrap"]},
                                  {"plugin.py": f"def register(mv): mv.bootstrap.line({text!r})\n"})
                self.enable("split")
                from multiverse.plugins import loader
                loader.reset()
                self.assertEqual(catalog._plugin_lines(self.root), [])
                self.assertIn("one line of text", self.entry("split").problem)
        self.write_plugin("long", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv):\n    mv.bootstrap.line('a' * 200)\n    mv.bootstrap.line('b' * 101)\n"})
        self.enable("long")
        self.assertIn(f"{api.BOOTSTRAP_CHARACTERS} characters", (catalog._plugin_lines(self.root), self.entry("long").problem)[1])

    def test_an_empty_qualifier_matches_nothing(self):
        with self.assertRaisesRegex(ValueError, "Expected one exact match for 'workflows/review@'"):
            catalog.command(self.root, "workflows/review@")
        with self.assertRaisesRegex(ValueError, "Expected one exact match for '@'"):
            catalog.command(self.root, "@")
