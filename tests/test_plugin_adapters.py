"""Harness adapters applying plugin launch plans: Claude and Pi here; Codex in its own tests."""
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import harnesses
from multiverse.harnesses.types import LaunchContext, SessionHandle
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write

GUARD = """
from multiverse.plugins import api

def register(mv):
    @mv.launch.contribute("claude")
    def claude(info):
        return api.Contribution(environment={"MV2_GUARD": "1"},
                                claude_settings={"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": "guard"}]}]}},
                                problem=None if info.version >= (2, 1, 295) else "Launched without guard: Claude too old",
                                mode=None if info.version >= (2, 1, 295) else "without guard")
"""


class PluginLaunchTest(PluginTestCase):
    bundled = True  # the bundled typeahead is part of what Claude launches load

    def setUp(self):
        super().setUp()
        self.agent_dir = self.root / "state" / "agents" / str(uuid4())
        self.agent_dir.mkdir(parents=True)
        self.write_plugin("pi-tools", {"permissions": ["harness.pi"], "harnesses": {"pi": [
            {"extension": "pi/tools.ts"}, {"extension": "pi/bridged.ts", "needs_bridge": True}]}})
        self.write_plugin("guard", {"module": "plugin", "extends": ["launch"], "permissions": ["harness.claude"],
                                    "harnesses": {"claude": [{"dir": "claude", "min_version": "2.1.290"}]}},
                          {"plugin.py": GUARD})
        # The base template's compaction.json turns the bundled compaction plugin on; these tests are about
        # typeahead, guard and pi-tools.
        self.enable("compaction", on=False)

    def bridge(self, on):
        path = self.root / "harnesses" / "pi" / "harness.json"
        data = read(path)
        data.setdefault("settings", {})["inbox_bridge"] = on
        write(path, data)

    def spawn(self):
        values = {"name": "worker", "agent_id": self.agent_dir.name, "session_id": "", "bootstrap": "b",
                  "bootstrap_json": '"b"', "task": "--literal task", "agent_dir": str(self.agent_dir),
                  "harness_dir": str(self.root / "harnesses" / "pi"),
                  "session_file": str(self.agent_dir / "session.jsonl"), "bootstrap_file": "/launch/bootstrap.md",
                  "task_file": "/launch/task.md", "workspace": str(self.work), "model": "",
                  "control_socket": "/tmp/owned/rpc.sock"}
        return harnesses.load(self.root, "pi").spawn(LaunchContext("spawn", values, resume_prompt="--hello"))

    def resume(self, prompt=None):
        session_file = self.agent_dir / "session.jsonl"
        native = str(uuid4())
        session_file.write_text(json.dumps({"type": "session", "id": native}) + "\n")
        values = {"name": "worker", "agent_id": self.agent_dir.name, "session_id": native, "bootstrap": "b",
                  "bootstrap_json": '"b"', "task": prompt or "", "agent_dir": str(self.agent_dir),
                  "harness_dir": str(self.root / "harnesses" / "pi"), "session_file": str(session_file),
                  "bootstrap_file": "/launch/bootstrap.md", "task_file": "/launch/task.md", "workspace": str(self.work),
                  "model": "", "control_socket": "/tmp/owned/rpc.sock"}
        session = SessionHandle(self.agent_dir.name, "pi", str(self.work), native_id=native,
                                session_file=str(session_file))
        return harnesses.load(self.root, "pi").resume(session, LaunchContext("resume", values, resume_prompt=prompt))

    def launch(self, bridge=None, version=(2, 1, 296)):
        path = self.root / "harnesses" / "claude" / "harness.json"
        data = read(path)
        data["settings"].pop("inbox_bridge", None)
        if bridge is not None:
            data["settings"]["inbox_bridge"] = bridge
        write(path, data)
        values = {"name": "worker", "agent_id": self.agent_dir.name, "session_id": str(uuid4()), "bootstrap": "b",
                  "bootstrap_json": '"b"', "task": "--literal task", "agent_dir": str(self.agent_dir),
                  "harness_dir": "/h", "session_file": "", "bootstrap_file": "", "task_file": "",
                  "workspace": str(self.work), "model": "", "control_socket": str(self.work / "inbox.sock")}
        with patch("multiverse.harnesses.claude.claude_version", return_value=version) as probe:
            command = harnesses.load(self.root, "claude").spawn(LaunchContext("spawn", values))
        return command, probe

    def test_discovery_failure_and_bad_environment_never_stop_a_launch(self):
        """Claude and Pi launches, bridge on or off, still start when plugin discovery raises, and a plugin's
        unusable environment leaves only it out; a break goes unnoticed because suites run with healthy plugins, and
        when it breaks no agent can start."""
        self.enable("guard")
        self.enable("pi-tools")
        with patch.object(loader, "active", side_effect=RuntimeError("discovery failed")):
            for bridge in (True, False):
                with self.subTest("claude", bridge=bridge):
                    command, _ = self.launch(bridge=bridge)
                    self.assertEqual(command.plugins["error"],
                                     "plugins were left out of this launch: RuntimeError: discovery failed")
                    self.assertEqual(command.argv.count("--plugin-dir"), 1 if bridge else 0)
                with self.subTest("pi", bridge=bridge):
                    self.bridge(bridge)
                    command = self.spawn()
                    self.assertNotIn("pi/tools.ts", " ".join(command.argv))
                    self.assertIn("discovery failed", command.plugins["error"])
        with self.subTest("an environment value no process can take"):
            self.write_plugin("nul", {"module": "plugin", "extends": ["launch"]}, {
                "plugin.py": "from multiverse.plugins import api\n"
                             "def register(mv): mv.launch.contribute('pi', lambda info: api.Contribution("
                             "environment={'NUL_VALUE': 'a\\x00b'}))\n"})
            self.enable("nul")
            command = self.spawn()
            self.assertNotIn("NUL_VALUE", command.environment)
            self.assertEqual(command.plugins["plugins"]["nul"]["state"], "skipped")

    def test_pi_plugin_extensions_go_before_the_prompt(self):
        """Pi plugin --extension flags go before -- on spawn, resume and wake, and no plugin leaves argv unchanged;
        fragile because each path builds argv separately, and a prompt like --wake up placed before -- is parsed by
        Pi as a flag."""
        tools = str((self.plugins_dir / "pi-tools" / "pi" / "tools.ts").resolve())
        bridged = str((self.plugins_dir / "pi-tools" / "pi" / "bridged.ts").resolve())
        for on in (False, True):
            with self.subTest("no plugin leaves argv unchanged", bridge=on):
                self.bridge(on)
                command = self.spawn()
                self.enable("pi-tools")
                with_plugin = self.spawn()
                self.enable("pi-tools", on=False)
                self.assertEqual(self.spawn().argv, command.argv)
                self.assertEqual(command.plugins, {"plugins": {}, "status_items": []})
                self.assertNotEqual(with_plugin.argv, command.argv)
        self.enable("pi-tools")
        with self.subTest("plain and bridged spawns"):
            self.bridge(False)
            plain = self.spawn()
            self.assertEqual(plain.argv[:3], (plain.argv[0], "--extension", tools))
            self.assertNotIn(bridged, plain.argv)
            self.assertIsNone(plain.control_endpoint)
            self.assertEqual(plain.plugins["plugins"]["pi-tools"], {
                "version": "0.1.0", "state": "loaded", "added": ["pi_extensions"],
                "unfit": ["needs mv2's bridge, which this launch does not use"]})
            self.bridge(True)
            command = self.spawn()
            own = str(harnesses.PACKAGES / "pi" / "extension.ts")
            self.assertEqual(command.argv[1:7], ("--extension", own, "--extension", tools, "--extension", bridged))
            self.assertEqual(command.plugins["plugins"]["pi-tools"], {"version": "0.1.0", "state": "loaded",
                                                                      "added": ["pi_extensions"]})
            if "--" in command.argv:
                self.assertLess(command.argv.index(bridged), command.argv.index("--"))
            self.assertEqual(command.control_endpoint.address, "/tmp/owned/rpc.sock")
        for bridge in (False, True):
            for prompt in (None, "--wake up"):
                with self.subTest("resume and wake", bridge=bridge, prompt=prompt):
                    self.bridge(bridge)
                    command = self.resume(prompt)
                    self.assertIn(tools, command.argv)
                    if "--" in command.argv:
                        self.assertLess(command.argv.index(tools), command.argv.index("--"))
                        self.assertEqual(command.argv[command.argv.index("--") + 1:], (prompt,))

    def test_plain_claude_launches_load_no_plugin_directory_and_probe_no_version(self):
        """Bridged Claude launches get plugin dirs and settings merged beside mv2's own, and the plugin's problem and
        mode joined to the launch's, while plain launches get none and never probe the version; fragile because plain
        is the fallback for Claudes that cannot take these flags and is rarely exercised."""
        self.enable("guard")
        with self.subTest("bridged"):
            command, _ = self.launch()
            directories = [command.argv[index + 1] for index, item in enumerate(command.argv) if item == "--plugin-dir"]
            # In plugin-name order, after the mod.
            self.assertEqual(directories[1:], [str((self.plugins_dir / "guard" / "claude").resolve()),
                                               str(harnesses.PACKAGES.parent / "plugins" / "builtin" / "typeahead"
                                                   / "claude")])
            settings = json.loads(command.argv[command.argv.index("--settings") + 1])
            self.assertEqual(settings["crossSessionInbound"], "accept")
            self.assertEqual(settings["hooks"]["PreCompact"][0]["hooks"][0]["command"], "guard")
            self.assertEqual(command.environment["MV2_GUARD"], "1")
            self.assertEqual(command.environment["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"], "1")
            self.assertEqual(command.native_mode["mode"], "bridge")
        with self.subTest("a plugin's problem and mode join the launch's"):
            command, _ = self.launch(version=(2, 1, 292))
            self.assertEqual(command.native_mode["mode"], "bridge without guard")
            self.assertEqual(command.native_mode["fallback_reason"], "Launched without guard: Claude too old")
        with self.subTest("plain"):
            command, probe = self.launch(bridge=False)
            probe.assert_not_called()
            self.assertNotIn("--plugin-dir", command.argv)
            self.assertNotIn("--settings", command.argv)
            self.assertEqual(command.plugins["plugins"]["guard"]["state"], "skipped")
            self.assertEqual(command.plugins["plugins"]["typeahead"]["state"], "skipped")
            self.assertNotIn("MV2_GUARD", command.environment)  # all of guard is left out, its contribution too
            old, _ = self.launch(version=(2, 1, 270))  # auto falls back to plain on an old Claude
            self.assertEqual(old.native_mode["mode"], "plain")
            self.assertNotIn("--plugin-dir", old.argv)
