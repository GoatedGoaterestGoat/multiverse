"""Definite failures remain visible, launch-only refusals stay final, and neither leaks message text."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch
from multiverse.runtime import lifecycle, native
from multiverse.store import read, uid, write
from multiverse.tools import deliver, doctor, inspect, list_agents, send
from multiverse.universe import mailbox, templates
from multiverse import harnesses
from multiverse.harnesses.claude import BRIDGE, CONVERSATION_MISSING, FRESH_SESSION, NEVER_USED, NOT_REGISTERED
from multiverse.harnesses.types import NativeResult, Message, NativeEndpoint, SessionHandle
from test_multiverse import BaseTest

TOO_OLD = 'Claude Code 2.1.279 is older than 2.1.280, which the multiverse mod needs'
OLD_LEDGER = Path(__file__).parent / 'fixtures/delivery/main-52b9df4-launch-only.json'  # written by main@52b9df4
CUSTOM = {'schema': 1, 'spawn': [sys.executable, '-c', 'pass'], 'resume': [sys.executable, '-c', 'pass']}


class DeliveryProblemTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.recipient(self.root)

    def recipient(self, root, facts=None, **properties):
        directory = root / 'state' / 'agents' / uid()
        write(directory / 'properties.json', {'id': directory.name, 'name': 'recipient',
            'harness': 'claude', 'workspace': str(self.work), 'native_session_id': uid(), **properties})
        write(directory / 'runtime.json', facts or {'launch_id': uid(), 'state': 'stopped'})
        return directory

    def universe(self, name):
        return templates.create(self.work / name, name, workdir=self.work)

    def running(self):
        return {'launch_id': uid(), 'state': 'running', 'native_mode': {'mode': 'plain', 'policy': 'plain'},
                'tmux': {'socket': str(self.work / 'absent.sock'), 'pane': '%1', 'session': '$1'}}

    def message(self, state, directory=None):
        directory = directory or self.directory
        message = mailbox.append_message(directory, 'PRIVATE BODY ' + state, {}, delivery='steer')
        mailbox.update_message(directory, message['id'], submission_state=state,
            attempts=[{'state': 'error' if state == 'failed' else state, 'ok': False,
                       'reason': 'Harness startup refused', 'value': {'wake_refused': True}}])
        return message

    def test_only_failed_messages_count_as_undelivered(self):
        """list, inspect and doctor count only failed messages as undelivered, never unknown, accepted or cancelled
        ones; a break goes unnoticed because an undelivered message gives no other signal."""
        for state in ('failed', 'failed', 'unknown', 'accepted', 'cancelled'):
            self.message(state)
        expected = '2 messages not delivered: mv2 deliver recipient --retry or --dismiss'
        self.assertIn(expected, list_agents.run(self.root, wide=True))
        self.assertIn(expected, inspect.cli_run(self.root, agent='recipient'))
        self.assertTrue(any(expected in reason for reason in doctor.run(self.root)['problems']))
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['delivery_problem'], expected)
        self.assertNotIn('PRIVATE BODY', expected)

    def test_dismissed_failures_stay_in_history_and_can_never_be_resent(self):
        """Dismissing a failure must remove its warning without replaying it or hiding uncertain acceptance."""
        selected = [self.message(state) for state in ('failed', 'launch-only')]
        untouched = [self.message(state) for state in ('accepted', 'unknown', 'submitting', 'pending', 'cancelled')]
        before = {item['id']: mailbox.message_record(self.directory, item['id'])[1]
                  for item in selected + untouched}
        with patch.object(native, 'send_or_wake', side_effect=AssertionError('dismiss must not submit')), \
                patch.object(harnesses, 'load', side_effect=AssertionError('dismiss needs no adapter')):
            lines = []
            result = deliver.run(self.root, agent='recipient', dismiss=True, _present=lines.append)
            self.assertTrue(result['ok'])
            self.assertEqual(result['dismissed'], 2)
            self.assertIn('2 delivery failures dismissed', lines[0])
            self.assertEqual(deliver.run(self.root, agent='recipient', dismiss=True)['dismissed'], 0)
        self.assertIsNone(mailbox.delivery_problem(self.directory, 'recipient'))
        history = mailbox.inbox(self.directory, all_messages=True)['messages']
        self.assertEqual(len(history), 7)
        for message in history:
            state = message['tracking']
            if message['id'] in {item['id'] for item in selected}:
                self.assertTrue(state.pop('dismissed_at'))
            self.assertEqual(state, before[message['id']])
        with patch.object(native, 'context', side_effect=AssertionError('dismissed input needs no native connection')):
            for message in selected:
                self.assertTrue(native.deliver(self.root, self.directory.name, message['id'], retry=True)['value']['dismissed'])
                self.assertTrue(native.send_or_wake(self.root, self.directory.name, message['id'], retry=True)['value']['dismissed'])
        # A retry snapshot taken before dismissal must re-read the final marker under the delivery locks.
        stale = [{**item, 'tracking': before[item['id']]} for item in selected]
        with patch.object(mailbox, 'inbox', return_value={'messages': stale}), \
                patch.object(harnesses, 'load', side_effect=AssertionError('stale retry must not load an adapter')):
            self.assertTrue(deliver.run(self.root, agent='recipient', retry=True)['ok'])
        for message in untouched:
            self.assertEqual(mailbox.message_record(self.directory, message['id'])[1], before[message['id']])

    def test_launch_only_or_broken_connection_per_harness(self):
        """For each harness, mode and state an unavailable send or ask is classed launch-only (restarting cannot
        help) or a broken connection, with the cause; fragile because the cause combines setting, native mode, state
        and fallback notes, and a wrong class drops messages as final or retries a launch-only agent forever
        (74887d9, CLAUDE-2, 526733d)."""
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.work / 'claude-config')}))
        self.enterContext(patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 302)))
        plain = harnesses.ClaudeAdapter.configured('claude', {'inbox_bridge': False})
        auto = harnesses.ClaudeAdapter.configured('claude', {})
        codex = harnesses.CodexAdapter.configured('codex', {'inbox_bridge': False})
        endpoint = NativeEndpoint('unix-lines', str(self.work / 'in.sock'))

        def claude(state='running', policy='auto', fallback=None, mode='plain', **fields):
            return SessionHandle(self.directory.name, 'claude', str(self.work), launch_id='launch', native_id=uid(),
                                 state=state, native_mode={'policy': policy, 'mode': mode, 'fallback_reason': fallback},
                                 **fields)

        def bridged(state='running', **fields):
            return claude(state, mode='bridge', endpoint=endpoint, **fields)

        def launch_only(cause, final=True):
            return f'This agent is launch-only: {cause}', {'launch_only': True, 'final': final}
        setting = launch_only('its harness sets inbox_bridge to false')
        stopped = "This agent isn't running: resume it first", None
        registered = {'bridge': BRIDGE, 'session_file': str(self.work / 'agent/session.jsonl')}
        down = ('stopped', 'dormant', 'failed')
        rows = [  # (case, adapter, session, send mechanism, (send and ask reason, value) or None)
            ('setting, no mode recorded', plain, SessionHandle(self.directory.name, 'claude', str(self.work),
                native_id=uid(), state='running'), 'launch-only', None),
            # inbox_bridge false: the setting is the cause, also after a fresh start added its note.
            ('setting', plain, claude(policy='plain'), 'launch-only', setting),
            ('setting, fresh start', plain, claude(policy='plain', fallback=NEVER_USED), 'launch-only', setting),
            ('setting, stopped', auto, claude('stopped', policy='plain'), None, stopped),
            # Unset (auto) on a Claude too old for the mod: the version is the cause, never the fresh-start note.
            ('too old', auto, claude(fallback=TOO_OLD), None, launch_only(TOO_OLD, False)),
            ('too old, missing', auto, claude(fallback=f'{TOO_OLD}; {CONVERSATION_MISSING}'), None,
             launch_only(TOO_OLD, False)),
            ('too old, older record', auto, claude(fallback=f'{TOO_OLD}; {FRESH_SESSION}'), None,
             launch_only(TOO_OLD, False)),
            # A bridged launch whose mod has not registered yet should be connected: a restart case, no flag.
            ('not registered', auto, bridged(), None, (NOT_REGISTERED, None)),
            ('starting', auto, bridged('starting'), None, (NOT_REGISTERED, None)),
            ('registered', auto, bridged(**registered), 'claude-peer-inbox', None),
            # A launch that is not running is a resume case, registered or not, never launch-only.
            *[(state, auto, bridged(state, **registered), None, stopped) for state in down],
            *[(f'{state}, unregistered', auto, bridged(state), None, None) for state in down],
            # Codex set to plain: only a plain launch is launch-only, never a starting or lost bridge.
            *[(f'codex {mode}', codex, SessionHandle(self.directory.name, 'codex', str(self.work), state='starting',
                native_mode={'mode': mode, 'policy': 'auto'}, native_relay=relay),
               'launch-only' if mode == 'plain' else None, None)
              for mode, relay in (('plain', None), ('bridge', None), ('bridge', {'state': 'lost', 'reason': 'lost'}))],
        ]
        executor = Mock()
        message = Message(uid(), 'hi', framed='Message from lead:\nhi')
        for case, adapter, session, mechanism, outcome in rows:
            with self.subTest(case, state=session.state):
                found = adapter.capabilities(session)
                for name in ('send_steer', 'send_follow_up'):
                    self.assertEqual(found[name].mechanism, mechanism)
                if outcome is None:
                    continue
                reason, value = outcome
                for result in (adapter.send(session, message, executor), adapter.ask(session, 'why?', executor)):
                    self.assertEqual((result.state, result.reason, result.value), ('unavailable', reason, value))
                self.assertEqual(found['ask'].reason, reason)
                if value:
                    for name in ('send_steer', 'send_follow_up', 'ask'):
                        self.assertEqual((found[name].state, found[name].reason), ('unavailable', reason))
                    supported = {name for name, capability in found.items() if capability.state == 'supported'}
                    self.assertNotIn('get_delivery', supported)
        executor.write_lines.assert_not_called()
        self.assertNotEqual(plain.capabilities(replace(rows[0][2], agent_id=''))['send_steer'].mechanism, 'launch-only')

    def test_launch_only_refusal_is_final_and_failed_wakes_stay_retryable(self):
        """A policy refusal stays final until explicit retry sees messaging connected, including after a ledger reset, while a
        failed wake, pre-submission failure and temporary fallback stay retryable; fragile because three ledger generations and two
        delivery mechanisms share submission_state, and a break silently loses or duplicates messages (LC-23,
        CLAUDE-13, e5bbba0); Codex must probe the resumed bridge before reopening its policy refusal."""
        never = patch.object(native, 'send_or_wake', side_effect=AssertionError('a final refusal must not be resent'))
        with self.subTest('a typed refusal'):
            for state in ('failed', 'accepted', 'unknown', 'cancelled'):
                message = self.message(state)
                mailbox.update_message(self.directory, message['id'], native_receipt={'launch_only': True},
                    attempts=[{'state': 'unavailable', 'ok': False, 'reason': 'This harness is launch-only',
                               'value': {'launch_only': True, 'final': True}}])
                tracking = mailbox.message_record(self.directory, message['id'])[1]
                self.assertEqual(tracking['submission_state'], 'launch-only' if state == 'failed' else state)
                if state == 'failed':
                    self.assertEqual(tracking['final_reason'], 'launch-only')
            with never:
                self.assertEqual(deliver.run(self.root, agent='recipient', retry=True)['messages'], [])
            self.assertNotIn('messages not delivered', list_agents.run(self.root, wide=True))
        with self.subTest('a failed wake keeps its retry path'):
            root = self.universe('wakes')
            directory = self.recipient(root)
            for changes in ({'delivery_method': 'resume_prompt'}, {'wake_launch_id': uid()}, {}):
                message = self.message('failed', directory)  # Its saved receipt also marks a refused wake.
                mailbox.update_message(directory, message['id'], **changes)
                with patch.object(native, 'context', side_effect=AssertionError('wake must keep its retry path')):
                    self.assertIsNone(native.finalize_launch_only(root, directory, message['id']))
                self.assertEqual(mailbox.message_record(directory, message['id'])[1]['submission_state'], 'failed')
        with self.subTest('a pre-submission failure wakes a stopped plain Claude'):
            root = self.universe('stopped')
            facts = {'state': 'stopped', 'launch_id': uid(), 'native_mode': {'mode': 'plain', 'policy': 'plain'}}
            directory = self.recipient(root, facts)
            message = mailbox.append_message(directory, 'wake after repair', {}, delivery='steer')
            mailbox.update_message(directory, message['id'], submission_state='failed',
                attempts=[{'state': 'error', 'reason': 'Ledger failed before submission',
                           'value': {'pre_submission_failure': True}}])
            with patch.object(native, 'observe', return_value={'live': 'stopped'}):
                deliver.run(root, agent='recipient', check=True)
                self.assertEqual(mailbox.message_record(directory, message['id'])[1]['submission_state'], 'failed')
                def wake(root, *, resume, resume_message_id, socket_path=None):
                    mailbox.update_message(directory, resume_message_id, submission_state='accepted',
                        delivery_method='resume_prompt', attempts=[{'state': 'ok', 'ok': True}])
                    return {'runtime': facts}
                with patch.object(lifecycle, 'start', side_effect=wake) as start:
                    self.assertTrue(deliver.run(root, agent='recipient', retry=True)['ok'])
                start.assert_called_once()
        with self.subTest('auto fallbacks and older final markers remain retryable after bridge recovery'):
            for old_state in (None, 'failed', 'launch-only'):
                with self.subTest(old_state=old_state):
                    root = self.universe('transient-' + str(old_state))
                    facts = {**self.running(), 'native_mode': {
                        'mode': 'plain', 'policy': 'auto', 'fallback_reason': TOO_OLD}}
                    directory = self.recipient(root, facts)
                    with patch.object(native, 'observe', return_value={'live': 'alive'}), \
                            patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                            patch.object(native.NativeExecutor, 'check_session'):
                        if old_state is None:
                            result = send.run(root, agent='recipient', text='keep for bridge')
                            self.assertFalse(result['ok'])
                            message = mailbox.inbox(directory, all_messages=True)['messages'][0]
                        else:
                            message = mailbox.append_message(directory, 'keep for bridge', {}, delivery='steer')
                            mailbox.update_message(directory, message['id'], submission_state=old_state,
                                final_reason='launch-only', attempts=[{'state': 'unavailable', 'ok': False,
                                    'value': {'launch_only': True}}])
                        before = (directory / 'inbox-state.json').read_bytes()
                        self.assertEqual(mailbox.message_record(directory, message['id'])[1]['submission_state'], 'failed')
                        self.assertIn('1 message not delivered', list_agents.run(root, wide=True))
                        self.assertEqual((directory / 'inbox-state.json').read_bytes(), before)
                        with patch.object(harnesses.ClaudeAdapter, 'capabilities', return_value={}), \
                                patch.object(harnesses.ClaudeAdapter, 'send',
                                             return_value=NativeResult('ok', value={'transcript_confirmed': True})) as submit:
                            self.assertTrue(deliver.run(root, agent='recipient', retry=True)['ok'])
                            submit.assert_called_once()
                            deliver.run(root, agent='recipient', retry=True)
                            self.assertEqual(submit.call_count, 1)

        with self.subTest('explicit retry rechecks a final refusal only after messaging connects'):
            root = self.universe('enabled-bridge')
            directory = self.recipient(root, self.running())
            manifest = root / 'harnesses/claude/harness.json'
            definition = read(manifest)
            definition['settings']['inbox_bridge'] = False
            write(manifest, definition)
            with patch.object(native, 'observe', return_value={'live': 'alive'}), \
                    patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                    patch.object(native.NativeExecutor, 'check_session'):
                self.assertFalse(send.run(root, agent='recipient', text='keep until messaging connects')['ok'])
                message = mailbox.inbox(directory, all_messages=True)['messages'][0]
                self.assertEqual(message['tracking']['submission_state'], 'launch-only')
                ledger = directory / 'inbox-state.json'
                original = ledger.read_bytes()
                lines = []
                with patch.object(harnesses.ClaudeAdapter, 'send',
                                  return_value=NativeResult('ok', value={'transcript_confirmed': True})) as submit:
                    result = deliver.run(root, agent='recipient', retry=True, _present=lines.append)
                    self.assertEqual(ledger.read_bytes(), original)
                    submit.assert_not_called()
                    definition['settings']['inbox_bridge'] = True
                    write(manifest, definition)
                    facts = {**self.running(), 'native_mode': {'mode': 'bridge', 'policy': 'required'},
                             'native_bridge': BRIDGE,
                             'control_endpoint': {'transport': 'unix-lines', 'address': str(self.work / 'inbox.sock')}}
                    write(directory / 'runtime.json', facts)
                    # Observing the newly connected launch must not reopen anything.
                    deliver.run(root, agent='recipient', check=True)
                    list_agents.run(root, wide=True)
                    self.assertEqual(ledger.read_bytes(), original)
                    protected = []
                    for state in ('accepted', 'unknown', 'submitting'):
                        saved = self.message(state, directory)
                        protected.append((saved['id'], mailbox.message_record(directory, saved['id'])[1]))
                    self.assertTrue(deliver.run(root, agent='recipient', retry=True)['ok'])
                    submit.assert_called_once()
                    for identifier, tracking in protected:
                        self.assertEqual(mailbox.message_record(directory, identifier)[1], tracking)
                    self.assertEqual(result.get('finalized'), 1)
                    self.assertIn('final', lines[-1])
                    self.assertEqual(submit.call_args.args[1].id, message['id'])
                    self.assertEqual(mailbox.message_record(directory, message['id'])[1]['submission_state'], 'accepted')
                    deliver.run(root, agent='recipient', retry=True)
                    self.assertEqual(submit.call_count, 1)

        with self.subTest('Codex plain refusals retry once after the same session resumes with an auto bridge'):
            root = self.universe('codex-enabled-bridge')
            directory = self.recipient(root, self.running(), harness='codex')
            native_id = read(directory / 'properties.json')['native_session_id']
            manifest = root / 'harnesses/codex/harness.json'
            definition = read(manifest)
            definition['settings']['inbox_bridge'] = False
            write(manifest, definition)
            requests, queued = [], []
            controllable = False
            def rpc(specification, workspace, timeout):
                plan = specification.plan()
                request = next(plan)
                while True:
                    requests.append(request)
                    if 'threadId' in request.params:
                        self.assertEqual(request.params['threadId'], native_id)
                    if request.method == 'thread/queue/add':
                        queued.append({'id': 'queue-' + str(len(queued)),
                                       'clientUserMessageId': request.params['clientUserMessageId']})
                        answer = {'queuedSubmission': queued[-1]}
                    else:
                        answer = {'initialize': {}, 'thread/read': {'thread': {'id': native_id,
                            'cwd': str(self.work), 'status': {'type': 'idle' if controllable else 'notLoaded'}}},
                            'thread/turns/list': {'data': []}, 'thread/queue/list': {'data': queued[:1]},
                            'thread/queue/start': {'turn': {'id': 'turn'}}}[request.method]
                    try:
                        request = plan.send({'result': answer})
                    except StopIteration as done:
                        return done.value
            with patch.object(native, 'observe', return_value={'live': 'alive'}), \
                    patch.object(native, 'owned'), patch.object(native, 'execute_rpc', side_effect=rpc):
                for mode in ('steer', 'follow-up'):
                    self.assertFalse(send.run(root, agent='recipient', text='saved ' + mode, delivery=mode)['ok'])
                history = mailbox.inbox(directory, all_messages=True)['messages']
                self.assertEqual([m['tracking']['submission_state'] for m in history], ['launch-only'] * 2)
                ledger = directory / 'inbox-state.json'
                original = ledger.read_bytes()
                self.assertEqual(deliver.run(root, agent='recipient', retry=True)['finalized'], 2)
                self.assertEqual(requests, [])
                definition['settings'].pop('inbox_bridge')  # Auto, as on a resumed real bridge.
                write(manifest, definition)
                write(directory / 'runtime.json', {**self.running(),
                    'native_mode': {'mode': 'bridge', 'policy': 'auto'},
                    'control_endpoint': {'transport': 'unix-websocket', 'address': str(self.work / 'codex.sock')}})
                # A bridge whose exact thread is not controllable cannot reopen a refusal.
                deliver.run(root, agent='recipient', retry=True)
                self.assertEqual(ledger.read_bytes(), original)
                self.assertEqual(queued, [])
                controllable = True
                self.assertTrue(deliver.run(root, agent='recipient', retry=True)['ok'])
                self.assertEqual([item['clientUserMessageId'] for item in queued], [m['id'] for m in history])
                self.assertEqual([m['tracking']['submission_state'] for m in
                    mailbox.inbox(directory, all_messages=True)['messages']], ['accepted'] * 2)
                self.assertEqual(deliver.run(root, agent='recipient', retry=True)['messages'], [])
                self.assertEqual(len(queued), 2)

        with self.subTest('a custom harness, running then stopped'):
            root = self.universe('custom')
            write(root / 'harnesses/custom/harness.json', CUSTOM)
            directory = self.recipient(root, self.running(), harness='custom')
            for live in ('alive', 'stopped'):
                with patch.object(native, 'observe', return_value={'live': live}), patch.object(native, 'owned'), \
                        patch.object(lifecycle, 'observe', return_value={'live': live}):
                    for delivery in ('steer', 'follow-up'):
                        result = send.run(root, agent='recipient', text='refused', delivery=delivery)
                        self.assertEqual((result['saved'], result['ok']), (True, False))
                    history = mailbox.inbox(directory, all_messages=True)['messages']
                    self.assertEqual({m['tracking']['submission_state'] for m in history}, {'launch-only'})
                    self.assertNotIn('not delivered', list_agents.run(root, wide=True))
                    self.assertEqual(doctor.run(root)['problems'], [])
                    with never, patch.object(lifecycle, 'start', side_effect=AssertionError('a final refusal')):
                        for option in ('retry', 'check'):
                            result = deliver.run(root, agent='recipient', **{option: True})
                            self.assertEqual(result, {'messages': [], 'ok': True,
                                **({'finalized': len(history)} if option == 'retry' else {})})
        with self.subTest('a ledger written by main@52b9df4, then reset'):
            fixture = json.loads(OLD_LEDGER.read_text())
            attempt = fixture['tracking']['attempts'][-1]
            root = self.universe('upgraded')
            manifest = root / 'harnesses/claude/harness.json'
            definition = read(manifest)
            definition['settings']['inbox_bridge'] = False
            write(manifest, definition)
            directory = root / 'state/agents' / attempt['agent_id']
            write(directory / 'properties.json', {'id': directory.name, 'name': 'recipient', 'harness': 'claude',
                'task': '', 'universe_id': read(root / 'universe.json')['id'], 'workspace': str(self.work),
                'native_session_id': attempt['native_session_id']})
            write(directory / 'runtime.json', {**self.running(), 'launch_id': attempt['launch_id']})
            (directory / 'inbox.jsonl').write_text(json.dumps(fixture['message']) + '\n')
            ledger = directory / 'inbox-state.json'
            write(ledger, {fixture['message']['id']: fixture['tracking']})
            with patch.object(native, 'observe', return_value={'live': 'alive'}), \
                    patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
                with never:
                    result = deliver.run(root, agent='recipient', retry=True)
                self.assertEqual(result, {'messages': [], 'ok': True, 'finalized': 1})
                write(root / 'harnesses/custom/harness.json', CUSTOM)
                write(directory / 'properties.json', {**read(directory / 'properties.json'), 'harness': 'custom'})
                ledger.write_text('[]')
                self.assertTrue(send.run(root, agent='recipient', text='saved while the ledger was damaged')['saved'])
                ledger.unlink()  # Both inbox messages survive the explicit ledger reset.
                lines = []
                with patch.object(native, 'deliver', side_effect=AssertionError('no receipt implementation exists')):
                    deliver.run(root, agent='recipient', check=True, _present=lines.append)
                self.assertEqual(lines.pop(0), 'recipient: 2 not delivered.')
                self.assertEqual(len(lines), 2)
                for line in lines:
                    self.assertIn('inbox-only', line)
                    self.assertIn('attach', line)
                    self.assertNotIn('resume it', line)
                history = mailbox.inbox(directory, all_messages=True)['messages']
                self.assertEqual([m['tracking']['submission_state'] for m in history], ['launch-only'] * 2)
                self.assertEqual(deliver.run(root, agent='recipient', check=True)['messages'], [])
                self.assertEqual(deliver.run(root, agent='recipient', retry=True)['messages'], [])
                self.assertEqual(doctor.run(root)['problems'], [])
