"""The harness side of plugins: launch plans, capabilities, usage, status-line items and helper actions."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, harness, loader, manifest
from multiverse.plugins.testing import PluginTestCase

PROJECT = Path(__file__).resolve().parents[1]

GUARD = """
from multiverse.plugins import api

def register(mv):
    @mv.launch.contribute("claude")
    def claude(info):
        return api.Contribution(environment={"GUARD_ON": "1"},
                                claude_settings={"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": "guard"}]}]}})
    mv.capabilities.add("guarding", lambda info: api.Capability("supported", mechanism="guard-" + info.family))
    mv.usage.adjust(lambda info, usage: {**usage, "context": {**usage.get("context", {}), "estimated": True}})

    @mv.helper.action("status")
    def status(arguments):
        print("status", *arguments)
        return 3

    mv.helper.action("crash", lambda arguments: 1 / 0)
"""


class HarnessManifestTest(PluginTestCase):
    def test_harness_entries_and_status_items(self):
        directory = self.write_plugin("guard", {
            "extends": ["launch", "statusline"], "permissions": ["harness.claude", "harness.pi", "harness.codex"],
            "harnesses": {"claude": [{"dir": "claude", "min_version": "2.1.290", "needs_bridge": True}],
                          "pi": [{"extension": "pi/extension.ts"}],
                          "codex": [{"min_version": "0.162.1", "max_version": "0.162.1", "config": {"model_verbosity": "low"}}]},
            "statusline": {"trees": {"claude": True, "tmux": True}}})
        found = manifest.load(directory)
        [claude] = found.harnesses["claude"]
        self.assertEqual((claude.path, claude.min_version, claude.needs_bridge), ("claude", (2, 1, 290), True))
        self.assertIsNone(claude.fits((2, 1, 296)))
        self.assertEqual(claude.fits((2, 1, 289)), "needs claude 2.1.290 or newer; this is 2.1.289")
        self.assertEqual(claude.fits(None), "the harness version could not be read")
        [codex] = found.harnesses["codex"]
        self.assertEqual(codex.fields, {"config": {"model_verbosity": "low"}})
        self.assertEqual(codex.fits((0, 162, 2)), "needs codex 0.162.1 or older; this is 0.162.2")
        self.assertEqual(found.statusline, (manifest.Item("trees", True, True),))

    def test_refuses_what_it_cannot_use(self):
        cases = {
            "permission": ({"harnesses": {"claude": [{"dir": "c"}]}}, 'needs the permission "harness.claude"'),
            "family": ({"permissions": ["harness.claude"], "harnesses": {"emacs": []}}, "unknown harness family"),
            "version": ({"permissions": ["harness.claude"], "harnesses": {"claude": [{"dir": "c", "min_version": "2.1"}]}},
                        "MAJOR.MINOR.PATCH"),
            "escape": ({"permissions": ["harness.pi"], "harnesses": {"pi": [{"extension": "/etc/x.ts"}]}}, "inside the plugin"),
            "core item": ({"extends": ["statusline"], "statusline": {"inbox": {}}}, "one mv2 draws itself"),
            "undeclared": ({"statusline": {"trees": {}}}, 'needs "statusline" in extends'),
        }
        for label, (fields, expected) in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(manifest.ManifestError, expected):
                    manifest.load(self.write_plugin("case", fields))


class LaunchPlanTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("guard", {
            "module": "plugin", "extends": ["launch", "capabilities", "usage", "statusline", "helper"],
            "permissions": ["harness.claude", "harness.pi"],
            "harnesses": {"claude": [{"dir": "claude", "min_version": "2.1.290"}], "pi": [{"extension": "pi/x.ts"}]},
            "statusline": {"trees": {"claude": True, "tmux": False}}}, {"plugin.py": GUARD})
        self.enable("guard")

    def info(self, **changes):
        values = dict(universe=self.root, agent_id="a", agent_name="worker", mode="spawn", family="claude",
                      harness="claude", version=(2, 1, 296), bridge=True)
        values.update(changes)
        return harness.LaunchInfo(**values)

    def test_a_fitting_launch_gets_everything(self):
        plan = harness.launch_plan(self.info())
        directory = str((self.plugins_dir / "guard" / "claude").resolve())
        self.assertEqual(plan.claude_plugin_dirs, [directory])
        self.assertEqual(plan.environment, {"GUARD_ON": "1"})
        self.assertEqual(plan.claude_settings["hooks"]["PreCompact"][0]["hooks"][0]["command"], "guard")
        self.assertEqual(plan.plugins, {"guard": {"version": "0.1.0", "state": "loaded",
                                                  "added": ["claude_plugin_dirs", "claude_settings", "environment"]}})
        self.assertEqual(plan.status_items, [{"name": "trees", "plugin": "guard", "claude": True, "tmux": False}])
        pi = harness.launch_plan(self.info(family="pi", version=None))
        self.assertEqual(pi.pi_extensions, [str((self.plugins_dir / "guard" / "pi" / "x.ts").resolve())])

    def test_an_unfitting_launch_records_why(self):
        for info, reason in ((self.info(version=(2, 1, 280)), "needs claude 2.1.290 or newer; this is 2.1.280"),
                             (self.info(bridge=False), "needs mv2's bridge, which this launch does not use")):
            plan = harness.launch_plan(info)
            self.assertEqual((plan.claude_plugin_dirs, plan.environment), ([], {}))
            self.assertEqual(plan.plugins["guard"], {"version": "0.1.0", "state": "skipped", "reason": reason})
        self.assertEqual(harness.launch_plan(self.info(family="codex")).plugins, {})

    def test_conflicts_and_core_variables_leave_the_plugin_out(self):
        self.write_plugin("rival", {"module": "plugin", "extends": ["launch"], "permissions": ["harness.claude"]}, {
            "plugin.py": "from multiverse.plugins import api\n"
                         "def register(mv): mv.launch.contribute('claude', lambda info: api.Contribution("
                         "environment={'GUARD_ON': '2'}))\n"})
        self.write_plugin("thief", {"module": "plugin", "extends": ["launch"]}, {
            "plugin.py": "from multiverse.plugins import api\n"
                         "def register(mv): mv.launch.contribute('claude', lambda info: api.Contribution("
                         "environment={'MV2_AGENT_ID': 'someone'}))\n"})
        self.write_plugin("slowpoke", {"module": "plugin", "extends": ["launch"]}, {
            "plugin.py": "def register(mv): mv.launch.contribute('claude', lambda info: 1 / 0)\n"})
        for name in ("rival", "thief", "slowpoke"):
            self.enable(name)
        plan = harness.launch_plan(self.info())
        self.assertEqual(plan.environment, {"GUARD_ON": "1"})
        self.assertEqual(plan.plugins["rival"]["reason"], "guard already sets GUARD_ON")
        self.assertEqual(plan.plugins["thief"]["reason"], "it may not set the environment variable 'MV2_AGENT_ID'")
        self.assertIn("ZeroDivisionError", plan.plugins["slowpoke"]["reason"])

    def test_the_adapters_own_settings_and_environment_never_change(self):
        plan = harness.launch_plan(self.info(), base_settings={"crossSessionInbound": "accept", "hooks": {"Stop": [0]}},
                                   base_environment={"GUARD_ON": "0"})
        self.assertEqual(plan.environment, {"GUARD_ON": "0"})
        self.assertEqual(plan.claude_settings, {"crossSessionInbound": "accept", "hooks": {"Stop": [0]}})
        self.assertEqual(plan.plugins["guard"]["reason"], "the harness already sets GUARD_ON")
        plain = harness.launch_plan(self.info(bridge=False))
        self.assertEqual(plain.plugins["guard"]["state"], "skipped")
        self.assertEqual(harness.status_items(self.root),
                         {"plugins": {}, "status_items": [{"name": "trees", "plugin": "guard", "claude": True, "tmux": False}]})

    def test_settings_merge_concatenates_hooks_and_refuses_clashes(self):
        merged = harness._merge_settings({"crossSessionInbound": "accept", "hooks": {"Stop": [1]}},
                                         {"hooks": {"Stop": [2], "PreCompact": [3]}})
        self.assertEqual(merged, {"crossSessionInbound": "accept", "hooks": {"Stop": [1, 2], "PreCompact": [3]}})
        with self.assertRaisesRegex(ValueError, "settings crossSessionInbound is set differently"):
            harness._merge_settings(merged, {"crossSessionInbound": "ask"})

    def test_problems_and_modes_are_reported(self):
        self.write_plugin("partial", {"module": "plugin", "extends": ["launch"]}, {
            "plugin.py": "from multiverse.plugins import api\n"
                         "def register(mv): mv.launch.contribute('claude', lambda info: api.Contribution("
                         "problem='Launched without partial: its settings are unusable', mode='without partial'))\n"})
        self.enable("partial")
        plan = harness.launch_plan(self.info())
        self.assertEqual(plan.problems, ["Launched without partial: its settings are unusable"])
        self.assertEqual(plan.modes, ["without partial"])

    def test_capabilities_usage_and_items(self):
        info = harness.SessionInfo(self.root, "a", "claude", "claude")
        self.assertEqual(harness.capabilities(info, taken={"spawn"}),
                         {"guarding": ("supported", "", "guard-claude")})
        self.assertEqual(harness.capabilities(info, taken={"guarding"}), {})
        self.assertEqual(harness.adjust_usage(info, {"context": {"tokens": 5}}),
                         {"context": {"tokens": 5, "estimated": True}})
        agent_dir = self.root / "state" / "agents" / "a"
        mv = api.Plugin(loader.load(self.entry("guard")))
        mv.statusline.write(agent_dir, "trees", "trees 3/4\n· building")
        self.assertEqual(harness.item_path(agent_dir, "trees").read_text(), "trees 3/4 · building\n")
        runtime = {"plugins": harness.launch_plan(self.info()).record()}
        self.assertEqual(harness.read_items(agent_dir, runtime), {"trees": "trees 3/4 · building"})
        with self.assertRaisesRegex(api.Error, "declare it in plugin.json"):
            mv.statusline.write(agent_dir, "other", "x")
        mv.statusline.write(agent_dir, "trees", "")
        self.assertFalse(harness.item_path(agent_dir, "trees").exists())

    def test_helper_actions(self):
        environment = {**os.environ, "PYTHONPATH": str(PROJECT / "src"), "MV2_UNIVERSE": str(self.root)}
        run = lambda *args: subprocess.run([sys.executable, "-m", "multiverse.plugins", "run", *args],
                                           env=environment, text=True, capture_output=True, timeout=60)
        result = run("guard", "status", "a", "--b")
        self.assertEqual((result.returncode, result.stdout), (3, "status a --b\n"))
        result = run("guard", "crash")
        self.assertEqual(result.returncode, 1)
        self.assertIn("mv2 plugin guard crash: helper crash: ZeroDivisionError", result.stderr)
        self.assertEqual(run("guard", "nothing").stderr.strip(), "mv2 plugin guard nothing: guard has no helper action 'nothing'")
        self.assertEqual(run("ghost", "x").returncode, 1)
