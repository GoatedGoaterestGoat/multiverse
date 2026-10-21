"""Removing plugin leftovers requires independent terminal absence, not guessed records."""
from pathlib import Path
from unittest.mock import patch

from multiverse.runtime import lifecycle
from multiverse.runtime import attachments
from multiverse.store import read, uid, write
from multiverse.terminal.tmux import Tmux
from multiverse.tools import list_agents
from multiverse.universe import records, relocate
from test_multiverse import BaseTest


class OrphanRemovalTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents' / uid()
        self.note = self.directory / 'compaction' / 'note.json'
        write(self.note, {'leftover': True})

    def test_an_orphan_folder_is_removed_only_with_independent_proof(self):
        """A record-free agent folder goes to trash intact only when no exact-tagged terminal or unreadable server
        suggests a live launch, and surviving core evidence never gets the remove hint; fragile because the folder has
        no record to prove absence, and a wrong removal trashes a running agent; a torn runtime must not hide the
        only surviving process proof in its cleanup journal (bd3d410)."""
        pane = {'pane_id': '%1', 'session_id': '$1', 'pane_pid': '123', 'pane_dead': '0',
                '@mv2_agent_id': self.directory.name,
                '@mv2_universe_id': read(self.root / 'universe.json')['id'], '@mv2_launch_id': uid()}
        for snapshot in (({'%1': pane}, None), ({}, 'Permission denied')):
            with self.subTest(snapshot=snapshot), patch.object(Tmux, 'snapshot', return_value=snapshot), \
                    patch.object(Tmux, 'run', side_effect=AssertionError('no terminal action')), \
                    self.assertRaises(records.RecordError) as caught:
                relocate.remove_agent(self.root, self.directory.name)
            if snapshot[0]:
                self.assertIn('%1', str(caught.exception))
                self.assertNotIn('restore', str(caught.exception).lower())
                self.assertNotIn('runtime.json', str(caught.exception))
            self.assertTrue(self.note.exists())
            self.assertFalse((self.root / 'trash').exists())
        for name in ('runtime.json', 'sessions.jsonl', 'launches/' + uid() + '/ready'):
            path = self.directory / name
            with self.subTest(evidence=name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}\n')
                try:
                    row = lifecycle.listing(self.root)[0]
                    self.assertNotEqual(row['damaged'].get('recover'), 'remove')
                    with patch.object(Tmux, 'snapshot', return_value=({'%1': pane}, None)), \
                            self.assertRaises(records.RecordError):
                        relocate.remove_agent(self.root, self.directory.name)
                finally:
                    path.unlink()
                    if path.name == 'ready':
                        # The next cleanup scan creates .connections.lock in every
                        # earlier empty launch it visits; do not share that fixture.
                        path.parent.rmdir()
        with self.subTest('a torn runtime still has a live runner in its cleanup journal'):
            runtime = self.directory / 'runtime.json'
            runtime.write_text('{')
            cleanup = self.directory / 'launches' / uid() / 'cleanup.json'
            write(cleanup, {'schema': 1, 'launch_id': cleanup.parent.name, 'state': 'active', 'pid': 990001})
            try:
                with patch.object(Tmux, 'snapshot', return_value=({}, None)), \
                        patch.object(attachments, '_observation', return_value=(False, None)), \
                        self.assertRaisesRegex(records.RecordError, 'still closing'):
                    relocate.remove_agent(self.root, self.directory.name)
                self.assertTrue(cleanup.exists())
                self.assertEqual(runtime.read_text(), '{')
            finally:
                runtime.unlink(missing_ok=True)
                cleanup.unlink(missing_ok=True)
                if cleanup.parent.exists():
                    cleanup.parent.rmdir()
                if not self.directory.exists():
                    write(self.note, {'leftover': True})
        with self.subTest('main lost-spawn folders contain no startup or session evidence'):
            launch = self.directory / 'launches' / uid()
            launch.mkdir(parents=True)
            (launch / 'bootstrap.md').write_text('old bootstrap')
            (launch / 'task.md').write_text('old task')
            row = lifecycle.listing(self.root)[0]
            self.assertEqual(row['damaged'].get('recover'), 'remove')
            shown = list_agents.run(self.root, wide=True)
            self.assertIn('leftover folder with no agent records', shown)
            self.assertIn(f'mv2 remove {self.directory.name}', shown)
            with patch.object(Tmux, 'snapshot', return_value=({}, None)), \
                    patch.object(Tmux, 'run', side_effect=AssertionError('no terminal action')):
                removed = relocate.remove_agent(self.root, self.directory.name)
            self.assertFalse(self.directory.exists())
            self.assertEqual(removed['id'], self.directory.name)
            self.assertEqual(read(Path(removed['trash']) / 'compaction/note.json'), {'leftover': True})
        with self.subTest('runtime-only leftovers retain process proof before removal'):
            write(self.directory / 'runtime.json', {'launch_id': uid(), 'state': 'failed', 'pid': 12345})
            original = (self.directory / 'runtime.json').read_bytes()
            for snapshot, process in ((({'%1': pane}, None), None),
                                      (({}, 'Permission denied'), None),
                                      (({}, None), 'recorded child remains live')):
                with self.subTest(snapshot=snapshot, process=process), \
                        patch.object(Tmux, 'snapshot', return_value=snapshot), \
                        patch.object(attachments, 'recorded_process_problem', return_value=process), \
                        self.assertRaises(records.RecordError):
                    relocate.remove_agent(self.root, self.directory.name)
                self.assertEqual((self.directory / 'runtime.json').read_bytes(), original)
            with patch.object(Tmux, 'snapshot', return_value=({}, None)), \
                    patch.object(attachments, 'recorded_process_problem', return_value=None), \
                    patch.object(Tmux, 'run', side_effect=AssertionError('no terminal action')):
                removed = relocate.remove_agent(self.root, self.directory.name)
            self.assertEqual((Path(removed['trash']) / 'runtime.json').read_bytes(), original)
            self.assertFalse(self.directory.exists())

    def test_new_core_evidence_during_discovery_is_not_moved(self):
        """Core evidence that appears while orphan discovery runs stops the move; fragile because it is a race with a
        starting launch that hand-testing never hits (bd3d410)."""
        def snapshot(backend):
            write(self.directory / 'runtime.json', {'state': 'starting'})
            return {}, None
        with patch.object(Tmux, 'snapshot', snapshot), self.assertRaisesRegex(records.RecordError, 'changed'):
            relocate.remove_agent(self.root, self.directory.name)
        self.assertTrue(self.note.exists())
        self.assertTrue((self.directory / 'runtime.json').exists())
        self.assertFalse((self.root / 'trash').exists())
