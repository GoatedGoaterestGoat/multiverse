"""A harness's models through the plugin API (1.13): Universe.harness(), models() and check()."""
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.harnesses import types
from multiverse.harnesses.claude import ClaudeAdapter
from multiverse.plugins import api, loader
from multiverse.plugins.testing import PluginTestCase

class HarnessModelsTest(PluginTestCase):
    def picker(self):
        self.write_plugin("picker", {"api": "1.13", "module": "plugin"}, {"plugin.py": "def register(mv): pass\n"})
        self.enable("picker")
        return api.Plugin(loader.load(self.entry("picker")))

    def test_models_are_read_where_the_plugin_is_on_and_never_leak_an_adapter_failure(self):
        """models() and check() run as observe where the plugin is on, a harness without models raises
        ModelsUnavailable, an adapter's failure reaches the plugin only as a fixed reason, and the adapter is
        asked with what a launch would get: a break would let a plugin read where it is off, break a settings
        screen's free-form fallback, leak a transcript row into plugin text, or resolve aliases for the wrong
        provider, all unnoticed."""
        mv = self.picker()
        settings = self.root / "harnesses" / "claude" / "harness.json"
        harness = json.loads(settings.read_text())
        harness["settings"] = {"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}
        settings.write_text(json.dumps(harness))
        seen = []

        def models(adapter, *, environment):
            seen.append(dict(environment))
            return types.ModelCatalog(models=(types.ModelInfo(id="claude-sonnet", name="Sonnet", aliases=("sonnet",)),),
                                      free_form=True, source="table", version="2.1.310", provider="bedrock")

        def check(adapter, model, *, effort=None, environment):
            return types.ModelCheck(ok=False, model="claude-sonnet", reason="claude-sonnet does not take effort max")

        with mock.patch.object(ClaudeAdapter, "models", models, create=True), \
                mock.patch.object(ClaudeAdapter, "check_model", check, create=True), \
                mock.patch.dict(os.environ, {"MV2_AGENT_ID": "someone", "CLAUDECODE": "1"}):
            claude = mv.universe(self.root).harness("claude")
            self.assertEqual(claude.info, mv.harness_info(self.root, "claude"))
            catalog = claude.models()
            self.assertIsInstance(catalog, api.ModelCatalog)
            self.assertEqual((catalog.provider, catalog.models), ("bedrock", (
                api.Model(id="claude-sonnet", name="Sonnet", aliases=("sonnet",)),)))
            self.assertEqual(claude.check("sonnet", effort="max"), api.ModelCheck(
                ok=False, model="claude-sonnet", reason="claude-sonnet does not take effort max"))
            environment = seen[-1]
            self.assertEqual(environment["CLAUDE_CODE_USE_BEDROCK"], "1")  # the harness's settings env
            self.assertIsNone(environment["CLAUDECODE"])                   # the harness removes it
            self.assertFalse([key for key in environment if key.startswith("MV2_")])

        for name, change, raised, text in (
                ("codex has none yet", "codex", api.ModelsUnavailable, "codex does not provide its models: not "
                                                                       "implemented for this harness yet"),
                ("an adapter failure", "fails", api.ModelsUnavailable, "claude's models could not be read"),
                ("an unknown harness", "nowhere", api.Error, "nowhere"),
                ("off here", "off", api.AccessDenied, "picker has no access here; reading claude's models needs "
                                                      "observe")):
            with self.subTest(name):
                def fails(adapter, *, environment):
                    raise RuntimeError('{"message": {"content": "a transcript row"}}')
                with mock.patch.object(ClaudeAdapter, "models", fails, create=True):
                    if change == "off":
                        self.enable("picker", on=False)
                        loader.reset()
                    target = {"codex": "codex", "nowhere": "nowhere"}.get(change, "claude")
                    with self.assertRaises(raised) as caught:
                        if target == "nowhere":
                            mv.universe(self.root).harness(target)
                        else:
                            mv.universe(self.root).harness(target).models()
                    self.assertIn(text, str(caught.exception))
                    self.assertNotIn("transcript row", str(caught.exception))
                    if raised is not api.ModelsUnavailable:
                        self.assertNotIsInstance(caught.exception, api.ModelsUnavailable)
