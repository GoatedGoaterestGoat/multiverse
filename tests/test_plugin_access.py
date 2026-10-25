"""Access levels (API 1.13): what a plugin may do in a universe is the level the user grants it there."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import __main__ as helper, api, config, harness, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import plugins as tool

# A plugin that asks for act and can work with observe; its contributions declare their classes.
PLANNER = """
from multiverse.plugins import api

RAN = []

def register(mv):
    def plan(root, **arguments):
        RAN.append("plan")
        return {"agents": [agent.name for agent in mv.universe(root).agents()]}
    def install(root, **arguments):
        RAN.append("install")
        return {"installed": True}
    mv.cli.command("planner", lambda parser: None, plan, effect="observe")
    mv.cli.command("planner-install", lambda parser: None, install)
    mv.dashboard.column("sent", "SENT", 4, lambda row: mv.universe(row["universe"]).agent(row["id"]).send("x"))

    @mv.effect("observe")
    def peek(universe):
        return mv.universe(universe).agent("worker").send("from an observe function")
    RAN.append(peek)

    mv.launch.contribute("claude", lambda info: api.Contribution(environment={"PLANNER_ON": "1"}))
    mv.helper.action("install", lambda arguments: RAN.append("helper") or 2)
"""
PLANNER_MANIFEST = {"module": "plugin", "api": "1.13", "extends": ["cli", "dashboard.column", "launch", "helper"],
                    "commands": {"planner": "Show the plan", "planner-install": "Install it"},
                    "permissions": ["agents.read", "messages.send", "harness.claude"],
                    "access": {"request": "act", "minimum": "observe", "why": "the plan needs observe"}}


class AccessTest(PluginTestCase):
    def planner(self):
        self.write_plugin("planner", PLANNER_MANIFEST, {"plugin.py": PLANNER})
        return self.module_of("planner")

    def module_of(self, name):
        found = self.entry(name)
        return sys.modules[loader.load(found, off=not found.on).package + ".plugin"]

    def mv(self, name):
        found = self.entry(name)
        return api.Plugin(loader.load(found, off=not found.on))

    def test_a_plugins_level_is_its_grant_capped_by_its_request_and_the_universe(self):
        """The level a plugin has in a universe: none where it is off, else the level granted there (its request
        when none is recorded), never above its request, capped by access_ceiling and MV2_EFFECTS; a call above it
        raises api.AccessDenied before core runs, never an OSError; fragile because a wrong level silently gives a
        plugin reach the user never granted, or takes a working one's away."""
        self.planner()
        self.write_plugin("old", {"module": "plugin", "permissions": ["agents.read"]},
                          {"plugin.py": "def register(mv): pass\n"})
        self.write_plugin("viewer", {"module": "plugin", "api": "1.13", "access": {"request": "observe"}},
                          {"plugin.py": "def register(mv): pass\n"})
        self.agent("worker")
        rows = {  # name: (plugins.json changes, plugin, environment, level)
            "off": ({}, "planner", {}, None),
            "no grant recorded: its request": ({"enabled": {"planner": True}}, "planner", {}, "act"),
            "a manifest without access: act": ({"enabled": {"old": True}}, "old", {}, "act"),
            "granted": ({"enabled": {"planner": True}, "access": {"planner": "spend"}}, "planner", {}, "spend"),
            "a grant above its request": ({"enabled": {"viewer": True}, "access": {"viewer": "act"}}, "viewer", {},
                                          "observe"),
            "a read-only universe": ({"enabled": {"planner": True}, "access_ceiling": "observe"}, "planner", {},
                                     "observe"),
            "a read-only process": ({"enabled": {"planner": True}}, "planner", {"MV2_EFFECTS": "observe"}, "observe"),
        }
        path = config.path(self.root)
        for name, (changes, plugin, environment, level) in rows.items():
            with self.subTest(name), mock.patch.dict(os.environ, environment):
                path.write_text(json.dumps({"schema": 1, **changes}))
                loader.reset()
                mv = self.mv(plugin)
                self.assertEqual(mv.access(self.root), level)
                self.assertEqual(plugins.entry_access(self.root, plugin)["level"], level)
                universe = mv.universe(self.root)
                if level is None:
                    with self.assertRaisesRegex(api.AccessDenied, "planner has no access here; listing agents needs "
                                                                  "observe") as caught:
                        universe.agents()
                    self.assertNotIsInstance(caught.exception, OSError)  # an except OSError never swallows it
                    # A universe's own harness definitions stay readable, to configure a plugin before it is on.
                    self.assertEqual(mv.harness_info(self.root, "claude").family, "claude")
                    continue
                self.assertEqual([agent.name for agent in universe.agents()], ["worker"])
                if plugin == "viewer":  # access and no permissions: the permissions of its level, no more
                    self.assertTrue(mv.can("agents.read"))
                    with self.assertRaisesRegex(api.Error, "did not ask for permission 'messages.send'"):
                        universe.agent("worker").send("hi")
                    continue
                if level != "act" and plugin == "planner":
                    with mock.patch("multiverse.tools.send.run") as send:
                        with self.assertRaisesRegex(api.AccessDenied, f"planner has {level} access here; send needs "
                                                                      "act"):
                            universe.agent("worker").send("hi")
                        send.assert_not_called()  # refused before core is asked

    def test_what_a_plugin_is_shown_allowed_is_what_its_grant_allows(self):
        """mv2 plugins info and the listing say a plugin can do only what its access here allows, and list the rest as
        held back with the level each needs (a declaration no level gates stays allowed; off, it only asks): a
        break would tell a person a plugin lowered to observe can still spawn and send, the opposite of the truth."""
        self.write_plugin("lister", {"module": "plugin", "api": "1.13", "permissions": [
            "agents.read", "messages.ask", "messages.send", "harness.claude", "network.listen"],
            "access": {"request": "act", "minimum": "observe", "why": "it lists and sends"}},
            {"plugin.py": "def register(mv): pass\n"})
        names = {"agents.read": "read this universe's agents", "messages.ask": "ask agents side questions",
                 "messages.send": "send messages", "harness.claude": "add to Claude Code launches",
                 "network.listen": "listen for network connections"}
        for level, allowed, held in (
                ("act", {"agents.read", "messages.ask", "messages.send", "harness.claude", "network.listen"}, {}),
                ("spend", {"agents.read", "messages.ask", "network.listen"},
                 {"messages.send": "act", "harness.claude": "act"}),
                ("observe", {"agents.read", "network.listen"},
                 {"messages.ask": "spend", "messages.send": "act", "harness.claude": "act"}),
                (None, None, None)):
            with self.subTest(level or "off"):
                config.set_enabled(self.root, "lister", level is not None, grant=level)
                access = plugins.entry_access(self.root, "lister")
                info = tool.cli_run(self.root, plugin_action="info", name="lister")
                if level is None:
                    self.assertEqual((access["allowed"], access["held_back"]), (None, None))
                    self.assertIn("It asks to:\n- ", info)
                    self.assertNotIn("It can:", info)
                    continue
                shown = {name for name, text in names.items() for item in access["allowed"] if item.startswith(text)}
                self.assertEqual(shown, allowed)
                kept = {name: item["needs"] for name, text in names.items() for item in access["held_back"]
                        if item["text"].startswith(text)}
                self.assertEqual(kept, held)
                if held:
                    self.assertIn(f"Held back by its {level} access (a person can grant it with: mv2 plugins grant "
                                  "lister act):", info)
                    self.assertIn("(needs act)", info)
                    can = info.split("It can:")[1].split("Held back")[0]
                    self.assertNotIn("send messages", can)
                else:
                    self.assertNotIn("Held back", info)
        listed = {item["name"]: item for item in plugins.listing(self.root)}["lister"]
        self.assertEqual(listed["access"], plugins.entry_access(self.root, "lister"))  # the screen reads the same

    def test_a_refusal_names_what_actually_limits_the_plugin(self):
        """A refusal, a launch's note, mv2 plugins info and doctor name what holds a plugin back (MV2_EFFECTS, the
        universe's access_ceiling or its grant) and how to change it, and a plugin held below its minimum is said to
        be: a grant hint under a ceiling can't help, and an 'on' plugin that can't work looks fine, both unnoticed."""
        planner = self.planner()
        self.write_plugin("strict", {"module": "plugin", "api": "1.13", "permissions": ["agents.read"],
                                     "access": {"request": "act", "minimum": "act", "why": "it installs"}},
                          {"plugin.py": "def register(mv): pass\n"})
        for limit, environment, settings, hint, line in (
                ("grant", {}, {"access": {"planner": "observe", "strict": "observe"}},
                 "A person can grant it with: mv2 plugins grant planner act", "Access here: observe (it asks for act, "
                                                                               "at least observe)."),
                ("access_ceiling", {}, {"access_ceiling": "observe"},
                 "This universe's plugins.json caps every plugin at observe (access_ceiling)",
                 "; this universe's access_ceiling allows at most observe."),
                ("MV2_EFFECTS", {"MV2_EFFECTS": "observe"}, {},
                 "MV2_EFFECTS=observe caps every plugin in this process: run it without MV2_EFFECTS",
                 "; MV2_EFFECTS caps every plugin in this process at observe.")):
            with self.subTest(limit), mock.patch.dict(os.environ, environment):
                (self.root / "plugins.json").write_text(json.dumps(
                    {"schema": 1, "enabled": {"planner": True, "strict": True}, **settings}))
                with self.assertRaises(api.AccessDenied) as refused:
                    plugins.command(self.root, "planner-install").run(self.root)
                self.assertIn(hint, str(refused.exception))
                launch = harness.LaunchInfo(universe=self.root, agent_id="a", agent_name="worker", mode="spawn",
                                            family="claude", harness="claude", bridge=True)
                skipped = harness.launch_plan(launch).plugins["planner"]["reason"]
                self.assertIn(hint.lower(), skipped.lower())
                info = tool.cli_run(self.root, plugin_action="info", name="planner")
                self.assertIn(line, info)
                if limit != "grant":
                    self.assertNotIn("mv2 plugins grant", info)
                strict = "strict is on but held back below its minimum here: it has observe access and needs at least act"
                self.assertTrue(any(problem.startswith(strict) for problem in plugins.problems(self.root)))
                listed = {item["name"]: item for item in plugins.listing(self.root)}["strict"]
                self.assertIn(listed["state"], ("on", "loaded"))  # on, and said to be held back
                self.assertTrue(listed["problem"].startswith(strict))
        (self.root / "plugins.json").write_text(json.dumps({"schema": 1, "enabled": {"planner": True, "strict": True}}))
        self.assertFalse([problem for problem in plugins.problems(self.root) if "minimum" in problem])
        self.assertIn("Access here: act (it asks for act, and needs it).",
                      tool.cli_run(self.root, plugin_action="info", name="strict"))

    def test_declared_classes_cap_what_runs_inside_them(self):
        """A contribution runs under the class it declared (undeclared: act) and only where the plugin's level
        reaches it: an observe column or @mv.effect("observe") function can't send, an act command is refused
        before it runs, and launch contributions need act, the launch saying why; fragile because these run
        unattended (every refresh, every launch), where a break goes unnoticed."""
        self.planner()
        self.enable("planner")
        config.set_grant(self.root, "planner", "observe")
        worker = self.agent("worker")
        loader.reset()
        module = self.module_of("planner")
        found = self.entry("planner")
        registered = loader.load(found).registrations
        with self.subTest("an observe column"), mock.patch("multiverse.tools.send.run") as send:
            row = {"id": worker.name, "universe": str(self.root)}
            with self.assertRaisesRegex(api.AccessDenied, "planner's column sent is declared observe; send needs act"):
                registered.columns["sent"].value(row)
            send.assert_not_called()
        with self.subTest("an observe function"), mock.patch("multiverse.tools.send.run") as send:
            config.set_grant(self.root, "planner", "act")  # even with act granted
            with self.assertRaisesRegex(api.AccessDenied, "planner's peek is declared observe; send needs act"):
                [item for item in module.RAN if callable(item)][0](self.root)
            send.assert_not_called()
            config.set_grant(self.root, "planner", "observe")
        with self.subTest("commands"):
            module.RAN.clear()
            self.assertEqual(registered.commands["planner"].run(self.root), {"agents": ["worker"]})
            with self.assertRaisesRegex(api.AccessDenied, "planner has observe access here; its command "
                                                          "planner-install needs act. A person can grant it with: "
                                                          "mv2 plugins grant planner act"):
                registered.commands["planner-install"].run(self.root)
            self.assertEqual(module.RAN, ["plan"])  # the act command never ran
        with self.subTest("a helper action: exit 1, never the action's own 2"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, {"MV2_UNIVERSE": str(self.root)}):
                code = helper.main(["-u", str(self.root), "run", "planner", "install"])
            self.assertEqual(code, 1)
            self.assertIn("mv2 plugins grant planner act", err.getvalue())
            self.assertEqual(module.RAN, ["plan"])  # it never ran
            self.assertEqual(loader.errors().get("planner", []), [])  # a refusal, not a plugin that broke
        with self.subTest("launch contributions need act"):
            plan = harness.launch_plan(harness.LaunchInfo(self.root, "a1", "worker", "spawn", "claude", "claude",
                                                          version=(2, 1, 310), bridge=True))
            self.assertEqual(plan.plugins["planner"]["state"], "skipped")
            self.assertNotIn("PLANNER_ON", plan.environment)
            self.assertTrue(any(problem.startswith("plugin planner was left out: it has observe access here; launch "
                                                   "contributions need act") for problem in plan.problems), plan.problems)
            config.set_grant(self.root, "planner", "act")
            plan = harness.launch_plan(harness.LaunchInfo(self.root, "a1", "worker", "spawn", "claude", "claude",
                                                          version=(2, 1, 310), bridge=True))
            self.assertEqual(plan.environment.get("PLANNER_ON"), "1")

    def test_only_a_person_grants_more_than_a_plugins_minimum(self):
        """Enabling and granting stay within a plugin's minimum and request; without a terminal, enabling records
        the minimum and says how to raise it; from an agent, enabling records at most the minimum and a grant is
        never raised, the refusal naming the command a person runs; fragile because an agent that can raise its
        own plugins' access defeats the grant, and a missed minimum hands out act by default."""
        self.planner()
        with self.subTest("bounds"):
            self.write_plugin("viewer", {"module": "plugin", "api": "1.13",
                                         "access": {"request": "spend", "minimum": "spend", "why": "it runs a model"}},
                              {"plugin.py": "def register(mv): pass\n"})
            with self.assertRaisesRegex(plugins.PluginError, "viewer needs at least spend access: it runs a model"):
                plugins.set_enabled(self.root, "viewer", True, grant="observe")
            with self.assertRaisesRegex(plugins.PluginError, "viewer asks for at most spend access"):
                plugins.set_enabled(self.root, "viewer", True, grant="act")
            self.assertFalse(self.entry("viewer").on)  # a refused grant turns nothing on
        with self.subTest("no terminal: its minimum, said"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(sys.stdin, "isatty", return_value=False):
                text = tool.cli_run(self.root, plugin_action="enable", name="planner")
            self.assertEqual(config.load(self.root).access["planner"], "observe")
            self.assertIn("with observe access", text)
            self.assertIn("mv2 plugins grant planner act", text)
            text = tool.cli_run(self.root, plugin_action="grant", name="planner", level="spend")
            self.assertEqual((config.load(self.root).access["planner"], self.mv("planner").access(self.root)),
                             ("spend", "spend"))
        with self.subTest("from an agent"), mock.patch.dict(os.environ, {"MV2_AGENT_ID": "a1"}):
            plugins.set_enabled(self.root, "planner", False)
            config.set_grant(self.root, "planner", "observe")
            with self.assertRaisesRegex(plugins.PluginError, "A person runs: mv2 plugins grant planner act"):
                plugins.set_grant(self.root, "planner", "act")
            with self.assertRaisesRegex(plugins.PluginError, "at most observe access.*mv2 plugins grant planner act"):
                plugins.set_enabled(self.root, "planner", True, grant="act")
            plugins.set_enabled(self.root, "planner", True)
            self.assertEqual(self.mv("planner").access(self.root), "observe")  # at most its minimum
            plugins.set_grant(self.root, "planner", "observe")  # lowering, or keeping, is fine
        with self.subTest("from an agent, while a person lowers it"), \
                mock.patch.dict(os.environ, {"MV2_AGENT_ID": "a1"}):
            config.set_grant(self.root, "planner", "act")
            lock, lowered = config.lock, []

            def person_first(target):  # the person's grant takes the lock just before the agent's write does
                if not lowered:
                    lowered.append(True)
                    config.set_grant(self.root, "planner", "observe")  # its own lock is the real one
                return lock(target)
            with mock.patch.object(config, "lock", person_first):
                with self.assertRaisesRegex(plugins.PluginError, "can't raise.*observe access recorded here now"):
                    plugins.set_grant(self.root, "planner", "spend")  # a lowering from act, raising from observe
            self.assertEqual(config.load(self.root).access["planner"], "observe")  # the person's lowering stays
