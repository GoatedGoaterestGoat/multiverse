"""Trust for a universe's own plugins covers every file that can run, and what runs is the trusted copy (PR #20, item 1)."""
import importlib.util
import json
import os
from pathlib import Path
import py_compile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import harness, loader, trust
from multiverse.plugins.testing import PluginTestCase

HARMLESS = "def register(mv):\n    pass\n"
HARMFUL = ("import os\nopen(os.environ['MARKER'], 'w').write('ran')\n"
           "def register(mv):\n    pass\n")


class TrustTest(PluginTestCase):
    bundled = True  # the shadowing test needs typeahead

    def setUp(self):
        super().setUp()
        self.marker = self.work / "marker"
        os.environ["MARKER"] = str(self.marker)
        self.directory = self.write_plugin("local", {"module": "plugin", "extends": ["launch"],
                                                     "permissions": ["harness.claude"],
                                                     "harnesses": {"claude": [{"dir": "claude"}]}},
                                           {"plugin.py": HARMLESS, "claude/hooks/index.ts": "export default {}\n"},
                                           where=self.root / "plugins")
        self.enable("local")

    def plant_bytecode(self):
        """A .pyc beside plugin.py that Python would trust over the source (unchecked-hash)."""
        source = self.work / "harmful.py"
        source.write_text(HARMFUL)
        cached = Path(importlib.util.cache_from_source(str(self.directory / "plugin.py")))
        cached.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(source), cfile=str(cached), dfile=str(self.directory / "plugin.py"),
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        return cached

    def test_planted_bytecode_never_runs(self):
        self.plant_bytecode()
        trust.trust(self.directory)
        loaded = loader.load(self.entry("local"))
        self.assertFalse(self.marker.exists(), "the trusted plugin.py, not the planted bytecode, must run")
        self.assertIn("plugin-cache", loaded.module.__file__)

    def test_compiled_files_and_node_modules_count(self):
        trust.trust(self.directory)
        self.assertEqual(self.entry("local").state, "on")
        for added in ("__pycache__/extra.cpython-312.pyc", "node_modules/dep/index.js"):
            with self.subTest(added):
                path = self.directory / added
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")
                loader.reset()
                self.assertEqual(self.entry("local").state, "changed")
                path.unlink()
                loader.reset()
                self.assertEqual(self.entry("local").state, "on")

    def test_trust_warns_about_compiled_files_and_node_modules(self):
        from multiverse.tools import plugins as tool
        (self.directory / "node_modules").mkdir()
        (self.directory / "node_modules" / "x.js").write_text("x")
        (self.directory / "native.so").write_bytes(b"\0")
        result = tool.run(self.root, plugin_action="trust", name="local")
        text = tool.text("trust", result)
        self.assertIn("node_modules", text)
        self.assertIn("native.so", text)

    def test_claude_runs_the_trusted_copy_and_its_writes_never_change_trust(self):
        trust.trust(self.directory)
        plan = harness.launch_plan(harness.LaunchInfo(self.root, "a", "w", "spawn", "claude", "claude",
                                                      (2, 1, 296), True))
        [claude_dir] = [path for path in plan.claude_plugin_dirs if "typeahead" not in path]
        self.assertIn("plugin-cache", claude_dir)
        (Path(claude_dir) / "tsconfig.json").write_text("{}")  # what Claude writes into a directory it loads
        loader.reset()
        self.assertEqual(self.entry("local").state, "on")
        self.assertFalse((self.directory / "claude" / "tsconfig.json").exists())

    def test_a_copy_that_does_not_match_is_never_used(self):
        value = trust.trust(self.directory)
        (self.directory / "plugin.py").write_text(HARMFUL)  # changed after trust: not copied, not run
        with self.assertRaises(ValueError):
            trust.cached(self.directory, value)
        self.assertEqual(self.entry("local").state, "changed")
        self.assertFalse(self.marker.exists())

    def test_an_untrusted_universe_plugin_never_hides_another_source(self):
        self.write_plugin("typeahead", {"module": "plugin"}, {"plugin.py": HARMFUL}, where=self.root / "plugins")
        found = self.entry("typeahead")
        self.assertEqual(found.source, "bundled")
        self.assertIn("universe", [source for source, _ in found.shadowed])
        self.assertFalse(self.marker.exists())


class CheckTest(PluginTestCase):
    """mv2 plugins check never imports an untrusted universe plugin (PR #20, item 2)."""

    def test_an_untrusted_universe_plugin_is_checked_without_its_code(self):
        from multiverse.tools import plugins as tool
        marker = self.work / "marker"
        os.environ["MARKER"] = str(marker)
        directory = self.write_plugin("local", {"module": "plugin"}, {"plugin.py": HARMFUL},
                                      where=self.root / "plugins")
        result = tool.run(self.root, plugin_action="check", name=str(directory))
        self.assertFalse(marker.exists(), "check imported untrusted code")
        self.assertTrue(result["ok"], result)
        self.assertIn("its code was not imported: it is not trusted", " ".join(result["notes"]))
        self.assertIn("its code was not checked", tool.text("check", result))
        tool.run(self.root, plugin_action="check", name=str(directory), load=True)  # explicitly asked
        self.assertTrue(marker.exists())

    def test_a_trusted_universe_plugin_is_checked_from_its_trusted_copy(self):
        from multiverse.tools import plugins as tool
        directory = self.write_plugin("local", {"module": "plugin"}, {"plugin.py": HARMLESS},
                                      where=self.root / "plugins")
        trust.trust(directory)
        result = tool.run(self.root, plugin_action="check", name=str(directory))
        self.assertTrue(result["ok"], result)
        self.assertNotIn("not imported", " ".join(result["notes"]))
        self.assertIn("local: ready.", tool.text("check", result))
