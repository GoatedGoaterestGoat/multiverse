from pathlib import Path
import shutil
from contextlib import ExitStack
from unittest.mock import patch
from multiverse.runtime import attachments, lifecycle, missing_runtime
from multiverse.runtime.process import process_snapshot
from multiverse.store import read, uid, write
from multiverse.universe import records, relocate, templates
from test_multiverse import BaseTest, TerminalBase

class RuntimeMoveDamageTest(TerminalBase):
    def test_remove_cannot_rename_a_live_agent_with_null_launch_identity(self):
        first = lifecycle.start(self.root, 'live-remove', '', 'fake', socket_path=self.socket,
                                wait=True, wait_timeout=15)
        self.assertTrue(first['ready'], first)
        directory = self.root / 'state' / 'agents' / first['id']
        original = read(directory / 'runtime.json')
        write(directory / 'runtime.json', {**original, 'launch_id': None})
        moved = None
        try:
            try:
                moved = relocate.remove_agent(self.root, 'live-remove')
            except ValueError as error:
                print('remove refused damaged live authority:', error, flush=True)
                return
            panes, error = self.backend.snapshot()
            self.assertIsNone(error)
            tagged = [row for row in panes.values() if row.get('@mv2_agent_id') == first['id']]
            snapshot = process_snapshot(original['pid'])
            alive = snapshot is not None and not snapshot[1].startswith('Z')
            print('removed live agent:', bool(moved), 'tagged panes:', len(tagged), 'child_alive:', alive, flush=True)
            self.fail('remove renamed the record of a live agent with damaged launch authority')
        finally:
            if moved and Path(moved['trash']).is_dir():
                Path(moved['trash']).rename(directory)
            write(directory / 'runtime.json', original)

    def test_all_four_operations_preserve_a_live_damaged_agent_and_kill_can_recover(self):
        source = templates.create(self.root / 'subverses/lab', 'lab', workdir=self.work)
        destination = templates.create(self.root / 'subverses/review', 'review', workdir=self.work)
        for scope in (source, destination):
            shutil.copytree(self.root / 'harnesses/fake', scope / 'harnesses/fake')
        launched = lifecycle.start(source, 'worker', '', 'fake', socket_path=self.socket,
                                   wait=True, wait_timeout=15)
        self.assertTrue(launched['ready'], launched)
        directory = source / 'state/agents' / launched['id']
        path = directory / 'runtime.json'
        original = read(path)
        self.addCleanup(write, path, original)
        cases = [('null generation', {**original, 'launch_id': None}),
                 ('missing generation', {key: value for key, value in original.items() if key != 'launch_id'}),
                 ('invalid generation', {**original, 'launch_id': 'not-a-uuid'}),
                 ('missing terminal', {key: value for key, value in original.items() if key != 'tmux'}),
                 ('invalid pane', {**original, 'tmux': {**original['tmux'], 'pane': None}}),
                 ('invalid process token', {**original, 'pid_started': 'invalid'}),
                 ('missing file', None)]
        operations = [('remove agent', lambda: relocate.remove_agent(source, 'worker')),
                      ('move agent', lambda: relocate.move_agent(source, 'worker', destination)),
                      ('remove subverse', lambda: relocate.remove_subverse(source)),
                      ('move subverse', lambda: relocate.move_subverse(source, destination))]
        for label, facts in cases:
            if facts is None:
                path.unlink()
            else:
                write(path, facts)
            before = path.read_bytes() if path.exists() else None
            for operation, call in operations:
                with self.subTest(damage=label, operation=operation):
                    with self.assertRaisesRegex(records.RecordError, 'runtime.json.*running terminal.*move or remove') as caught:
                        call()
                    self.assertEqual(caught.exception.file, path)
                    self.assertIn('Restore', str(caught.exception))
                    self.assertIn('mv2 kill', str(caught.exception))
                    self.assertTrue(directory.is_dir())
                    self.assertEqual(path.read_bytes() if path.exists() else None, before)
                    self.assertFalse((destination / 'state/agents' / launched['id']).exists())
                    self.assertFalse((destination / 'subverses/lab').exists())
                    self.assertFalse((self.root / 'trash').exists())
                    self.assertFalse((source / 'trash').exists())
        write(path, {**original, 'launch_id': None})
        result = lifecycle.kill(source, 'worker', attachment_only=True)
        self.assertEqual(result['state'], 'dormant')
        panes, error = self.backend.snapshot()
        self.assertIsNone(error)
        self.assertFalse(any(row.get('@mv2_agent_id') == launched['id'] for row in panes.values()))
        self.assertTrue(directory.exists())


class RelocateAuthorityBoundaryTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory, self.properties, _, _ = lifecycle._record(self.root, 'worker', '', 'codex', None, None)
        write(self.directory / 'properties.json', self.properties)
        self.path = self.directory / 'runtime.json'
        self.launch_id = uid()
        self.launch = self.directory / 'launches' / self.launch_id
        write(self.launch / 'request.json', {'env': {'MV2_TMUX_SOCKET': str(self.work / 'absent.sock')}})
        self.facts = {'launch_id': None, 'state': 'running', 'pid': 12345}
        write(self.path, self.facts)

    def test_unknown_server_or_process_refuses_without_mutating_records(self):
        before = self.path.read_bytes()
        cases = [('cannot inspect terminal server', ({}, 'Permission denied'), None),
                 ('recorded child is still running', ({}, None), 'recorded child is still running'),
                 ('process identity cannot be verified', ({}, None), 'process identity cannot be verified')]
        for expected, snapshot, process in cases:
            with self.subTest(expected=expected), \
                 patch.object(missing_runtime.Tmux, 'snapshot', return_value=snapshot), \
                 patch.object(attachments, 'recorded_process_problem', return_value=process), \
                 self.assertRaisesRegex(records.RecordError, expected):
                relocate.remove_agent(self.root, 'worker')
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse((self.root / 'trash').exists())

    def test_runtime_replacement_during_discovery_refuses(self):
        replacement = {**self.facts, 'launch_id': uid()}
        def discovery(*args, **kwargs):
            write(self.path, replacement)
            return []
        with patch.object(missing_runtime, 'require_stopped', side_effect=discovery), \
             self.assertRaisesRegex(records.RecordError, 'runtime.json.*changed during terminal discovery'):
            relocate.remove_agent(self.root, 'worker')
        self.assertEqual(read(self.path), replacement)
        self.assertFalse((self.root / 'trash').exists())

    def test_complete_absence_allows_removal_without_rewriting_damaged_facts(self):
        before = self.path.read_bytes()
        with patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)), \
             patch.object(attachments, 'recorded_process_problem', return_value=None), \
             patch.object(attachments, 'problem', return_value=None), \
             patch.object(relocate, 'owned', side_effect=AssertionError('no terminal closure from damaged facts')):
            result = relocate.remove_agent(self.root, 'worker')
        self.assertEqual((Path(result['trash']) / 'runtime.json').read_bytes(), before)
        self.assertFalse(self.directory.exists())

    def test_abandoned_minimal_preparation_is_safe_but_child_evidence_is_not(self):
        (self.launch / 'request.json').unlink()
        facts = {'launch_id': self.launch_id, 'state': 'starting', 'status': 'unknown', 'preparing': True}
        write(self.path, facts)
        before = self.path.read_bytes()
        with patch.object(missing_runtime, 'discover', side_effect=AssertionError('no discovery for minimal preparation')):
            with ExitStack() as stack:
                relocate._hold(stack, [self.directory])
        self.assertEqual(self.path.read_bytes(), before)
        write(self.launch / 'request.json', {'env': {}})
        with self.assertRaisesRegex(records.RecordError, 'runtime.json.*native startup evidence'):
            relocate.remove_agent(self.root, 'worker')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.directory.exists())

    def test_missing_runtime_without_history_still_checks_tagged_terminals(self):
        self.path.unlink()
        shutil.rmtree(self.launch)
        pane = {'pane_id': '%1', 'session_id': '$1', 'pane_pid': '12345',
                '@mv2_agent_id': self.directory.name,
                '@mv2_universe_id': self.properties['universe_id'],
                '@mv2_launch_id': self.launch_id}
        with patch.object(missing_runtime.Tmux, 'snapshot', return_value=({'%1': pane}, None)), \
             self.assertRaisesRegex(records.RecordError, 'runtime.json.*tagged terminal.*saved launch request'):
            relocate.remove_agent(self.root, 'worker')
        self.assertTrue(self.directory.exists())
        self.assertFalse(self.path.exists())
