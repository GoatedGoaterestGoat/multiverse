"""Messages follow their agent; receipt checks retain the original conversation."""
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.harnesses.types import CommandSpec, NativeResult
from multiverse.harnesses.claude import ClaudeAdapter
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write
from multiverse.tools import deliver
from multiverse.universe import agents
from multiverse.universe.mailbox import append_message, message_record, update_message
from test_message_delivery import DeliveryFixture


class SessionDeliveryTest(DeliveryFixture):
    def setUp(self):
        super().setUp()
        self.adapter = native.harnesses.load(self.root, 'claude')

    def recipient(self, **properties):
        super().recipient(native_session_home=str(self.work), native_session_provider='claude', **properties)

    def clear(self):
        previous = read(self.directory / 'properties.json')['native_session_id']
        target = str(uuid4())
        agents.rebind_session(self.root, self.agent, previous, target, 'clear', self.launch, home=str(self.work))
        return target

    def test_a_switch_saves_concurrent_input_and_flushes_once_after_its_definite_result(self):
        """Input arriving during native selection must neither run on the departing thread nor disappear after a refused switch."""
        from multiverse.runtime import navigation, recovery
        for outcome in ('commit', 'abort'):
            with self.subTest(outcome=outcome):
                self.recipient()
                (self.directory / '.launch.lock').touch()
                agents.update_runtime(self.directory, self.launch, status='idle', startup_checked=True,
                                      native_relay={'state': 'ready'})
                with patch.object(recovery, 'check', return_value=NativeResult('ok')):
                    ticket = navigation.begin(self.root, self.directory, self.launch,
                        {'previous': self.native_id, 'target': None, 'reason': 'new', 'correlation': 'owner:10'})
                message = self.send('input during selection', delivery='follow-up')
                self.assertEqual(self.calls, [])
                tracking = message_record(self.directory, message['id'])[1]
                self.assertEqual(tracking.get('submission_state', 'pending'), 'pending')
                self.assertEqual(tracking['navigation_wait'], ticket['token'])
                native.deliver(self.root, self.agent, message['id'], retry=True)
                self.assertEqual(self.calls, [])
                target = str(uuid4()) if outcome == 'commit' else self.native_id
                if outcome == 'commit':
                    navigation.commit(self.root, self.directory, self.launch, {**ticket, 'target': target, 'home': str(self.work)})
                else:
                    navigation.abort(self.root, self.directory, self.launch, ticket)
                navigation.flush(self.root, self.directory, self.launch, ticket)
                navigation.flush(self.root, self.directory, self.launch, ticket)
                self.assertEqual([(s.native_id, m.id) for s, m, _ in self.calls], [(target, message['id'])])
                self.assertEqual(message_record(self.directory, message['id'])[1]['target_session'], target)

    def test_resume_settles_input_stranded_by_an_unfinished_switch_without_replay(self):
        """Closing an unresolved switch must not strand unsent input or let resume turn uncertain input into a retry."""
        from multiverse.runtime import navigation, recovery, queue_continuation
        from multiverse.universe import mailbox
        class LaunchAdapter(type(self.adapter)):
            default_model = None
            session_mode = 'discovered'
            bootstrap = ''
            storage_env = ()
            family = 'claude'
            def resume(self, session, context):
                return CommandSpec(argv=('fixture-harness',), native_mode={'mode': 'plain'})
            def continue_queued(self, *args):
                raise AssertionError('A retry must not restart the native queue')
        adapter = LaunchAdapter()
        backend = Mock()
        backend.create.return_value = self.terminal
        def resume(*, live='dormant'):
            with patch.object(lifecycle.harnesses, 'load', return_value=adapter), \
                    patch.object(lifecycle, 'Tmux', return_value=backend), \
                    patch.object(lifecycle.shutil, 'which', return_value='/fixture/executable'), \
                    patch.object(lifecycle, 'observe', return_value={'live': live}):
                return lifecycle.start(self.root, resume=self.agent, socket_path=self.terminal['socket'])
        for ending in ('admitted', 'aborted', 'committed', 'journal-only', 'missing-marker', 'failed-launch',
                       'publish-interrupted'):
            with self.subTest(ending=ending):
                self.recipient(task='', universe_id=read(self.root / 'universe.json')['id'],
                               native_storage_environment={})
                (self.directory / '.launch.lock').touch()
                agents.update_runtime(self.directory, self.launch, status='idle', startup_checked=True,
                                      native_relay={'state': 'ready'})
                with patch.object(recovery, 'check', return_value=NativeResult('ok')):
                    ticket = navigation.begin(self.root, self.directory, self.launch,
                        {'previous': self.native_id, 'target': None, 'reason': 'new', 'correlation': 'owner:lost'})
                messages = [self.send('held ' + mode, delivery=mode) for mode in ('steer', 'follow-up')]
                self.assertEqual(self.calls, [])
                original = [message_record(self.directory, m['id'])[0] for m in messages]
                if ending == 'admitted':
                    waiting = (self.directory / 'inbox-state.json').read_bytes()
                    with self.assertRaisesRegex(ValueError, 'close this terminal'):
                        resume(live='alive')
                    self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), waiting)
                target = self.native_id
                if ending in ('committed', 'journal-only'):
                    target = str(uuid4())
                    if ending == 'committed':
                        navigation.commit(self.root, self.directory, self.launch,
                                          {**ticket, 'target': target, 'home': str(self.work)})
                    else:
                        agents.rebind_session(self.root, self.agent, self.native_id, target, 'new', self.launch,
                                              home=str(self.work))
                elif ending == 'aborted':
                    navigation.abort(self.root, self.directory, self.launch, ticket)
                elif ending == 'missing-marker':
                    agents.update_runtime(self.directory, self.launch, navigation=None)
                agents.update_runtime(self.directory, self.launch, state='stopped')
                if ending == 'failed-launch':
                    with patch.object(backend, 'create', side_effect=RuntimeError('injected terminal failure')), \
                            self.assertRaisesRegex(RuntimeError, 'injected terminal failure'):
                        resume()
                elif ending == 'publish-interrupted':
                    saving = lifecycle.write
                    def interrupt_after_runtime(path, value):
                        saving(path, value)
                        if path == self.directory / 'runtime.json':
                            raise OSError('injected interruption after runtime replacement')
                    with patch.object(lifecycle, 'write', side_effect=interrupt_after_runtime), \
                            self.assertRaisesRegex(OSError, 'interruption after runtime replacement'):
                        resume()
                else:
                    resumed = resume()
                new_launch = agents.runtime(self.directory)['launch_id']
                self.assertNotEqual(new_launch, self.launch)
                self.assertEqual(self.calls, [])
                for message, saved in zip(messages, original):
                    current, tracking = message_record(self.directory, message['id'])
                    self.assertEqual(current, saved)
                    self.assertEqual(tracking.get('submission_state', 'pending'), 'failed')
                    self.assertIn('--retry', tracking['last_receipt']['reason'])
                if ending not in ('failed-launch', 'publish-interrupted'):
                    self.assertIn('--retry', ' '.join(resumed.get('notices', [])))
                # Failed rows survive another resume and do not acquire duplicate diagnostic attempts.
                settled = (self.directory / 'inbox-state.json').read_bytes()
                agents.update_runtime(self.directory, new_launch, state='stopped')
                resumed = resume()
                new_launch = resumed['runtime']['launch_id']
                self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), settled)
                checked = deliver.run(self.root, agent=self.agent, check=True)
                self.assertEqual(len(checked['messages']), 2)
                self.assertTrue(all(m['state'] == 'error' for m in checked['messages']))
                agents.update_runtime(self.directory, new_launch, state='running', status='idle',
                                      startup_checked=True, startup_delivery='ready', native_relay={'state': 'ready'})
                # The former marker no longer strands a later selection; no input was sent to make it admissible.
                with patch.object(recovery, 'check', return_value=NativeResult('ok')):
                    next_ticket = navigation.begin(self.root, self.directory, new_launch,
                        {'previous': target, 'target': None, 'reason': 'new', 'correlation': 'owner:next'})
                navigation.abort(self.root, self.directory, new_launch, next_ticket)
                with patch.object(native.harnesses, 'load', return_value=adapter), \
                        patch.object(queue_continuation, 'claim_restart', side_effect=AssertionError('retry restarted queue')):
                    self.assertTrue(deliver.run(self.root, agent=self.agent, retry=True)['ok'])
                    self.assertEqual(deliver.run(self.root, agent=self.agent, retry=True)['messages'], [])
                self.assertEqual([(s.launch_id, s.native_id, m.id) for s, m, _ in self.calls],
                                 [(new_launch, target, m['id']) for m in messages])
                self.assertFalse(list((self.directory / 'inbox-unsubmitted').glob('*.json')))
        # A tagged record is not authority to change an accepted/uncertain attempt, or manual/dismissed input.
        self.recipient(task='', universe_id=read(self.root / 'universe.json')['id'], native_storage_environment={})
        for state in ('accepted', 'submitting', 'unknown', 'failed', 'cancelled', 'manual', 'dismissed', 'untagged'):
            message = append_message(self.directory, state, {},
                                     delivery='inbox-only' if state == 'manual' else 'follow-up',
                                     target_session=self.native_id)
            update_message(self.directory, message['id'],
                           submission_state='pending' if state in ('manual', 'dismissed', 'untagged') else state,
                           navigation_wait=None if state == 'untagged' else 'old-switch',
                           dismissed_at='dismissed' if state == 'dismissed' else None,
                           attempts=[{'state': 'unknown', 'reason': 'preserve evidence'}])
        before = (self.directory / 'inbox-state.json').read_bytes()
        agents.update_runtime(self.directory, self.launch, state='stopped')
        resume()
        self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_checks_after_clear_use_the_original_conversation(self):
        """A check after /clear looks in the message's original conversation and storage; a break goes unnoticed
        because the properties pointer has moved, so it reports no receipt or reads the wrong transcript."""
        for state in ('pending', 'submitting', 'unknown', 'accepted'):
            with self.subTest(unresolved_switch=state):
                self.recipient()
                message = append_message(self.directory, 'check during selection', {}, delivery='follow-up',
                                         target_session=self.native_id)
                update_message(self.directory, message['id'], submission_state=state,
                               attempts=[{'state': 'unknown', 'launch_id': self.launch,
                                          'native_session_id': self.native_id}])
                agents.update_runtime(self.directory, self.launch, navigation={'state': 'admitted', 'id': 'switch'})
                before = (self.directory / 'inbox-state.json').read_bytes()
                lines = []
                with patch.object(self.adapter, 'get_delivery',
                                  side_effect=lambda session, identifier, executor: executor.check_session()) as lookup:
                    receipt = native.deliver(self.root, self.agent, message['id'], check=True)
                    self.assertEqual(receipt['state'], 'unknown')
                    deliver.run(self.root, agent=self.agent, check=True, _present=lines.append)
                lookup.assert_not_called()
                self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), before)
                self.assertIn('check is deferred', '\n'.join(lines))
                self.assertIn('--check', '\n'.join(lines))
                self.assertNotIn('--retry', '\n'.join(lines))
                self.assertNotIn('not delivered', '\n'.join(lines))
                self.assertEqual(self.calls, [])
        self.recipient()
        with self.subTest('an accepted message'):
            self.behavior = NativeResult('native_queued')
            message = self.send('accepted before clear')
            current = self.clear()
            checked = []
            def receipt(session, message_id, executor):
                checked.append((session.native_id, session.home, message_id))
                return NativeResult('native_queued', value={'transcript_confirmed': True})
            with patch.object(self.adapter, 'get_delivery', side_effect=receipt):
                result = deliver.run(self.root, agent=self.agent, check=True)
            self.assertTrue(result['ok'])
            self.assertEqual(checked, [(self.native_id, str(self.work), message['id'])])
            self.assertTrue(message_record(self.directory, message['id'])[1]['transcript_confirmed_at'])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], current)
            self.assertEqual(result['messages'][0]['conversation'], self.native_id)
            # A cached confirmed receipt retains its original conversation too.
            repeated = native.deliver(self.root, self.agent, message['id'], check=True)
            self.assertEqual(repeated['value']['native_session_id'], self.native_id)
        with self.subTest('an uncertain message'):
            self.recipient()
            self.behavior = TimeoutError('may have arrived')
            message = self.send('uncertain before clear')
            current = self.clear()
            unknown = NativeResult('unknown', 'No receipt yet')
            with patch.object(self.adapter, 'get_delivery', return_value=unknown) as check:
                result = native.deliver(self.root, self.agent, message['id'], check=True)
            self.assertEqual(result['state'], 'unknown')
            self.assertEqual(check.call_args.args[0].native_id, self.native_id)
            self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], current)
            self.assertEqual(message_record(self.directory, message['id'])[1]['submission_state'], 'unknown')
            self.assertEqual(len(self.calls), 1)
        with self.subTest('an old paused queue stays visible after a launch replacement'):
            from multiverse.tools._messaging import delivery_line
            self.recipient()
            self.behavior = NativeResult('native_queued', value={'queued_submission_id': 'old-native-queue-id'})
            message = self.send('paused old input')
            self.clear()
            paused = {'native_session_id': self.native_id, 'state': 'paused'}
            write(self.directory / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'running',
                  'tmux': self.terminal, 'queue_history': {self.native_id: paused}})
            def old_receipt(session, message_id, executor):
                self.assertEqual(session.native_id, self.native_id)
                self.assertEqual(session.queue_state, paused)
                return NativeResult('native_queued', value={'paused': True})
            with patch.object(self.adapter, 'get_delivery', side_effect=old_receipt):
                result = deliver.run(self.root, agent=self.agent, check=True)
            receipt = result['messages'][0]
            self.assertEqual(receipt['conversation'], self.native_id)
            self.assertTrue(receipt['paused'])
            with patch.object(self.adapter, 'get_delivery', side_effect=old_receipt):
                rendered = delivery_line('recipient', native.deliver(self.root, self.agent, message['id'], check=True))
            self.assertIn(self.native_id, rendered)
            self.assertIn('Return to that conversation', rendered)
            self.assertEqual(len(self.calls), 1)
        with self.subTest("Claude's receipt file, read without native calls"):
            self.recipient()
            self.behavior = NativeResult('native_queued')
            message = self.send('Claude receipt before clear')
            identifier = self.calls[-1][1].id
            write(self.directory / 'claude-receipts' / f'{identifier}.json',
                  {'id': identifier, 'route': 'mid-turn', 'at': 123})
            self.clear()
            def claude_receipt(*args):
                return ClaudeAdapter.get_delivery(None, *args)
            with patch.object(self.adapter, 'get_delivery', side_effect=claude_receipt):
                result = deliver.run(self.root, agent=self.agent, check=True)
            self.assertTrue(result['ok'])
            self.assertTrue(result['messages'][0]['transcript_confirmed'])
            self.assertEqual(len(self.calls), 1)

    def test_never_submitted_messages_follow_the_new_conversation_once(self):
        """Never-submitted messages are retargeted once to the current conversation on all three send paths, with the
        move journaled; fragile because startup flush, retry and wake each retarget separately (28f9aa9)."""
        with self.subTest('the startup queue'):
            message = append_message(self.directory, 'saved before clear', {}, delivery='steer',
                                     target_session=self.native_id)
            update_message(self.directory, message['id'], submission_state='pending', deferred_launch_id=self.launch)
            write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running',
                                                    'startup_delivery': 'waiting', 'tmux': self.terminal})
            current = self.clear()
            native.flush_startup(self.root, self.directory, self.launch)
            native.flush_startup(self.root, self.directory, self.launch)
            self.assertEqual([(s.native_id, m.id) for s, m, _ in self.calls], [(current, message['id'])])
            tracking = message_record(self.directory, message['id'])[1]
            self.assertEqual(tracking['target_session'], current)
            change, = tracking['session_changes']
            self.assertEqual((change['from'], change['to']), (self.native_id, current))
            self.assertEqual(change['transitions'][0]['reason'], 'clear')
        with self.subTest('a retry after two clears'):
            self.recipient()
            self.behavior = NativeResult('error', 'not accepted')
            message = self.send('retry after clear')
            self.clear()
            current = self.clear()
            self.behavior = NativeResult('native_queued')
            result = deliver.run(self.root, agent=self.agent, retry=True)
            self.assertTrue(result['ok'])
            self.assertEqual(self.calls[-1][0].native_id, current)
            original, tracking = message_record(self.directory, message['id'])
            self.assertEqual(original['target_session'], self.native_id)
            self.assertEqual(tracking['target_session'], current)
            self.assertEqual(len(tracking['session_changes'][0]['transitions']), 2)
        with self.subTest('a wake prompt'):
            self.recipient()
            message = append_message(self.directory, 'wake after clear', {}, delivery='follow-up',
                                     target_session=self.native_id)
            current = self.clear()
            prompt, tracking = lifecycle._wake_prompt(self.root, self.directory,
                                                    read(self.directory / 'properties.json'), message['id'])
            self.assertIn('wake after clear', prompt)
            self.assertEqual(tracking['target_session'], current)
            self.assertEqual(tracking['session_changes'][0]['from'], self.native_id)

    def test_unknown_and_interrupted_submissions_are_never_retargeted_or_replayed(self):
        """Accepted, unknown or submitting messages are never retargeted; a break duplicates an uncertain message into
        the new conversation, unseen."""
        for state in ('accepted', 'unknown', 'submitting'):
            message = append_message(self.directory, state, {}, delivery='steer', target_session=self.native_id)
            update_message(self.directory, message['id'], submission_state=state,
                           attempts=[{'state': 'unknown', 'reason': 'may have reached old session'}])
        self.clear()
        for message in native.inbox(self.directory, all_messages=True)['messages']:
            result = native.deliver(self.root, self.agent, message['id'], retry=True)
            self.assertTrue(result['reused'])
            tracking = message_record(self.directory, message['id'])[1]
            self.assertNotIn('session_changes', tracking)
            self.assertEqual(message['target_session'], self.native_id)
        self.assertEqual(self.calls, [])

    def test_the_session_journal_authorizes_only_its_own_agent_home_and_harness(self):
        """The session journal authorizes delivery only for its own agent, home and harness; a break delivers into
        another agent's or account's conversation."""
        with self.subTest('a foreign conversation or another home'):
            message = append_message(self.directory, 'foreign', {}, delivery='steer', target_session=str(uuid4()))
            self.clear()
            with self.assertRaisesRegex(ValueError, 'different native session'):
                native.deliver(self.root, self.agent, message['id'], retry=True)
            result = native.deliver(self.root, self.agent, message['id'], check=True)
            self.assertFalse(result['ok'])
            self.assertTrue(result['value']['outside_lineage'])
            historical = append_message(self.directory, 'wrong scope', {}, delivery='steer',
                                        target_session=self.native_id)
            props = read(self.directory / 'properties.json')
            write(self.directory / 'properties.json', {**props, 'native_session_home': str(self.work / 'other')})
            result = native.deliver(self.root, self.agent, historical['id'], check=True)
            self.assertFalse(result['ok'])
            self.assertEqual(self.calls, [])
        with self.subTest('a journal from another harness'):
            self.recipient()
            message = append_message(self.directory, 'wrong harness', {}, delivery='steer',
                                     target_session=self.native_id)
            self.clear()
            props = read(self.directory / 'properties.json')
            write(self.directory / 'properties.json', {**props, 'harness': 'pi'})
            with patch.object(self.adapter, 'get_delivery') as check:
                for checking in (False, True):
                    result = native.deliver(self.root, self.agent, message['id'], check=checking)
                    self.assertFalse(result['ok'])
                check.assert_not_called()
            self.assertEqual(self.calls, [])
        with self.subTest('the exact recipient and conversation across a resume'):
            self.recipient()
            self.behavior = NativeResult('error', 'before send')
            message = self.send()
            props = read(self.directory / 'properties.json')
            write(self.directory / 'properties.json', {**props, 'native_session_id': str(uuid4())})
            with self.assertRaisesRegex(ValueError, 'different native session'):
                native.deliver(self.root, self.agent, message['id'], retry=True)
            with self.assertRaisesRegex(ValueError, 'recipient inbox'):
                message_record(self.directory, str(uuid4()))
            write(self.directory / 'properties.json', props)
            resumed = str(uuid4())
            write(self.directory / 'runtime.json', {'launch_id': resumed, 'state': 'running', 'tmux': self.terminal})
            self.behavior = NativeResult('native_queued')
            result = native.deliver(self.root, self.agent, message['id'], retry=True)
            self.assertEqual(result['native_session_id'], self.native_id)
            self.assertEqual(result['launch_id'], resumed)
