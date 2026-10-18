"""statusline.json: shipped by every template and managed by `mv2 refresh`."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse.universe import templates
from multiverse.store import read, write
from isolation import isolate

# The mod's defaults (hooks/statusline.ts DEFAULTS, asserted there too), in display order.
DEFAULT_ITEMS = {'agent': False, 'phase': True, 'summary': False, 'inbox': True, 'compaction': True}
TEMPLATES = ('base',)
# What the tmux bar may show (codex-ui's renderer owns their meaning); the Claude mod's own items are the five above.
TMUX_ITEMS = {'agent', 'harness', 'status', 'phase', 'summary', 'inbox', 'context'}


class StatusLineFileTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def install(self, template):
        return templates.create(self.work / template / '.multiverse', 'demo', template, self.work)

    def changes(self, root):
        return [(change['action'], change['reason']) for change in templates.refresh_plan(root, None, ())['changes']
                if change['path'] == 'statusline.json']

    def test_every_template_ships_the_agreed_defaults_in_display_order(self):
        for template in TEMPLATES:
            with self.subTest(template=template):
                data = json.loads((self.install(template) / 'statusline.json').read_text())
                self.assertEqual(data['schema'], 1)
                self.assertEqual(data['items'], DEFAULT_ITEMS)
                self.assertEqual(list(data['items']), list(DEFAULT_ITEMS))
                self.assertLessEqual(set(data), {'schema', 'items', 'tmux'})  # `tmux` is the tmux bar's own block
                if 'tmux' in data:
                    self.check_tmux_block(data['tmux'])

    def check_tmux_block(self, block):
        """The tmux bar's block: its own ordered items and per-harness overrides; the Claude mod ignores all of it."""
        self.assertLessEqual(set(block), {'refresh_seconds', 'items', 'harnesses'})
        if 'refresh_seconds' in block:
            self.assertIsInstance(block['refresh_seconds'], (int, float))
            self.assertGreaterEqual(block['refresh_seconds'], 1)
        for items in (block.get('items', {}), *(entry.get('items', {}) for entry in block.get('harnesses', {}).values())):
            self.assertLessEqual(set(items), TMUX_ITEMS)
            self.assertTrue(all(isinstance(value, bool) for value in items.values()))
        self.assertLessEqual(set(block.get('harnesses', {})), {'codex', 'claude', 'pi'})

    def test_a_tmux_block_is_checked_when_a_template_has_one(self):
        self.check_tmux_block({'refresh_seconds': 2, 'items': {'agent': True, 'harness': True, 'status': False},
                               'harnesses': {'codex': {'items': {'status': True, 'context': True}}}})
        with self.assertRaises(AssertionError):
            self.check_tmux_block({'items': {'compaction': True}})  # not a tmux item
        with self.assertRaises(AssertionError):
            self.check_tmux_block({'refresh_seconds': 0})

    def test_a_current_universe_has_nothing_to_refresh(self):
        for template in TEMPLATES:
            with self.subTest(template=template):
                self.assertEqual(self.changes(self.install(template)), [])

    def test_refresh_adds_the_file_to_a_universe_installed_before_it_existed(self):
        root = self.install('base')
        manifest = read(root / templates.MANIFEST)
        manifest['files'].pop('statusline.json')
        write(root / templates.MANIFEST, manifest)
        (root / 'statusline.json').unlink()
        plan = templates.refresh_plan(root, None, ())
        self.assertEqual(self.changes(root), [('add', 'new in the template')])
        templates.refresh_apply(root, plan)
        self.assertEqual(json.loads((root / 'statusline.json').read_text())['items'], DEFAULT_ITEMS)
        self.assertEqual(self.changes(root), [])

    def test_a_local_choice_survives_refresh_and_a_deleted_file_stays_deleted(self):
        root = self.install('base')
        mine = {'schema': 1, 'items': {'agent': True, 'inbox': False}}
        write(root / 'statusline.json', mine)
        self.assertEqual(self.changes(root), [('local', 'edited locally; template unchanged')])
        templates.refresh_apply(root, templates.refresh_plan(root, None, ()))
        self.assertEqual(json.loads((root / 'statusline.json').read_text()), mine)
        (root / 'statusline.json').unlink()
        self.assertEqual(self.changes(root), [('deleted', 'deleted locally; kept deleted')])


if __name__ == '__main__':
    unittest.main()
