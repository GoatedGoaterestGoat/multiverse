"""`mv2 catalog --json` lists the same entries as the text output, for scripts and the Claude typeahead."""
import contextlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import cli
from multiverse.store import write
from multiverse.tools import _common, catalog
from multiverse.universe import catalog as universe_catalog
from test_multiverse import BaseTest


class CatalogJsonTest(BaseTest):
    def setUp(self):
        super().setUp()
        # An entry without a description, which both forms must show as empty.
        write(self.root / 'commands/bare/entry.json', {})
        (self.root / 'commands/bare/prompt.md').write_text('Bare.')

    def invoke(self, main, *args):
        """What an entry point prints for the command line `args`, in this process; it must not fail."""
        out = io.StringIO()
        with patch.object(sys, 'argv', ['mv2', *args]), contextlib.redirect_stdout(out):
            try:
                main()
            except SystemExit as exit:
                self.assertEqual(exit.code or 0, 0, out.getvalue())
        return out.getvalue()

    def test_json_lists_every_entry_with_kind_name_description_and_path(self):
        """catalog --json rows carry kind, name, description and path, with an empty description for a bare entry; a
        break goes unnoticed because the typeahead's TS tests parse a canned string, so a renamed key passes both
        suites while the typeahead lists nothing."""
        entries = universe_catalog.catalog(self.root)
        self.assertTrue(entries)
        forms = (('mv2 catalog', cli.main, ['-u', str(self.root), 'catalog', '--json']),
                 ('python -m multiverse.tools.catalog', lambda: _common.main(catalog.configure, catalog.run),
                  ['-u', str(self.root), '--json']))
        for form, main, args in forms:
            with self.subTest(form):
                listed = json.loads(self.invoke(main, *args))
                self.assertEqual(listed, entries)
                for entry in listed:
                    self.assertEqual(sorted(entry), ['description', 'kind', 'name', 'path'])
        bare = next(entry for entry in listed if entry['name'] == 'bare')
        self.assertEqual((bare['kind'], bare['description']), ('commands', ''))
        self.assertEqual(catalog.run(self.root, json=True), entries)
