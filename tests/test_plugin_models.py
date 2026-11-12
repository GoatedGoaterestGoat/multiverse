"""A harness's models through the plugin API (1.13): Universe.harness(), models() and check()."""
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.harnesses import types
from multiverse.harnesses.claude import ClaudeAdapter
from multiverse.harnesses.codex.plugins import CHECKED_VERSION, version_text
from multiverse import plugins
from multiverse.plugins import api, loader
from multiverse.plugins.testing import PluginTestCase

BUILDER = """
def register(mv):
    def check(values, root):
        found = mv.universe(root).harness("claude").check(values.get("model", "sonnet"))
        if not found.ok:
            raise ValueError(found.reason)
    mv.settings.check(check)
"""

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

        codex = mv.universe(self.root).harness("codex")
        catalog = codex.models()
        self.assertEqual((catalog.source, catalog.version), ("table", version_text(CHECKED_VERSION)))
        for item in catalog.models:
            check = codex.check(item.id)
            self.assertEqual((check.ok, check.verified, check.model, check.effort),
                             (True, True, item.id, item.default_effort))

        for name, change, raised, text in (
                ("pi has none yet", "pi", api.ModelsUnavailable, "pi does not provide its models: not "
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
                    target = {"pi": "pi", "nowhere": "nowhere"}.get(change, "claude")
                    with self.assertRaises(raised) as caught:
                        if target == "nowhere":
                            mv.universe(self.root).harness(target)
                        else:
                            mv.universe(self.root).harness(target).models()
                    self.assertIn(text, str(caught.exception))
                    self.assertNotIn("transcript row", str(caught.exception))
                    if raised is not api.ModelsUnavailable:
                        self.assertNotIsInstance(caught.exception, api.ModelsUnavailable)

    def test_a_persons_model_choices_limit_what_a_plugin_sees_and_judges(self):
        """models() and check() keep to the models harness.json's settings.models lists, a list that can't be used
        reaches the plugin as ModelsUnavailable naming its file, and changing the list runs plugins' settings checks
        again; fragile because the API reads models apart from spawn and run_once, so a break lets a plugin offer or
        run a model the person excluded, or keeps its settings judged by the old list, unnoticed."""
        mv = self.picker()
        path = self.root / "harnesses" / "claude" / "harness.json"

        def choose(models):
            harness = json.loads(path.read_text())
            harness.setdefault("settings", {}).pop("models", None)
            if models is not None:
                harness["settings"]["models"] = models
            path.write_text(json.dumps(harness))

        claude = mv.universe(self.root).harness("claude")
        everything = claude.models()
        with self.subTest("a list"):
            choose({"haiku": {"efforts": ["low"]}, "claude-sonnet-5-5": {}})
            catalog = claude.models()
            self.assertEqual(({item.id for item in catalog.models}, catalog.free_form),
                             ({"claude-haiku-5-5", "claude-sonnet-5-5"}, False))
            self.assertEqual(next(item.efforts for item in catalog.models if item.id == "claude-haiku-5-5"), ("low",))
            self.assertEqual((claude.check("haiku", effort="low").ok, claude.check("haiku", effort="low").model),
                             (True, "claude-haiku-5-5"))
            for model, effort in (("haiku", "high"), ("haiku", None), ("claude-opus-5-5", None)):
                refused = claude.check(model, effort=effort)
                self.assertFalse(refused.ok, (model, effort))
                self.assertTrue(refused.reason, (model, effort))
        with self.subTest("a list that can't be used"):
            choose({"claude-nothing-0": {}})
            with self.assertRaises(api.ModelsUnavailable) as caught:
                claude.models()
            self.assertIn("harness.json", str(caught.exception))
            refused = claude.check("haiku")  # nothing is allowed by a list that can't be used
            self.assertEqual((refused.ok, "harness.json" in (refused.reason or "")), (False, True))
        with self.subTest("no list"):
            choose(None)
            self.assertEqual(claude.models(), everything)
        with self.subTest("a settings check judged by the list"):
            self.write_plugin("builder", {"api": "1.13", "module": "plugin", "settings": {
                "version": 1, "schema": "settings.schema.json", "defaults": "defaults/settings.json"}}, {
                "plugin.py": BUILDER, "settings.schema.json": json.dumps(
                    {"type": "object", "properties": {"schema": {"type": "integer"}, "model": {"type": "string"}}}),
                "defaults/settings.json": json.dumps({"schema": 1, "model": "sonnet"})})
            self.enable("builder")

            def state():  # no loader.reset(): what a running dashboard or host would see
                return next(item for item in plugins.listing(self.root) if item["name"] == "builder")["state"]
            self.assertNotEqual(state(), "unusable")
            choose({"haiku": {}})
            self.assertEqual(state(), "unusable")
            choose(None)
            self.assertNotEqual(state(), "unusable")
