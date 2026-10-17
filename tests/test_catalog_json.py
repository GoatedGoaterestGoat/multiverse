"""`mv2 catalog --json` lists the same entries as the text output, for scripts and the Claude typeahead."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import universe
from multiverse.store import write
from multiverse.tools import catalog
from multiverse.universe import catalog as universe_catalog
from test_multiverse import BaseTest

PROJECT = Path(__file__).resolve().parents[1]


class CatalogJsonTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src')}
        for name in [name for name in self.env if name.startswith('MV2_')]:
            self.env.pop(name)
        self.env.pop('CODEX_THREAD_ID', None)
        # An entry without a description, which both forms must show as empty.
        write(self.root / 'commands/bare/entry.json', {})
        (self.root / 'commands/bare/prompt.md').write_text('Bare.')

    def invoke(self, *args, standalone=True):
        entry = ['-m', 'multiverse.tools.catalog'] if standalone else [str(PROJECT / 'bin/mv2'), '-u', str(self.root), 'catalog']
        if standalone:
            entry += ['-u', str(self.root)]
        result = subprocess.run([sys.executable, *entry, *args], env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_json_lists_every_entry_with_kind_name_description_and_path(self):
        entries = universe_catalog.catalog(self.root)
        self.assertTrue(entries)
        for standalone in (True, False):
            with self.subTest(standalone=standalone):
                listed = json.loads(self.invoke('--json', standalone=standalone))
                self.assertEqual(listed, entries)
                for entry in listed:
                    self.assertEqual(sorted(entry), ['description', 'kind', 'name', 'path'])
        bare = next(entry for entry in listed if entry['name'] == 'bare')
        self.assertEqual((bare['kind'], bare['description']), ('commands', ''))
        self.assertEqual(catalog.run(self.root, json=True), entries)

    def test_the_text_output_is_unchanged(self):
        entries = universe_catalog.catalog(self.root)
        expected = '\n'.join(f'{e["kind"]}/{e["name"]}: {e["description"]}\n  {e["path"]}' for e in entries)
        self.assertEqual(catalog.run(self.root), expected)
        for standalone in (True, False):
            with self.subTest(standalone=standalone):
                self.assertEqual(self.invoke(standalone=standalone), expected + '\n')
        self.assertIn('commands/bare: \n', expected + '\n')
