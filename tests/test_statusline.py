"""statusline.json: shipped by every template and managed by `mv2 refresh`."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import universe
from multiverse.store import read, write

# The mod's defaults (hooks/statusline.ts DEFAULTS, asserted there too), in display order.
DEFAULT_ITEMS = {'agent': False, 'phase': True, 'summary': False, 'inbox': True, 'compaction': True}
TEMPLATES = ('base', 'development', 'testing')


class StatusLineFileTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def install(self, template):
        return universe.create(self.work / template / '.multiverse', 'demo', template, self.work)

    def changes(self, root):
        return [(change['action'], change['reason']) for change in universe.refresh_plan(root, None, ())['changes']
                if change['path'] == 'statusline.json']

    def test_every_template_ships_the_agreed_defaults_in_display_order(self):
        for template in TEMPLATES:
            with self.subTest(template=template):
                data = json.loads((self.install(template) / 'statusline.json').read_text())
                self.assertEqual(data['schema'], 1)
                self.assertEqual(data['items'], DEFAULT_ITEMS)
                self.assertEqual(list(data['items']), list(DEFAULT_ITEMS))
                self.assertEqual(set(data), {'schema', 'items'})

    def test_a_current_universe_has_nothing_to_refresh(self):
        for template in TEMPLATES:
            with self.subTest(template=template):
                self.assertEqual(self.changes(self.install(template)), [])

    def test_refresh_adds_the_file_to_a_universe_installed_before_it_existed(self):
        root = self.install('base')
        manifest = read(root / universe.MANIFEST)
        manifest['files'].pop('statusline.json')
        write(root / universe.MANIFEST, manifest)
        (root / 'statusline.json').unlink()
        plan = universe.refresh_plan(root, None, ())
        self.assertEqual(self.changes(root), [('add', 'new in the template')])
        universe.refresh_apply(root, plan)
        self.assertEqual(json.loads((root / 'statusline.json').read_text())['items'], DEFAULT_ITEMS)
        self.assertEqual(self.changes(root), [])

    def test_a_local_choice_survives_refresh_and_a_deleted_file_stays_deleted(self):
        root = self.install('base')
        mine = {'schema': 1, 'items': {'agent': True, 'inbox': False}}
        write(root / 'statusline.json', mine)
        self.assertEqual(self.changes(root), [('local', 'edited locally; template unchanged')])
        universe.refresh_apply(root, universe.refresh_plan(root, None, ()))
        self.assertEqual(json.loads((root / 'statusline.json').read_text()), mine)
        (root / 'statusline.json').unlink()
        self.assertEqual(self.changes(root), [('deleted', 'deleted locally; kept deleted')])


if __name__ == '__main__':
    unittest.main()
