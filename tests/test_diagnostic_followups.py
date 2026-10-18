"""Upgrade and partial-state diagnostics preserve unrelated work and finality."""
import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch
from multiverse import cli
from multiverse.runtime import lifecycle, native
from multiverse.store import lock, now, read, uid, write
from multiverse.tools import catalog, command, deliver, doctor, list_agents
from multiverse.universe import agents, mailbox
from test_multiverse import BaseTest


class DefinitionFollowupTest(BaseTest):
    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *args]):
            try:
                cli.main()
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_broken_harness_is_skipped_with_named_warning_in_catalog_and_command(self):
        path = self.root / 'harnesses/broken/harness.json'
        write(path, {})
        path.write_text('{"schema":\n')
        for args in (('catalog',), ('catalog', '--json'), ('command', 'help')):
            with self.subTest(args=args):
                code, out, err = self.call(*args)
                self.assertEqual(code, 0, err)
                self.assertTrue(out)
                self.assertIn(str(path), err)
                self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn('harnesses/broken:', self.call('catalog')[1])

    def test_unknown_schema_is_named_and_other_problems_remain(self):
        path = self.root / 'harnesses/future/harness.json'
        write(path, {'schema': 7})
        directory = self.root / 'state/agents' / uid()
        write(directory / 'properties.json', {'id': directory.name, 'name': 'missing-workspace',
            'harness': 'claude', 'workspace': str(self.work / 'gone')})
        problems = doctor.run(self.root)['problems']
        self.assertTrue(any(str(path) in value and 'schema' in value.lower() for value in problems), problems)
        self.assertTrue(any('missing-workspace' in value for value in problems), problems)

    def test_missing_peer_error_keeps_peer_path_and_remedy_through_cli(self):
        directory = self.root / 'state/agents' / uid()
        directory.mkdir()
        code, _, error = self.call('spawn', '--name', 'other')
        self.assertEqual(code, 1)
        self.assertIn(str(directory / 'properties.json'), error)
        self.assertIn('restore or remove', error.lower())

    def test_doctor_ignores_full_starting_record_only_while_its_launch_lock_is_held(self):
        directory = self.root / 'state/agents' / uid()
        write(directory / 'properties.json', {'id': directory.name, 'name': 'preparing',
            'harness': 'claude', 'workspace': str(self.work)})
        launch_id = uid()
        write(directory / 'runtime.json', {'launch_id': launch_id, 'state': 'starting',
            'host': 'fixture', 'started_at': now(), 'native_mode': {'mode': 'plain'}})
        write(directory / 'launches' / launch_id / 'registration.json', {'stage': 'preparing terminal status'})
        with lock(directory / '.launch.lock'):
            self.assertEqual(doctor.run(self.root)['problems'], [])
        self.assertTrue(doctor.run(self.root)['problems'])
    def test_progress_and_terminal_writes_preserve_preparing_but_generation_or_state_changes_do_not(self):
        directory = self.root / 'state/agents' / uid()
        facts = {'launch_id': uid(), 'state': 'starting', 'host': 'fixture'}
        write(directory / 'runtime.json', facts)
        (directory / 'launches' / facts['launch_id']).mkdir(parents=True)
        with lock(directory / '.launch.lock'):
            for changes, expected in (({'observed_at': now(), 'native_versions': {'cli': 'fixture'}}, True),
                                      ({'launch_id': uid()}, False), ({'state': 'failed'}, False),
                                      ({'tmux': {'socket': str(self.work / 'absent.sock'),
                                                 'session': '$1', 'pane': '%1'}}, True)):
                with self.subTest(changes=changes):
                    write(directory / 'runtime.json', {**facts, **changes})
                    self.assertEqual(agents.launch_in_progress(directory, facts), expected)



class RefusalUpgradeTest(BaseTest):
    def setUp(self):
        super().setUp()
        fixture = Path(__file__).parent / 'fixtures/delivery/main-52b9df4-launch-only.json'
        self.record = json.loads(fixture.read_text())
        attempt = self.record['tracking']['attempts'][-1]
        self.directory = self.root / 'state/agents' / attempt['agent_id']
        self.path = self.directory / 'inbox-state.json'
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'recipient',
            'harness': 'claude', 'task': '', 'universe_id': read(self.root/'universe.json')['id'],
            'workspace': str(self.work), 'native_session_id': attempt['native_session_id']})
        write(self.directory / 'runtime.json', {'launch_id': attempt['launch_id'], 'state': 'running',
            'native_mode': {'mode': 'plain', 'policy': 'plain'},
            'tmux': {'socket': str(self.work / 'absent.sock'), 'pane': '%1', 'session': '$1'}})
        (self.directory / 'inbox.jsonl').write_text(json.dumps(self.record['message']) + '\n')
        write(self.path, {self.record['message']['id']: self.record['tracking']})
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'alive'}))

    def test_main_written_refusal_is_not_counted_and_counting_does_not_migrate(self):
        before = self.path.read_bytes()
        self.assertNotIn('not delivered', list_agents.run(self.root, wide=True))
        self.assertFalse(any('not delivered' in reason for reason in doctor.run(self.root)['problems']))
        self.assertEqual(self.path.read_bytes(), before)

    def test_accepted_uncertain_and_failed_wakes_keep_their_existing_evidence(self):
        message = self.record['message']
        tracking = self.record['tracking']
        for state in ('accepted', 'unknown', 'submitting'):
            with self.subTest(state=state):
                current = {**tracking, 'submission_state': state}
                write(self.path, {message['id']: current})
                before = self.path.read_bytes()
                self.assertIsNone(native.finalize_launch_only(self.root, self.directory, message['id']))
                self.assertEqual(self.path.read_bytes(), before)
        failed = {**tracking, 'delivery_method': 'resume_prompt'}
        write(self.path, {message['id']: failed})
        self.assertIn('1 message not delivered', list_agents.run(self.root, wide=True))
        before = self.path.read_bytes()
        self.assertIsNone(native.finalize_launch_only(self.root, self.directory, message['id']))
        self.assertEqual(self.path.read_bytes(), before)

    def test_retry_explains_finalization_of_the_earlier_refusal(self):
        lines = []
        with patch.object(native, 'send_or_wake', side_effect=AssertionError('final refusal must not resend')):
            result = deliver.run(self.root, agent='recipient', retry=True, _present=lines.append)
        self.assertTrue(result['ok'])
        self.assertEqual(result['messages'], [])
        self.assertIn('1 earlier refusal is final; nothing to retry', '\n'.join(lines))

    def test_reset_ledger_custom_harness_has_final_manual_advice_without_receipt_rpc(self):
        write(self.root / 'harnesses/custom/harness.json', {'schema': 1, 'spawn': [sys.executable, '-c', 'pass'],
              'resume': [sys.executable, '-c', 'pass']})
        properties = read(self.directory / 'properties.json')
        write(self.directory / 'properties.json', {**properties, 'harness': 'custom'})
        self.path.unlink()  # The old inbox message survives the explicit ledger reset.
        lines = []
        with patch.object(native, 'deliver', side_effect=AssertionError('no receipt implementation exists')):
            result = deliver.run(self.root, agent='recipient', check=True, _present=lines.append)
        self.assertTrue(any('inbox-only' in line and 'attach' in line for line in lines), lines)
        self.assertNotIn('resume it', '\n'.join(lines))
        self.assertEqual(mailbox.message_record(self.directory, self.record['message']['id'])[1]['submission_state'], 'launch-only')
        self.assertEqual(deliver.run(self.root, agent='recipient', check=True)['messages'], [])
        self.assertEqual(deliver.run(self.root, agent='recipient', retry=True)['messages'], [])
