"""Recorded model labels come from current table metadata and universe overrides, without native reads."""
from unittest.mock import patch

from multiverse import harnesses
from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import lifecycle, model_choices
from multiverse.store import read, write
from multiverse.tools import list_agents


class ModelNamesTest(PluginTestCase):
    def test_listing_labels_are_scoped_current_and_independent_of_allowed_models(self):
        """Cached or subset-filtered names can silently label a recorded model with another universe's settings."""
        def configure(root, harness, settings):
            path = root / 'harnesses' / harness / 'harness.json'
            write(path, {**read(path), 'settings': settings})

        def agent(name, model, root=None, harness='claude'):
            return self.agent(name, root=root, harness=harness, model=model)

        far = self.subverse('far')
        agent('alias', 'opus')
        agent('trimmed', ' opus ')
        agent('exact', '  opus  ')
        agent('id', 'claude-sonnet-5-5')
        agent('custom', 'private-model')
        agent('unknown', 'unlisted-model')
        agent('unset', None)
        agent('codex', 'gpt-6.1-sol', harness='codex')
        agent('far', 'opus', root=far)
        configure(self.root, 'claude', {'models': {'haiku': {}}, 'model_names': {'opus': 'Local Opus',
                                                                             'private-model': 'Private Builder',
                                                                             '  opus  ': 'Exact Spelling'}})
        configure(far, 'claude', {'model_names': {'opus': 'Other Opus'}})
        with patch.object(model_choices, 'display_names', wraps=model_choices.display_names) as names:
            rows = {row['name']: row for row in lifecycle.listing(self.root, recursive=True, usage=False)}
        self.assertEqual(names.call_count, 3, rows)
        self.assertEqual({name: row.get('model_name') for name, row in rows.items()}, {
            'alias': 'Local Opus', 'id': 'Sonnet 5.5', 'custom': 'Private Builder', 'unknown': None,
            'unset': None, 'codex': 'GPT 6.1 Sol', 'far': 'Other Opus', 'trimmed': 'Opus 5.5', 'exact': 'Exact Spelling'})
        self.assertTrue(all('damaged' not in row for row in rows.values()))
        self.assertEqual(rows['alias']['model'], 'opus')
        self.assertEqual(read(self.root / 'state' / 'agents' / rows['alias']['id'] / 'properties.json')['model'], 'opus')
        self.assertEqual(read(self.root / 'state' / 'agents' / rows['trimmed']['id'] / 'properties.json')['model'], ' opus ')
        configure(self.root, 'claude', {})
        projected = list_agents.cli_run(self.root, json=True, columns='name,model_name')
        self.assertEqual(next(row['model_name'] for row in projected if row['name'] == 'alias'), 'Opus 5.5')
        self.assertEqual(next(row['model_name'] for row in projected if row['name'] == 'custom'), None)

        # Bad display settings are diagnosed by the settings reader, but do not erase the agent's row.
        for broken in ([], None, {'opus': ''}, {'opus': True}, {'opus': 'two\nlines'}, {'': 'Unnamed'}):
            with self.subTest(broken=broken):
                configure(self.root, 'claude', {'model_names': broken})
                with self.assertRaisesRegex(ValueError, r'harnesses/claude/harness.json.*settings.model_names'):
                    harnesses.load(self.root, 'claude')
                rows = {row['name']: row for row in lifecycle.listing(self.root, usage=False)}
                self.assertIsNone(rows['alias'].get('model_name'))
                self.assertNotIn('damaged', rows['alias'])
        configure(self.root, 'claude', {'model_names': {'opus': 'Known locally'}})
        with patch.object(model_choices, '_catalog', side_effect=ValueError('unreadable table')):
            rows = {row['name']: row for row in lifecycle.listing(self.root, usage=False)}
        self.assertEqual(rows['alias'].get('model_name'), 'Known locally')
        self.assertIsNone(rows['id'].get('model_name'))
        self.assertNotIn('damaged', rows['id'])
