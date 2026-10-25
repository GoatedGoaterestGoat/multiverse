"""The harness side of plugins: what launch plans may contribute, usage adjustments and version bounds."""
import os
from pathlib import Path
import sys
import time
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import harnesses
from multiverse.harnesses.types import LaunchContext, NativeResult
from multiverse.plugins import harness, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import native
from multiverse.store import read

GUARD = """
from multiverse.plugins import api

def register(mv):
    @mv.launch.contribute("claude")
    def claude(info):
        return api.Contribution(environment={"GUARD_ON": "1"},
                                claude_settings={"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": "guard"}]}]}})
"""


class LaunchPlanTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("guard", {
            "module": "plugin", "extends": ["launch", "statusline"], "permissions": ["harness.claude", "harness.pi"],
            "harnesses": {"claude": [{"dir": "claude", "min_version": "2.1.290"}], "pi": [{"extension": "pi/x.ts"}]},
            "statusline": {"trees": {"claude": True, "tmux": False}}}, {"plugin.py": GUARD})
        self.enable("guard")

    def test_an_item_named_focus_is_not_shown_and_the_plugin_stays_valid(self):
        """A plugin's status-line item named focus, a name core took after API 1.8, is left out and doctor says why,
        while the plugin stays valid; a break turns a 1.x plugin that loaded before into an invalid one, with no
        commands, screens or launch additions."""
        self.write_plugin("moods", {"extends": ["statusline"],
                                    "statusline": {"focus": {"claude": True, "tmux": True}, "mood": {}}})
        self.enable("moods")
        found = {entry.name: entry for entry in loader.entries(self.root)}
        self.assertTrue(found["moods"].usable, found["moods"].problem)
        names = [item["name"] for item in harness.declared_items(self.root, loader.active(self.root))]
        self.assertIn("mood", names)
        self.assertNotIn("focus", names)
        self.assertIn("moods's status-line item 'focus' is not shown: mv2 draws that item itself now; rename it",
                      harness.item_conflicts(self.root))

    def info(self, **changes):
        values = dict(universe=self.root, agent_id="a", agent_name="worker", mode="spawn", family="claude",
                      harness="claude", version=(2, 1, 296), bridge=True)
        values.update(changes)
        return harness.LaunchInfo(**values)

    def spawn_pi(self):
        """A Pi launch of a new agent here, as the Pi adapter builds it."""
        agent_dir = self.root / "state" / "agents" / str(uuid4())
        agent_dir.mkdir(parents=True)
        values = {"name": "worker", "agent_id": agent_dir.name, "session_id": "", "bootstrap": "b",
                  "bootstrap_json": '"b"', "task": "--literal task", "agent_dir": str(agent_dir),
                  "harness_dir": str(self.root / "harnesses" / "pi"), "session_file": str(agent_dir / "session.jsonl"),
                  "bootstrap_file": "/launch/bootstrap.md", "task_file": "/launch/task.md", "workspace": str(self.work),
                  "model": "", "control_socket": "/tmp/owned/rpc.sock"}
        return harnesses.load(self.root, "pi").spawn(LaunchContext("spawn", values, resume_prompt="--hello"))

    def test_a_plugin_may_set_only_its_own_mv2_variables(self):
        """No plugin can set an identity or home variable (MV2_AGENT_ID, MV2_IDENTITY_MODE, CODEX_HOME,
        CLAUDE_CONFIG_DIR, HOME, PATH) or replace the adapter's or another plugin's environment and settings, and its
        hooks are appended to mv2's; a break goes unnoticed because launches start with a wrong identity or without
        mv2's hooks (1e1d810)."""
        with self.subTest("the variables a plugin may set"):
            self.assertTrue(harness.may_set("compaction", "MV2_COMPACTION"))
            self.assertTrue(harness.may_set("cost-item", "MV2_COST_ITEM_FORMAT"))
            self.assertTrue(harness.may_set("compaction", "CLAUDE_CODE_SOMETHING"))
            for plugin, key in (("compaction", "MV2_COMPACTIONS"), ("compaction", "MV2_AGENT_ID"),
                                ("agent", "MV2_AGENT_ID"), ("plugins", "MV2_PLUGINS"), ("x", "BAD-NAME"), ("x", ""),
                                ("x", "HOME"), ("x", "PATH"), ("x", "CODEX_HOME"), ("x", "CODEX_SQLITE_HOME"),
                                ("x", "CLAUDE_CONFIG_DIR"),
                                # The adapter's declared shared-agent authority: never a plugin's, even one named so.
                                ("identity", "MV2_IDENTITY_MODE"), ("x", "MV2_IDENTITY_MODE")):
                with self.subTest(plugin=plugin, key=key):
                    self.assertFalse(harness.may_set(plugin, key))
        with self.subTest("hooks are appended and a clashing setting is refused"):
            merged = harness._merge_settings({"crossSessionInbound": "accept", "hooks": {"Stop": [1]}},
                                             {"hooks": {"Stop": [2], "PreCompact": [3]}})
            self.assertEqual(merged, {"crossSessionInbound": "accept", "hooks": {"Stop": [1, 2], "PreCompact": [3]}})
            with self.assertRaisesRegex(ValueError, "settings crossSessionInbound is set differently"):
                harness._merge_settings(merged, {"crossSessionInbound": "ask"})
        with self.subTest("the adapter's own settings and environment never change"):
            plan = harness.launch_plan(self.info(), base_settings={"crossSessionInbound": "accept",
                                                                   "hooks": {"Stop": [0]}},
                                       base_environment={"GUARD_ON": "0"})
            self.assertEqual(plan.environment, {"GUARD_ON": "0"})
            self.assertEqual(plan.claude_settings, {"crossSessionInbound": "accept", "hooks": {"Stop": [0]}})
            self.assertEqual(plan.plugins["guard"]["reason"], "the harness already sets GUARD_ON")
            plain = harness.launch_plan(self.info(bridge=False))
            self.assertEqual(plain.plugins["guard"]["state"], "skipped")
            self.assertEqual(harness.status_items(self.root), {"plugins": {}, "status_items": [
                {"name": "trees", "plugin": "guard", "claude": True, "tmux": False}]})
        with self.subTest("another plugin's variable and core variables leave the plugin out"):
            self.write_plugin("rival", {"module": "plugin", "extends": ["launch"], "permissions": ["harness.claude"]}, {
                "plugin.py": "from multiverse.plugins import api\n"
                             "def register(mv): mv.launch.contribute('claude', lambda info: api.Contribution("
                             "environment={'GUARD_ON': '2'}))\n"})
            self.write_plugin("thief", {"module": "plugin", "extends": ["launch"]}, {
                "plugin.py": "from multiverse.plugins import api\n"
                             "def register(mv): mv.launch.contribute('claude', lambda info: api.Contribution("
                             "environment={'MV2_AGENT_ID': 'someone'}))\n"})
            for name in ("rival", "thief"):
                self.enable(name)
            plan = harness.launch_plan(self.info())
            self.assertEqual(plan.environment, {"GUARD_ON": "1"})
            self.assertEqual(plan.plugins["rival"]["reason"], "guard already sets GUARD_ON")
            # A plugin left out is a problem of the launch too, where the inspector shows it.
            self.assertEqual(sorted(problem.split(":")[0] for problem in plan.problems),
                             ["plugin rival was left out", "plugin thief was left out"])
            self.assertEqual(plan.plugins["thief"]["reason"], "it may not set the environment variable 'MV2_AGENT_ID'")

    def test_malformed_contributions_leave_only_that_plugin_out(self):
        """In one launch plan a plugin whose import fails, whose contributor raises, whose fields are malformed or
        whose settings its own check refuses loses only itself, saying why, and one that failed to load is tried again
        once its files change; a break goes unnoticed because tests use healthy plugins, and it blocks every launch
        or launches a plugin with settings it refuses."""
        with self.subTest("a contributor that raises or returns malformed fields"):
            cases = {"noenv": "environment=None", "badsettings": "claude_settings=[]",
                     "object": "claude_settings={'x': object()}", "blank": "problem='  '", "number": "mode=3"}
            for name, fields in cases.items():
                self.write_plugin(name, {"module": "plugin", "extends": ["launch"]}, {
                    "plugin.py": "from multiverse.plugins import api\n"
                                 f"def register(mv): mv.launch.contribute('claude', lambda info: "
                                 f"api.Contribution({fields}))\n"})
                self.enable(name)
            self.write_plugin("slowpoke", {"module": "plugin", "extends": ["launch"]}, {
                "plugin.py": "def register(mv): mv.launch.contribute('claude', lambda info: 1 / 0)\n"})
            self.enable("slowpoke")
            plan = harness.launch_plan(self.info())
            self.assertEqual(plan.environment, {"GUARD_ON": "1"})
            self.assertEqual({name: plan.plugins[name]["state"] for name in (*cases, "slowpoke")},
                             {name: "skipped" for name in (*cases, "slowpoke")})
            self.assertIn("environment must be a dict", plan.plugins["noenv"]["reason"])
            self.assertIn("claude_settings must be JSON", plan.plugins["object"]["reason"])
            self.assertIn("ZeroDivisionError", plan.plugins["slowpoke"]["reason"])
            self.assertIn("plugin slowpoke was left out", [problem.split(":")[0] for problem in plan.problems])
        with self.subTest("an import that fails is said, and tried again once its files change"):
            directory = self.write_plugin("brittle", {
                "module": "plugin", "extends": ["launch", "statusline"], "permissions": ["harness.claude"],
                "harnesses": {"claude": [{"dir": "claude"}]}, "statusline": {"brittle": {"claude": True}}},
                {"plugin.py": "raise SystemExit(2)\n"})
            self.enable("brittle")
            plan = harness.launch_plan(self.info())
            self.assertEqual(plan.plugins["brittle"]["state"], "skipped")
            self.assertIn("plugin brittle was left out: it failed to load: SystemExit: 2", plan.problems)
            self.assertNotIn("brittle", [item["name"] for item in plan.status_items])
            (directory / "plugin.py").write_text("def register(mv): pass\n")  # fixed: no restart needed
            plan = harness.launch_plan(self.info())
            self.assertEqual(plan.plugins["brittle"]["state"], "loaded")
            self.assertIn("brittle", [item["name"] for item in plan.status_items])
        with self.subTest("settings its own check refuses, in a fresh process: left out of Claude launches"):
            self.write_plugin("picky", {
                "module": "plugin", "extends": ["launch"], "permissions": ["harness.claude"],
                "harnesses": {"claude": [{"dir": "claude"}]}, "settings": {"version": 1}}, {"plugin.py": (
                    "def register(mv):\n"
                    "    def check(settings):\n"
                    "        if settings.get('level') not in (None, 1, 2):\n"
                    "            raise ValueError('level must be 1 or 2')\n"
                    "    mv.settings.check(check)\n")})
            self.enable("picky")
            (self.root / "plugin-settings").mkdir(exist_ok=True)
            (self.root / "plugin-settings" / "picky.json").write_text('{"schema": 1, "level": 9}')
            loader.reset()  # its check runs only once its code is loaded, as the launch loads it
            plan = harness.launch_plan(self.info())
            self.assertNotIn(str((self.plugins_dir / "picky" / "claude").resolve()), plan.claude_plugin_dirs)
            self.assertEqual(plan.plugins["picky"]["state"], "skipped")
            self.assertTrue(any(problem.startswith("plugin picky was left out: its settings cannot be used")
                                and "level must be 1 or 2" in problem for problem in plan.problems), plan.problems)
            self.assertEqual(plan.record()["problems"], plan.problems)  # recorded, so inspect can say it

    def test_a_plugins_outcome_is_recorded_as_its_own(self):
        """A contribution's problem and mode reach the launch's plugins record under the plugin's name, in its entry
        and in the launch's lists: the harness's own mode no longer carries them, so a break would make a launch
        that runs without a plugin's guard look normal in mv2 inspect and the inspector."""
        self.write_plugin("warden", {"module": "plugin", "extends": ["launch"]}, {
            "plugin.py": "from multiverse.plugins import api\n"
                         "def register(mv): mv.launch.contribute('claude', lambda info: "
                         "api.Contribution(problem='Launched without the guard: off here', mode='without the guard'))\n"})
        self.enable("warden")
        record = harness.launch_plan(self.info()).record()
        self.assertEqual({key: record["plugins"]["warden"].get(key) for key in ("state", "problem", "mode")},
                         {"state": "loaded", "problem": "Launched without the guard: off here",
                          "mode": "without the guard"})
        self.assertIn("Launched without the guard: off here", record["problems"])
        self.assertEqual(record["modes"], [{"plugin": "warden", "mode": "without the guard"}])

    def test_usage_adjusters_never_change_what_they_were_given(self):
        """Plugin usage adjusters get a copy, adjust only ok results, never change core's measured context (1.13:
        changes to it are dropped, the rest applies), and mutation or exceptions never corrupt measured usage; a
        break goes unnoticed because the Context numbers are just slightly wrong."""
        for name, body in (("amutate", "usage['context']['tokens'] = 999; raise RuntimeError('half done')"),
                           ("bjunk", "return ['not', 'usage']"),
                           ("cset", "return {**usage, 'context': {'tokens': 1, 'estimated': True}, 'note': 'kept'}"),
                           ("dnone", "return None")):
            self.write_plugin(name, {"module": "plugin", "extends": ["usage"]}, {
                "plugin.py": f"def adjust(info, usage):\n    {body}\ndef register(mv): mv.usage.adjust(adjust)\n"})
            self.enable(name)
        self.enable("guard", on=False)
        measured = {"context": {"tokens": 5}}
        adjusted = {"context": {"tokens": 5}, "note": "kept"}  # core's context as measured; the rest applies
        with self.subTest("each adjuster gets a copy, and core's context stays as measured"):
            info = harness.SessionInfo(self.root, "a", "claude", "claude")
            self.assertEqual(harness.adjust_usage(info, measured), adjusted)
            self.assertEqual(measured, {"context": {"tokens": 5}})
            self.assertEqual(harness.adjust_usage(info, {"cost": 2}), {"cost": 2, "note": "kept"})  # none added
            errors = loader.errors()
            self.assertIn("usage: RuntimeError: half done", errors["amutate"])
            self.assertIn("usage: TypeError: a usage adjustment returns the usage record (a dict) or None",
                          errors["bjunk"])
            self.enable("cset", on=False)
            self.assertIs(harness.adjust_usage(info, measured), measured)
            self.enable("cset")
        with self.subTest("only an ok measurement is adjusted, and a plugin failure leaves it as measured"):
            directory = self.agent("worker")
            properties, facts = read(directory / "properties.json"), read(directory / "runtime.json")

            def snapshot(result):
                with mock.patch("multiverse.harnesses.claude.ClaudeAdapter.get_usage", return_value=result):
                    return native.usage_snapshot(self.root, properties, facts, reconciled=True)
            self.assertEqual(snapshot(NativeResult("ok", value=measured))["value"], adjusted)
            for state in ("unavailable", "error"):
                result = NativeResult(state, "not measured", value=measured)
                self.assertEqual(snapshot(result), result.to_dict())
            with mock.patch.object(loader, "active", side_effect=RuntimeError("plugins broke")):
                self.assertEqual(snapshot(NativeResult("ok", value=measured))["value"], measured)

    def test_version_of_follows_an_executable_replaced_in_place(self):
        """The harness version cache is keyed on the executable's mtime, and Pi's version bounds ask the launched
        executable once and only when a plugin has something for Pi; fragile because Claude and Codex update
        themselves in place, and after an update version-gated plugin parts go to a version that cannot take them."""
        with self.subTest("an executable replaced in place is asked again"):
            tool = self.work / "tools" / "fake-pi"
            tool.parent.mkdir()

            def install(script):
                tool.write_text("#!/bin/sh\n" + script + "\n")
                tool.chmod(0o755)
            install("echo 'pi 0.84.3'")
            self.assertEqual(harness.version_of(str(tool)), (0, 84, 3))
            install("echo 'pi 0.85.0 (new build)'")
            os.utime(tool, ns=(time.time_ns(), time.time_ns() + 10**9))
            self.assertEqual(harness.version_of(str(tool)), (0, 85, 0))
            install("echo 'pi 9.9.9'; exit 3")
            os.utime(tool, ns=(time.time_ns(), time.time_ns() + 2 * 10**9))
            self.assertIsNone(harness.version_of(str(tool)))
            install("echo 'no version here'")
            os.utime(tool, ns=(time.time_ns(), time.time_ns() + 3 * 10**9))
            self.assertIsNone(harness.version_of(str(tool)))
            self.assertIsNone(harness.version_of(str(self.work / "missing")))
        with self.subTest("Pi's bounds ask the selected executable once"):
            self.enable("guard", on=False)
            self.write_plugin("pi-new", {"permissions": ["harness.pi"], "harnesses": {"pi": [
                {"extension": "pi/new.ts", "min_version": "0.84.0", "max_version": "0.90.0"}]}})
            self.enable("pi-new")
            new = str((self.plugins_dir / "pi-new" / "pi" / "new.ts").resolve())
            for version, expected in (((0, 84, 3), True), ((0, 83, 9), False), ((0, 91, 0), False), (None, False)):
                with self.subTest(version=version), \
                     mock.patch("multiverse.plugins.harness.version_of", return_value=version) as probe:
                    command = self.spawn_pi()
                    self.assertEqual(new in command.argv, expected)
                    probe.assert_called_once_with(command.argv[0])
            self.enable("pi-new", on=False)
            with mock.patch("multiverse.plugins.harness.version_of") as probe:
                self.spawn_pi()
                probe.assert_not_called()  # nothing here asks about Pi: no probe


class SessionWorkspaceTest(PluginTestCase):
    def test_both_session_entry_points_give_the_recorded_workspace(self):
        """SessionInfo.workspace is the agent's recorded workspace from both entry points (the core session handle
        and Agent.session()); a break goes unnoticed, because only GOAT's transcript search reads it, and one that
        is missing or another agent's makes that search fail closed or skip the wrong unreadable folder."""
        from multiverse.harnesses.types import SessionHandle
        from multiverse.plugins import api
        self.write_plugin("reader", {"module": "plugin", "permissions": ["agents.read"]},
                          {"plugin.py": "def register(mv):\n    pass\n"})
        self.enable("reader")
        directory = self.agent("worker")
        properties, facts = read(directory / "properties.json"), read(directory / "runtime.json")
        handle = SessionHandle.from_records(properties, facts, directory)
        mv = api.Plugin(loader.load(self.entry("reader")))
        for entry, info in (("core handle", harness.session_info(handle, "claude", facts)),
                            ("Agent.session()", mv.universe(self.root).agent("worker").session())):
            with self.subTest(entry):
                self.assertEqual(info.workspace, properties["workspace"])
                self.assertEqual(info.workspace, handle.workspace)
