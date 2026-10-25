"""The update pass changes old acceptances only with exact native message-ID evidence."""
import json
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.runtime import native
from multiverse.store import read, write
from multiverse.tools import plugins, update
from multiverse.universe import mailbox
from test_multiverse import BaseTest, ROOT


class ReceiptMigrationTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.session = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.home = self.work / 'native-home'
        self.transcript = self.home / 'projects' / 'fixture' / f'{self.session}.jsonl'
        self.transcript.parent.mkdir(parents=True)
        write(self.directory / 'properties.json', {'id': self.agent, 'name': 'reader', 'harness': 'claude',
              'workspace': str(self.work), 'native_session_id': self.session, 'native_session_home': str(self.home)})
        write(self.directory / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'stopped'})

    def acceptance(self, **changes):
        message = mailbox.append_message(self.directory, 'identical body', {}, delivery='steer', target_session=self.session)
        mailbox.update_message(self.directory, message['id'], submission_state='accepted', delivery_method='native',
                               attempts=[{'native_session_id': self.session}], **changes)
        return message

    def row(self, message):
        return {'type': 'attachment', 'sessionId': self.session, 'uuid': str(uuid4()),
                'attachment': {'type': 'queued_command', 'prompt': 'identical body',
                               'origin': {'kind': 'peer', 'msg_id': message['id']}}}

    def test_the_old_updater_reaches_the_once_only_exact_id_pass(self):
        """An updater already loaded before the upgrade must reach new receipt code, without guessing from text or replaying."""
        exact = self.acceptance()
        text_only = self.acceptance()
        receipt = self.acceptance(native_receipt={'receipt': 'already tracked'})
        wake = self.acceptance()
        mailbox.update_message(self.directory, wake['id'], delivery_method='resume_prompt')
        unknown = self.acceptance()
        mailbox.update_message(self.directory, unknown['id'], submission_state='unknown')
        foreign = self.acceptance()
        mailbox.update_message(self.directory, foreign['id'], target_session=str(uuid4()))
        self.transcript.write_text('\n'.join(json.dumps(self.row(item)) for item in (exact, receipt, wake, unknown, foreign))
                                   + '\n' + json.dumps({'type': 'user', 'message': {'content': 'identical body'}}) + '\n')
        before = {item['id']: mailbox.message_record(self.directory, item['id'])[1]
                  for item in (exact, text_only, receipt, wake, unknown, foreign)}
        # This shipped updater entry runs the new checkout's migration command in a new process.
        result = update._convert_settings(ROOT, self.root)
        self.assertEqual(result['state'], 'none', result)
        confirmed = mailbox.message_record(self.directory, exact['id'])[1]
        self.assertIs(confirmed.get('native_receipt', {}).get('transcript_confirmed'), True)
        self.assertEqual(confirmed['attempts'], before[exact['id']]['attempts'])
        for item in (text_only, receipt, wake, unknown, foreign):
            self.assertEqual(mailbox.message_record(self.directory, item['id'])[1], before[item['id']])
        late = self.acceptance()
        with self.transcript.open('a') as stream:
            stream.write(json.dumps(self.row(late)) + '\n')
        unchanged = (self.directory / 'inbox-state.json').read_bytes()
        update._convert_settings(ROOT, self.root)
        self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), unchanged)

    def test_migration_keeps_uncertainty_and_concurrent_receipts_and_never_submits(self):
        """A one-time transcript scan must not overwrite newer evidence, infer a home, or turn unknown into loss."""
        from multiverse.runtime import receipt_migration
        from multiverse.harnesses.types import NativeResult
        message = self.acceptance()
        with patch.object(native, 'context', side_effect=AssertionError('no native connection')), \
                patch.object(native, 'send_or_wake', side_effect=AssertionError('no native submission')):
            for verdict in (NativeResult('unknown'), NativeResult('ok', value={'delivered': False}),
                            NativeResult('error', 'unreadable')):
                with self.subTest(verdict=verdict.state), patch.object(harnesses.ClaudeAdapter, 'message_id_evidence', return_value=verdict):
                    receipt_migration.migrate(self.root)
                    self.assertIsNone(mailbox.message_record(self.directory, message['id'])[1].get('native_receipt'))
                    (self.root / receipt_migration.MARKER).unlink()
            def changed(*args):
                mailbox.update_message(self.directory, message['id'], native_receipt={'concurrent': True})
                return NativeResult('ok', value={'delivered': True})
            with patch.object(harnesses.ClaudeAdapter, 'message_id_evidence', side_effect=changed):
                receipt_migration.migrate(self.root)
            self.assertEqual(mailbox.message_record(self.directory, message['id'])[1]['native_receipt'], {'concurrent': True})
        (self.root / receipt_migration.MARKER).unlink()
        properties = read(self.directory / 'properties.json')
        properties.pop('native_session_home')
        write(self.directory / 'properties.json', properties)
        mailbox.update_message(self.directory, message['id'], native_receipt=None)
        with patch.object(harnesses.ClaudeAdapter, 'message_id_evidence', side_effect=AssertionError('no caller-home guess')) as guessed:
            receipt_migration.migrate(self.root)
        guessed.assert_not_called()
        self.assertIsNone(mailbox.message_record(self.directory, message['id'])[1]['native_receipt'])
        (self.root / receipt_migration.MARKER).unlink()
        write(self.directory / 'properties.json', {**properties, 'native_session_home': str(self.home)})
        with patch.object(harnesses.ClaudeAdapter, 'message_id_evidence') as bounded:
            limited = receipt_migration.migrate(self.root, deadline=0)
        self.assertTrue(limited['limited'])
        bounded.assert_not_called()
        self.assertIsNone(mailbox.message_record(self.directory, message['id'])[1]['native_receipt'])
        import builtins
        original_import = builtins.__import__
        def import_failure(name, *args, **kwargs):
            if name.endswith('receipt_migration'):
                raise ImportError('fixture receipt import failed')
            return original_import(name, *args, **kwargs)
        with patch.object(builtins, '__import__', side_effect=import_failure):
            self.assertNotIn('ok', plugins.run(self.root, plugin_action='migrate', apply=True))
        with patch.object(receipt_migration, 'migrate', side_effect=RuntimeError('cannot scan receipts')):
            self.assertNotIn('ok', plugins.run(self.root, plugin_action='migrate', apply=True))
            with patch.object(receipt_migration, 'migrate', side_effect=AssertionError('a plan must not migrate')) as planned:
                plugins.run(self.root, plugin_action='migrate')
            planned.assert_not_called()
