"""Codex release model checks without native services or account state."""
import copy
import json
from pathlib import Path
import runpy
from unittest.mock import patch
from multiverse import harnesses
from multiverse.harnesses.codex import models as m
from test_multiverse import BaseTest


class CodexModelsTest(BaseTest):
    def test_release_table_checks_efforts_without_guessing_account_or_provider(self):
        """Unsupported efforts must fail before Codex silently falls back, while account-specific IDs stay unverified."""
        adapter = harnesses.load(self.root, 'codex')
        with patch('os.environ', {'CODEX_HOME': '/never/read', 'OPENAI_API_KEY': 'not-a-credential'}):
            catalog = adapter.models(environment={'CODEX_HOME': None})
        self.assertEqual((catalog.source, catalog.version, catalog.provider, catalog.free_form), ('table', '0.164.0', None, True))
        self.assertEqual(sum(model.default for model in catalog.models), 1)
        for model in catalog.models:
            with self.subTest(model.id):
                default = adapter.check_model(model.id, environment={})
                self.assertEqual((default.ok, default.verified, default.model, default.effort),
                                 (True, True, model.id, model.default_effort))
                for effort in (*model.efforts, 'unsupported'):
                    check = adapter.check_model(model.id, effort=effort, environment={})
                    self.assertEqual(check.ok, effort in model.efforts)
        for name, effort, ok, verified in [('future-model', 'high', True, False), ('', None, False, False)]:
            with self.subTest(name):
                result = adapter.check_model(name, effort=effort, environment={})
                self.assertEqual((result.ok, result.verified), (ok, verified))

    def test_generator_refuses_unknown_release_shapes_instead_of_publishing_a_partial_table(self):
        """Release schema changes must stop model generation before silently losing effort or picker metadata."""
        generate = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/gen_codex_models.py'))['generate']
        source = self.work / 'release'
        target = source / 'codex-rs/models-manager/models.json'
        target.parent.mkdir(parents=True)
        item = {'slug': 'fixture', 'display_name': 'Fixture', 'visibility': 'list', 'priority': 0,
                'supported_in_api': True, 'supported_reasoning_levels': [{'effort': 'low'}],
                'default_reasoning_level': 'low', 'input_modalities': ['text'], 'context_window': 1000}
        target.write_text(json.dumps({'models': [item]}))
        result = generate(source, 'fixture-version')
        self.assertEqual((result['codex'], result['models'][0]['default']), ('fixture-version', True))
        for key, value in [('visibility', 'new'), ('priority', True), ('supported_in_api', 'yes'),
                           ('supported_reasoning_levels', [{'effort': 'low'}, {'effort': None}]), ('default_reasoning_level', 'high'),
                           ('input_modalities', ['audio']), ('context_window', True), ('slug', '')]:
            with self.subTest(key):
                changed = copy.deepcopy(item)
                changed[key] = value
                target.write_text(json.dumps({'models': [changed]}))
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    generate(source, 'fixture-version')
        for data in ({'models': []}, {'models': [item, item]}, {'future': []}):
            with self.subTest(data):
                target.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    generate(source, 'fixture-version')
