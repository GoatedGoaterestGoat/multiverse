"""Plugin commands in the command line: the universe they act on, and a broken plugin never breaking mv2's own."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import cli, plugins
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase

ECHO = """
def register(mv):
    def configure(parser):
        parser.add_argument("--universe", "-u", dest="named")
        parser.add_argument("words", nargs="*")
    def run(root=None, named=None, words=()):
        return {"root": root.parent.name if root.name == ".multiverse" else root.name, "named": named, "words": words}
    mv.cli.command("echo", configure, run)
"""


class CliCase(PluginTestCase):
    def mv2(self, *arguments):
        """(exit code, stdout, stderr) of mv2 run in this process as a new process would run it: nothing loaded."""
        loader.reset()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["mv2", *arguments]), contextlib.chdir(self.work), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as exit:
                code = 0 if exit.code is None else exit.code if isinstance(exit.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def json(self, *arguments):
        code, out, err = self.mv2(*arguments)
        self.assertEqual(code, 0, err)
        return json.loads(out)


class SelectionTest(CliCase):
    def test_only_explicit_universe_options_select_the_universe(self):
        """-u and its abbreviations (-ud, --uni, -hui, -u=h), not MV2_UNIVERSE, pick the universe a plugin command
        acts on, and everything from the command on is the command's own; fragile because _selection is a
        hand-written pre-parser, and every agent has MV2_UNIVERSE set, so a break quietly runs the command on the
        agent's own universe."""
        cases = ((["-u", "a", "command", "help", "--universe"], ("a", "command", False)),
                 (["--universe=b", "echo", "-u", "c"], ("b", "echo", False)),
                 (["-ud", "list"], ("d", "list", False)),
                 (["send", "x", "--universe", "e"], (None, "send", False)),
                 (["--uni", "f", "echo"], ("f", "echo", False)),
                 (["--u=g", "--he", "echo"], ("g", "echo", True)),
                 (["-h", "echo", "--help"], (None, "echo", True)),
                 (["echo", "-h"], (None, "echo", False)),
                 (["-u=h", "echo"], ("h", "echo", False)),
                 (["-hui", "bad"], ("i", "bad", True)),
                 (["-u"], (None, None, False)))
        for argv, expected in cases:
            with self.subTest(argv):
                self.assertEqual(cli._selection(argv), expected)
        self.write_plugin("echo", {"module": "plugin", "extends": ["cli"], "commands": {"echo": "Echo"}},
                          {"plugin.py": ECHO})
        child = self.subverse("child")
        self.enable("echo", root=child)
        os.environ["MV2_UNIVERSE"] = str(self.root)  # an agent's own universe, where echo is off
        with self.subTest("a built-in command keeps -u and --universe after it as its own"):
            code, out, err = self.mv2("-u", str(self.root), "command", "help", "--universe")
            self.assertEqual(code, 0, err)
            self.assertIn("User arguments:\n--universe", out)
        for argv, named, words in (
                (["-u", str(child), "echo", "--universe", "elsewhere", "-u", "again", "w"], "again", ["w"]),
                (["--uni", str(child), "echo", "--universe", "x", "w"], "x", ["w"]),
                ([f"--univ={child}", "echo"], None, []),
                ([f"-u={child}", "echo"], None, [])):
            with self.subTest(argv):
                self.assertEqual(self.json(*argv), {"root": "child", "named": named, "words": words})
        with self.subTest("without -u, the environment"):
            self.assertEqual(self.mv2("echo")[0], 2)  # echo is off in the universe MV2_UNIVERSE names
            os.environ["MV2_UNIVERSE"] = str(child)
            self.assertEqual(self.json("echo")["root"], "child")


class BrokenPluginTest(CliCase):
    def command(self, name, exception, cli_callback):
        source = ("def register(mv):\n"
                  "    def fail(root=None):\n"
                  f"        raise {exception}('QA callback failure')\n"
                  f"    mv.cli.command({name!r}, lambda parser: None, fail{', fail' if cli_callback else ''})\n")
        self.write_plugin(name, {"module": "plugin", "extends": ["cli"], "commands": {name: "QA callback failure"}},
                          {"plugin.py": source})
        self.enable(name)

    def test_a_broken_plugin_never_breaks_core_commands(self):
        """list, status and send stay core, two plugins claiming one name get neither, and a plugin command whose
        plugin failed to load, never registered it or raised fails alone, saying so once, while built-ins keep
        working; a break goes unnoticed because suites use healthy plugins, and one broken plugin would stop every
        agent's mv2 send (PLUGINS-1)."""
        with self.subTest("core names stay core"):
            self.write_plugin("thief", {"extends": ["cli"], "commands": {"list": "steal", "status": "steal",
                                                                         "send": "steal"}})
            self.enable("thief")
            parser = cli.parser(["-u", str(self.root), "list"])
            self.assertEqual(cli.tool("list").__name__, "multiverse.tools.list_agents")
            self.assertEqual(cli.tool("send").__name__, "multiverse.tools.send")
            self.assertEqual(cli.tool("status").__name__, "multiverse.tools.report")
            self.assertEqual(parser.parse_args(["list"]).action, "list")
            problems = plugins.problems(self.root, cli.RESERVED)
            self.assertTrue(any("thief" in problem and "mv2 list" in problem for problem in problems), problems)
            self.enable("thief", on=False)
        with self.subTest("a name two plugins claim"):
            for name in ("one", "two"):
                self.write_plugin(name, {"extends": ["cli"],
                                         "commands": {"shared": "x", f"own-{name}": "y", "list": "z"}})
                self.enable(name)
            self.assertEqual(set(plugins.commands(self.root, reserved={"list"})), {"own-one", "own-two"})
            problems = plugins.problems(self.root, reserved={"list"})
            self.assertTrue(any("one and two" in problem and "shared" in problem for problem in problems), problems)
            for name in ("one", "two"):
                self.enable(name, on=False)
        with self.subTest("failed to load"):
            self.write_plugin("broken", {"module": "plugin", "extends": ["cli"], "commands": {"broken": "QA broken"}},
                              {"plugin.py": "raise RuntimeError('QA load failure')\n"})
            self.enable("broken")
            code, out, err = self.mv2("-u", str(self.root), "broken")
            self.assertEqual(code, 1)
            self.assertEqual(err.strip().splitlines(),
                             ["mv2: plugin broken: failed to load: RuntimeError: QA load failure"])  # said once
        with self.subTest("never registered"):
            self.write_plugin("ghost", {"module": "plugin", "extends": ["cli"], "commands": {"ghost": "Not there"}},
                              {"plugin.py": "def register(mv):\n    pass\n"})
            self.enable("ghost")
            code, out, err = self.mv2("-u", str(self.root), "ghost")
            self.assertEqual(code, 1)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("mv2: plugin ghost:"), err)
        for cli_callback in (False, True):
            for exception in ("ValueError", "OSError", "KeyError", "RuntimeError", "TypeError", "ZeroDivisionError"):
                with self.subTest(cli_callback=cli_callback, exception=exception):
                    name = ("cli-" if cli_callback else "primary-") + exception.lower()
                    self.command(name, exception, cli_callback)
                    code, out, err = self.mv2("-u", str(self.root), name)
                    self.assertNotEqual(code, 0)
                    if exception in ("TypeError", "ZeroDivisionError"):  # unexpected: it names the plugin
                        self.assertTrue(err.startswith(f"mv2: plugin {name}:"), err)
                    else:  # an error the command gives on purpose reads as a built-in one's
                        message = "'QA callback failure'" if exception == "KeyError" else "QA callback failure"
                        self.assertEqual(err.strip(), "mv2: " + message)
        with self.subTest("built-ins keep working with every broken plugin on"):
            self.assertEqual(self.json("-u", str(self.root), "list", "--json"), [])
