"""Removing plugin leftovers requires independent terminal absence, not guessed records."""
from pathlib import Path
from unittest.mock import patch

from multiverse.runtime import lifecycle
from multiverse.store import read, uid, write
from multiverse.terminal.tmux import Tmux
from multiverse.universe import records, relocate
from test_multiverse import BaseTest


class OrphanRemovalTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents' / uid()
        self.note = self.directory / 'compaction' / 'note.json'
        write(self.note, {'leftover': True})

    def test_record_free_folder_is_removed_to_trash_with_contents_intact(self):
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['damaged'].get('recover'), 'remove')
        with patch.object(Tmux, 'snapshot', return_value=({}, None)), \
                patch.object(Tmux, 'run', side_effect=AssertionError('no terminal action')):
            removed = relocate.remove_agent(self.root, self.directory.name)
        self.assertFalse(self.directory.exists())
        self.assertEqual(removed['id'], self.directory.name)
        self.assertEqual(read(Path(removed['trash']) / 'compaction/note.json'), {'leftover': True})

    def test_exact_tagged_terminal_or_unreadable_server_blocks_orphan_removal(self):
        pane = {'pane_id': '%1', 'session_id': '$1', 'pane_pid': '123', 'pane_dead': '0',
                '@mv2_agent_id': self.directory.name,
                '@mv2_universe_id': read(self.root / 'universe.json')['id'], '@mv2_launch_id': uid()}
        for snapshot in (({'%1': pane}, None), ({}, 'Permission denied')):
            with self.subTest(snapshot=snapshot), patch.object(Tmux, 'snapshot', return_value=snapshot), \
                    patch.object(Tmux, 'run', side_effect=AssertionError('no terminal action')), \
                    self.assertRaises(records.RecordError):
                relocate.remove_agent(self.root, self.directory.name)
            self.assertTrue(self.note.exists())
            self.assertFalse((self.root / 'trash').exists())

    def test_new_core_evidence_during_discovery_is_not_moved(self):
        def snapshot(backend):
            write(self.directory / 'runtime.json', {'state': 'starting'})
            return {}, None
        with patch.object(Tmux, 'snapshot', snapshot), self.assertRaisesRegex(records.RecordError, 'changed'):
            relocate.remove_agent(self.root, self.directory.name)
        self.assertTrue(self.note.exists())
        self.assertTrue((self.directory / 'runtime.json').exists())
        self.assertFalse((self.root / 'trash').exists())

    def test_surviving_core_evidence_never_gets_remove_recovery_hint(self):
        for name in ('runtime.json', 'sessions.jsonl', 'launches'):
            path = self.directory / name
            with self.subTest(name=name):
                path.mkdir() if name == 'launches' else path.write_text('{}\n')
                try:
                    row = lifecycle.listing(self.root)[0]
                    self.assertNotEqual(row['damaged'].get('recover'), 'remove')
                    with self.assertRaises(records.RecordError):
                        relocate.remove_agent(self.root, self.directory.name)
                finally:
                    path.rmdir() if name == 'launches' else path.unlink()
