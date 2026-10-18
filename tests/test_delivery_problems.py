"""Definite failures remain visible without retrying them or leaking message text."""
from unittest.mock import patch
from multiverse.runtime import lifecycle, native
from multiverse.store import uid, write
from multiverse.tools import deliver, doctor, inspect, list_agents, send, inbox
from multiverse.universe import mailbox
from multiverse import harnesses
from multiverse.harnesses.types import SessionHandle, NativeEndpoint
from dataclasses import replace
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

    def test_launch_only_refusal_is_final_and_old_typed_failures_migrate(self):
        for state in ('failed', 'accepted', 'unknown', 'cancelled'):
            message = self.message(state)
            mailbox.update_message(self.directory, message['id'], native_receipt={'launch_only': True},
                attempts=[{'state': 'unavailable', 'ok': False, 'reason': 'This harness is launch-only',
                           'value': {'launch_only': True}}])
            tracking = mailbox.message_record(self.directory, message['id'])[1]
            self.assertEqual(tracking['submission_state'], 'launch-only' if state == 'failed' else state)
            if state == 'failed':
                self.assertEqual(tracking['final_reason'], 'launch-only')
        with patch.object(native, 'send_or_wake', side_effect=AssertionError('final refusal must not retry')):
            self.assertEqual(deliver.run(self.root, agent='recipient', retry=True)['messages'], [])
        self.assertNotIn('messages not delivered', list_agents.run(self.root, wide=True))

    def test_one_failed_message_is_singular_and_doctor_names_agent(self):
        self.message('failed')
        expected = '1 message not delivered: mv2 deliver recipient --retry'
        self.assertIn(expected, list_agents.run(self.root, wide=True))
        self.assertIn('recipient: ' + expected, doctor.run(self.root)['problems'])

    def test_failed_wake_is_not_finalized_by_a_launch_only_delivery_capability(self):
        for changes in ({'delivery_method': 'resume_prompt'}, {'wake_launch_id': uid()}, {}):
            message = self.message('failed')  # Its saved receipt also marks a refused wake.
            mailbox.update_message(self.directory, message['id'], **changes)
            with patch.object(native, 'context', side_effect=AssertionError('wake must keep its retry path')):
                self.assertIsNone(native.finalize_launch_only(self.root, self.directory, message['id']))
            self.assertEqual(mailbox.message_record(self.directory, message['id'])[1]['submission_state'], 'failed')

    def test_claude_capability_marker_distinguishes_design_from_broken_connection(self):
        from multiverse.harnesses.claude import BRIDGE
        base = SessionHandle(self.directory.name, 'claude', str(self.work), native_id=uid(), state='running')
        auto = harnesses.ClaudeAdapter.configured('claude', {})
        plain = harnesses.ClaudeAdapter.configured('claude', {'inbox_bridge': False})
        bridge = replace(base, native_mode={'mode': 'bridge', 'policy': 'auto'},
                         endpoint=NativeEndpoint('unix-lines', str(self.work / 'private.sock')))
        cases = [(plain, base, 'launch-only'),
                 (auto, replace(base, native_mode={'mode': 'plain', 'policy': 'auto',
                       'fallback_reason': 'Claude Code 2.1.279 is older than 2.1.280'}), 'launch-only'),
                 (auto, bridge, None),
                 (auto, replace(bridge, bridge=BRIDGE), 'claude-peer-inbox')]
        cases += [(auto, replace(bridge, state=state), None) for state in ('stopped', 'dormant', 'failed')]
        for adapter, session, expected in cases:
            with self.subTest(session=session, expected=expected):
                found = adapter.capabilities(session)
                for name in ('send_steer', 'send_follow_up'):
                    self.assertEqual(found[name].mechanism, expected)
        self.assertNotEqual(plain.capabilities(replace(base, agent_id=''))['send_steer'].mechanism, 'launch-only')

    def test_stopped_plain_claude_keeps_failed_unsubmitted_input_for_a_supported_wake(self):
        message = mailbox.append_message(self.directory, 'wake after repair', {}, delivery='steer')
        mailbox.update_message(self.directory, message['id'], submission_state='failed',
            attempts=[{'state': 'error', 'reason': 'Ledger failed before submission',
                       'value': {'pre_submission_failure': True}}])
        facts = {'state': 'stopped', 'launch_id': uid(), 'native_mode': {'mode': 'plain', 'policy': 'plain'}}
        write(self.directory / 'runtime.json', facts)
        with patch.object(native, 'observe', return_value={'live': 'stopped'}):
            deliver.run(self.root, agent='recipient', check=True)
            self.assertEqual(mailbox.message_record(self.directory, message['id'])[1]['submission_state'], 'failed')
            def wake(root, *, resume, resume_message_id, socket_path=None):
                mailbox.update_message(self.directory, resume_message_id, submission_state='accepted',
                    delivery_method='resume_prompt', attempts=[{'state': 'ok', 'ok': True}])
                return {'runtime': facts}
            with patch.object(lifecycle, 'start', side_effect=wake) as start:
                self.assertTrue(deliver.run(self.root, agent='recipient', retry=True)['ok'])
            start.assert_called_once()
