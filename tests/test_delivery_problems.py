"""Definite failures remain visible without retrying them or leaking message text."""
from unittest.mock import patch
from multiverse.runtime import lifecycle, native
from multiverse.store import uid, write
from multiverse.tools import deliver, doctor, inspect, list_agents, send, inbox
from multiverse.universe import mailbox
from multiverse import harnesses
from multiverse.harnesses.types import SessionHandle, NativeEndpoint
from test_multiverse import BaseTest


class DeliveryProblemTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state' / 'agents' / uid()
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'recipient',
            'harness': 'claude', 'workspace': str(self.work), 'native_session_id': uid()})
        write(self.directory / 'runtime.json', {'launch_id': uid(), 'state': 'stopped'})

    def message(self, state):
        message = mailbox.append_message(self.directory, 'PRIVATE BODY ' + state, {}, delivery='steer')
        mailbox.update_message(self.directory, message['id'], submission_state=state,
            attempts=[{'state': 'error' if state == 'failed' else state, 'ok': False,
                       'reason': 'Harness startup refused', 'value': {'wake_refused': True}}])
        return message

    def test_check_reports_failed_messages_without_native_reconciliation_or_resend(self):
        self.message('failed')
        with patch.object(native, 'deliver', side_effect=AssertionError('failed receipt is already definite')), \
             patch.object(native, 'send_or_wake', side_effect=AssertionError('check must not retry')):
            result = deliver.run(self.root, agent='recipient', check=True)
        self.assertFalse(result['ok'], result)
        self.assertEqual(len(result['messages']), 1)
        self.assertIn('Harness startup refused', result['messages'][0]['reason'])

    def test_list_inspect_doctor_show_failed_count_and_retry_command(self):
        for state in ('failed', 'failed', 'unknown', 'accepted', 'cancelled'):
            self.message(state)
        expected = '2 messages not delivered: mv2 deliver recipient --retry'
        self.assertIn(expected, list_agents.run(self.root, wide=True))
        self.assertIn(expected, inspect.cli_run(self.root, agent='recipient'))
        self.assertTrue(any(expected in reason for reason in doctor.run(self.root)['problems']))
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['delivery_problem'], expected)
        self.assertNotIn('PRIVATE BODY', expected)

    def test_inbox_damage_names_full_file_and_repair_action(self):
        path = self.directory / 'inbox-state.json'
        for damage in ('[]', '', '{"trunc\n', '{"m":{"attempts":5}}'):
            path.write_text(damage)
            for options in ({'all_messages': True}, {'peek': True}):
                with self.subTest(options=options, damage=damage), self.assertRaises(ValueError) as caught:
                    inbox.run(self.root, agent='recipient', **options)
                self.assertIn(str(path), str(caught.exception))
                self.assertIn('repair', str(caught.exception).lower())
                self.assertIn('reset', str(caught.exception).lower())

    def test_codex_plain_marker_does_not_classify_starting_or_lost_bridge(self):
        adapter = harnesses.CodexAdapter.configured('codex', {'inbox_bridge': False})
        for mode, relay in (('plain', None), ('bridge', None), ('bridge', {'state': 'lost', 'reason': 'lost'})):
            session = SessionHandle(self.directory.name, 'codex', str(self.work), state='starting',
                native_mode={'mode': mode, 'policy': 'auto'}, native_relay=relay)
            with self.subTest(mode=mode, relay=relay):
                capability = adapter.capabilities(session)['send_steer']
                self.assertEqual(capability.mechanism == 'launch-only', mode == 'plain')
