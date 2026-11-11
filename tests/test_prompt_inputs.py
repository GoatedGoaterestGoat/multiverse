"""Retained questions use ordinary native input without acquiring a second send path."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses, store
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import NativeResult, PromptInput, PromptQuestion, WaitingPrompt
from multiverse.runtime import native, navigation, prompts, recovery
from multiverse.universe import agents
from test_multiverse import BaseTest


class PromptInputTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.launch, self.thread = (str(uuid4()) for _ in range(3))
        self.directory = self.root / 'state/agents' / self.agent
        store.write(self.directory / 'properties.json', {
            'id': self.agent, 'name': 'worker', 'harness': 'codex', 'workspace': str(self.work),
            'native_session_id': self.thread, 'native_session_home': str(self.work / 'codex-home')})
        store.write(self.directory / 'runtime.json', {
            'launch_id': self.launch, 'session_generation': 1, 'state': 'running', 'status': 'idle',
            'native_mode': {'mode': 'bridge'}, 'native_relay': {'state': 'ready'}, 'startup_checked': True,
            'control_endpoint': {'transport': 'unix-websocket', 'address': str(self.work / 'fake.sock')}})
        self.adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        self.catalog, self.calls, self.queue, self.started, self.confirmed = [], [], [], [], set()
        self.status, self.fail_method = 'idle', None
        self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))
        self.enterContext(patch.object(native, 'session_snapshot', side_effect=lambda root, directory, p, f, **kw: (p, f)))
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', side_effect=lambda p, f: {
            'live': 'alive' if f.get('state') == 'running' else 'stopped'}))
        self.enterContext(patch.object(self.adapter, 'waiting_prompts', side_effect=lambda *args: NativeResult(
            'ok', value={'prompts': self.catalog, 'coverage': 'complete'})))
        self.prepare = self.enterContext(patch.object(self.adapter, 'answer_prompt', side_effect=lambda session, answer, ex:
            NativeResult('ok', value=PromptInput('prompt-' + answer.id, 'PRIVATE_INPUT:' + answer.answers['q'][0]))))
        self.callback = self.enterContext(patch.object(self.adapter, 'prompt_outcome', return_value=NativeResult('unknown')))
        self.events = self.enterContext(patch.object(agents, '_event'))
        self.enterContext(patch.object(native.NativeExecutor, 'rpc', self.rpc))
        self.fresh()

    def fresh(self):
        self.prompt = WaitingPrompt(id=str(uuid4()), kind='question', summary='Choose', requires_waiting=False,
                                    questions=(PromptQuestion(id='q', question='Which?', options=(('A', ''),)),))
        self.catalog = [self.prompt]

    def answer(self):
        return prompts.answer(self.root, self.agent, self.prompt.id, answers={'q': 'PRIVATE_ANSWER'}, wait=False)

    def result(self):
        return prompts.result(self.root, self.agent, self.prompt.id)

    def rpc(self, spec, timeout=10):
        """Drive the real Codex protocol with in-memory native replies, never a socket or process."""
        plan = spec.plan()
        reply = None
        while True:
            try:
                request = plan.send(reply)
            except StopIteration as stopped:
                return stopped.value
            self.calls.append((request.method, request.params))
            if request.method == self.fail_method:
                raise OSError('PRIVATE transport failure after an uncertain write')
            if request.method == 'initialize':
                value = {}
            elif request.method == 'thread/read':
                value = {'thread': {'id': self.thread, 'cwd': str(self.work), 'status': {'type': self.status},
                                    'canAcceptDirectInput': True}}
            elif request.method == 'thread/queue/add':
                item = {'id': 'queued-' + request.params['clientUserMessageId'],
                        'clientUserMessageId': request.params['clientUserMessageId']}
                self.queue.append(item)
                value = {'queuedSubmission': item}
            elif request.method == 'thread/queue/list':
                value = {'data': self.queue[:request.params['limit']], 'nextCursor': None}
            elif request.method == 'thread/queue/start':
                self.assertEqual(request.params, {'threadId': self.thread}, 'Start must choose the native FIFO head')
                self.started.append(self.queue.pop(0)['clientUserMessageId'])
                value = {'turn': {'id': 'turn-started'}}
            elif request.method == 'thread/turns/list':
                value = {'data': [{'id': 'turn-active', 'status': 'inProgress',
                                  'items': [{'type': 'userMessage', 'clientId': id} for id in self.confirmed]}]}
            elif request.method == 'turn/steer':
                value = {'turnId': 'turn-active'}
            else:
                raise AssertionError('Unexpected native operation: ' + request.method)
            if request.method in ('thread/queue/add', 'turn/steer') and getattr(self, 'input_echo', None):
                self.input_echo(request.params)
            reply = {'result': value}

    def test_real_async_recipe_and_early_echo_keep_core_delivery_once_and_fifo(self):
        """The real registry and core must agree on input identity before an echo races the send receipt."""
        from multiverse.harnesses.codex import async_questions, prompt_codec, prompts as registry

        version = '.'.join(map(str, prompt_codec.VERSION))
        agents.update_runtime(self.directory, self.launch, native_versions={'cli': version, 'daemon': version})
        connection = SimpleNamespace(
            root=SimpleNamespace(native_id=self.thread, bound=True, launch_refusal=None,
                                 settings={'navigation_cli': prompt_codec.VERSION}),
            version=prompt_codec.VERSION, owns_primary=lambda: True, ended=False, connection_id=str(uuid4()))
        relay = registry.Relay(connection, {'agent_directory': str(self.directory), 'launch_id': self.launch})
        relay.observe('outgoing', {'id': 'mode', 'method': 'turn/start', 'params': {'threadId': self.thread}})
        relay.observe('incoming', {'id': 'mode', 'result': {'turn': {'id': 'first'}}})

        def flush():
            jobs, relay.jobs = relay.jobs, registry.Jobs()
            relay.files.apply(jobs)

        self.adapter.waiting_prompts.side_effect = lambda *args: CodexAdapter.waiting_prompts(self.adapter, *args)
        self.prepare.side_effect = lambda *args: CodexAdapter.answer_prompt(self.adapter, *args)
        for index, state in enumerate(('idle', 'paused', 'lost-receipt')):
            with self.subTest(queue=state):
                generation = index + 1
                self.status = 'active' if state == 'lost-receipt' else 'idle'
                self.queue = [{'id': 'older', 'clientUserMessageId': 'older-native-input'}] if state == 'paused' else []
                agents.update_runtime(self.directory, self.launch, session_generation=generation,
                    queue_state={'native_session_id': self.thread, 'state': state if state == 'paused' else 'idle'})
                item = 'async-' + str(index)
                relay.observe('incoming', {'method': 'item/completed', 'params': {
                    'threadId': self.thread, 'turnId': 'first', 'item': {'type': 'agentMessage', 'id': item,
                    'delivery': 'async', 'text': 'Questions',
                    'questions': [{'title': 'Same title', 'options': None}]}}})
                flush()
                listed = prompts.waiting(self.root, self.agent)
                choices = [p for p in listed.prompts if p.answerable]
                self.assertEqual(len(choices), 1, listed)
                self.prompt = choices[0]
                input_id = 'prompt-' + self.prompt.id
                text = 'PRIVATE_JOINED_ANSWER_' + str(index)

                def echo(params):
                    self.assertEqual(params['clientUserMessageId'], input_id)
                    record = store.read(self.directory / 'prompts' / (self.prompt.id + '.json'))
                    self.assertEqual((record['input_id'], record['session_generation']), (input_id, generation))
                    wire = params['input'][0]['text']
                    self.assertEqual(async_questions.replies([{'type': 'text', 'text': wire}]), [{
                        'answer': text, 'question': 'Same title',
                        'questionItemId': async_questions.question_id(item, 0)}])
                    relay.observe('incoming', {'method': 'item/completed', 'params': {
                        'threadId': self.thread, 'turnId': 'echo', 'item': {'type': 'userMessage', 'id': 'echo-' + item,
                        'clientId': input_id, 'content': [{'type': 'text', 'text': wire}]}}})
                    flush()
                    self.assertNotEqual(relay.records[self.prompt.id].get('result', {}).get('state'), 'person')
                    if state == 'lost-receipt':
                        raise OSError('PRIVATE accepted input lost its receipt')

                self.input_echo = echo
                outcome = prompts.answer(self.root, self.agent, self.prompt.id, answers={'answer': text}, wait=False)
                self.assertEqual(outcome.state, 'unknown' if state == 'lost-receipt' else 'submitted', outcome)
                self.assertNotEqual(relay.records[self.prompt.id].get('result', {}).get('state'), 'person')
                if state != 'lost-receipt':
                    self.assertEqual(self.started[-1:], ['older-native-input' if state == 'paused' else input_id])
                pending = prompts.pending_inputs(self.directory, self.launch, self.thread, generation)
                self.assertEqual(pending, self.prompt.id)
                event = {'previous': self.thread, 'reason': 'new', 'target': None, 'correlation': 'owner:1'}
                with patch.object(recovery, 'check', return_value=NativeResult('ok')) as recovery_check:
                    with self.assertRaisesRegex(ValueError, self.prompt.id):
                        navigation.begin(self.root, self.directory, self.launch, event)
                    recovery_check.assert_not_called()
                before = len(self.started), sum(m in ('thread/queue/add', 'turn/steer') for m, _ in self.calls)
                for _ in range(2):
                    self.assertIn(self.result().state, ('submitted', 'unknown'))
                    again = prompts.answer(self.root, self.agent, self.prompt.id, answers={'answer': text}, wait=False)
                    self.assertEqual(again.cause, 'already_answered')
                after = len(self.started), sum(m in ('thread/queue/add', 'turn/steer') for m, _ in self.calls)
                self.assertEqual(after, before)
                self.queue.clear()
                self.confirmed.add(input_id)
                self.assertEqual(self.result().state, 'answered')
                self.assertIsNone(prompts.pending_inputs(self.directory, self.launch, self.thread, generation))
                for path in (self.directory / 'prompts').glob('*.json'):
                    self.assertNotIn(text, path.read_text())
                self.assertNotIn(text, str(self.events.call_args_list))
                self.assertFalse((self.directory / 'inbox.jsonl').exists())
                self.assertFalse((self.directory / 'prompts' / (self.prompt.id + '.answer.json')).exists())
        self.callback.assert_not_called()

    def test_policy_recipe_and_reservation_do_not_widen_callback_authority(self):
        """A nonwaiting question must not bypass caller, callback policy, payload bounds or permanent admission."""
        callback = replace(self.prompt, id=str(uuid4()), requires_waiting=True)
        self.catalog.append(callback)
        found = prompts.waiting(self.root, self.agent)
        self.assertEqual([p.id for p in found.prompts], [self.prompt.id])
        self.assertEqual(self.result().cause, 'not_answered')
        self.assertEqual(prompts.answer(self.root, self.agent, callback.id, answers={'q': 'A'}, wait=False).cause,
                         'not_waiting')
        self.assertEqual(prompts.result(self.root, self.agent, callback.id).cause, 'not_waiting')
        for value in (1, 0, None, 'false'):
            with self.subTest(policy=value):
                self.catalog = [replace(self.prompt, requires_waiting=value)]
                self.assertEqual(prompts.waiting(self.root, self.agent).state, 'unavailable')
                self.assertEqual(self.answer().state, 'refused')
        self.catalog = [replace(self.prompt, kind='permission')]
        self.assertEqual(prompts.waiting(self.root, self.agent).state, 'unavailable')
        self.assertFalse(self.calls)
        self.assertFalse((self.directory / 'prompts').exists())

        for facts in (None, [], {'session_generation': None}, {'session_generation': True},
                      {'session_generation': -1}, {'session_generation': '1'}):
            with self.subTest(input_facts=facts):
                self.fresh()
                connected = prompts._connect(self.root, self.directory)
                executor = connected[2]
                with patch.object(executor, 'facts', facts), patch.object(executor, 'check_session'), \
                        patch.object(prompts, '_connect', return_value=connected):
                    try:
                        result = self.answer()
                    except (AttributeError, TypeError):
                        self.fail('Missing or malformed input facts must refuse rather than raise')
                self.assertEqual((result.state, result.cause), ('refused', 'unconfirmed'))
                self.assertFalse((self.directory / 'prompts' / (self.prompt.id + '.json')).exists())
        self.prepare.assert_not_called()
        self.assertFalse(self.calls)

        for name, recipe in (('wrong ID', lambda id: PromptInput('wrong', 'PRIVATE')),
                             ('NUL', lambda id: PromptInput(id, 'PRIVATE\0')),
                             ('invalid UTF-8', lambda id: PromptInput(id, '\ud800')),
                             ('wrong type', lambda id: PromptInput(id, [])),
                             ('large', lambda id: PromptInput(id, 'é' * (64 * 1024 + 1))),
                             ('callback recipe', lambda id: PromptInput(id, 'PRIVATE'))):
            with self.subTest(recipe=name):
                self.fresh()
                if name == 'callback recipe':
                    self.prompt = replace(self.prompt, requires_waiting=True)
                    self.catalog = [self.prompt]
                    agents.update_runtime(self.directory, self.launch, status='waiting')
                self.prepare.side_effect = lambda s, a, e: NativeResult('ok', value=recipe('prompt-' + a.id))
                result = self.answer()
                self.assertEqual(result.state, 'refused')
                self.assertNotIn('PRIVATE', str(result))
                self.assertEqual(self.answer().cause, 'already_answered')
        self.assertFalse(self.calls)
        self.assertFalse(list((self.directory / 'prompts').glob('*.answer.json')))
        for state in ('answered', 'sent_first', 'refused'):
            with self.subTest(bare_outcome=state):
                self.fresh()
                self.prepare.side_effect = lambda *args: NativeResult('ok', value={'state': state, 'reason': 'PRIVATE'})
                outcome = self.answer()
                self.assertEqual(outcome.state, 'refused' if state == 'refused' else 'unknown')
                self.assertNotIn('PRIVATE', str(outcome))
        self.fresh()
        text = 'é' * (64 * 1024)
        self.prepare.side_effect = lambda s, a, e: NativeResult('ok', value=PromptInput('prompt-' + a.id, text))
        self.assertEqual(self.answer().state, 'submitted')
        wire = next(params['input'][0]['text'] for method, params in self.calls if method == 'thread/queue/add')
        self.assertEqual(len(wire.encode('utf-8')), 128 * 1024)

    def test_new_input_uses_one_normal_fifo_restart_and_receipts_never_restart(self):
        """Async answers must use the same paused FIFO restart as a new message, never a receipt-driven resend."""
        for state, predecessors in (('idle', []), ('paused', ['older-native-input'])):
            with self.subTest(queue=state):
                self.fresh()
                self.queue = [{'id': 'older', 'clientUserMessageId': id} for id in predecessors]
                agents.update_runtime(self.directory, self.launch,
                    queue_state={'native_session_id': self.thread, 'state': state})
                store.write(self.directory / 'focus.json', {'id': str(uuid4()), 'since': store.now(), 'by': 'user'})
                sent = self.answer()
                self.assertEqual(sent.state, 'submitted')
                input_id = 'prompt-' + self.prompt.id
                self.assertEqual(self.started[-1:], [predecessors[0] if predecessors else input_id])
                record = self.directory / 'prompts' / (self.prompt.id + '.json')
                saved = store.read(record)
                self.assertEqual((saved['input_id'], saved['session_generation'], saved['requires_waiting']),
                                 (input_id, 1, False))
                self.assertNotIn('PRIVATE', record.read_text())
                self.assertFalse((self.directory / 'inbox.jsonl').exists())
                self.assertFalse(record.with_name(self.prompt.id + '.answer.json').exists())
                listed = prompts.waiting(self.root, self.agent)
                self.assertEqual([p.id for p in listed.prompts], [self.prompt.id])
                self.assertFalse(listed.prompts[0].answerable)
                before = len(self.started), sum(m == 'thread/queue/add' for m, _ in self.calls)
                for _ in range(2):
                    self.assertIn(self.result().state, ('submitted', 'unknown'))
                    self.assertEqual(self.answer().cause, 'already_answered')
                self.assertEqual((len(self.started), sum(m == 'thread/queue/add' for m, _ in self.calls)), before)
                self.queue.clear()
                self.confirmed.add(input_id)
                confirmed = self.result()
                self.assertEqual((confirmed.state, confirmed.reason), ('answered', prompts.INPUT_ANSWERED_REASON))
                self.assertTrue((self.directory / 'focus.json').exists())
        self.callback.assert_not_called()
        self.assertNotIn('PRIVATE', str(self.events.call_args_list))

    def test_uncertainty_and_identity_fences_keep_input_out_of_a_new_conversation(self):
        """Lost native writes and generation changes must neither resend nor authorize a conversation switch."""
        for method, status in (('thread/queue/add', 'idle'), ('turn/steer', 'active')):
            with self.subTest(uncertain_native_write=method):
                self.fresh()
                self.fail_method, self.status = method, status
                self.calls.clear()
                self.assertEqual(self.answer().state, 'unknown')
                self.assertNotIn('PRIVATE', str(self.result()))
                self.assertEqual(sum(m == method for m, _ in self.calls), 1)
                self.assertFalse(any(m in ('thread/resume', 'turn/start') for m, _ in self.calls))
                if method == 'turn/steer':
                    self.assertFalse(any(m == 'thread/queue/add' for m, _ in self.calls))
                self.fail_method = None
                self.calls.clear()
                self.assertEqual(self.answer().cause, 'already_answered')
                self.assertFalse(self.calls)
                if status == 'idle':
                    self.confirmed.add('prompt-' + self.prompt.id)
                    self.assertEqual(self.result().state, 'answered')
        self.status = 'idle'
        event = {'previous': self.thread, 'reason': 'new', 'target': None, 'correlation': 'owner:1'}
        with patch.object(recovery, 'check', return_value=NativeResult('ok')) as probe:
            with self.assertRaisesRegex(ValueError, self.prompt.id):
                navigation.begin(self.root, self.directory, self.launch, event)
            probe.assert_not_called()
        properties = store.read(self.directory / 'properties.json')
        facts = store.read(self.directory / 'runtime.json')
        rows = [('properties.json', {'native_session_id': str(uuid4())}),
                ('properties.json', {'workspace': str(self.work / 'other')}),
                ('properties.json', {'native_session_home': str(self.work / 'other-home')}),
                ('properties.json', {'harness': 'other'}),
                ('runtime.json', {'launch_id': str(uuid4())}),
                ('runtime.json', {'session_generation': 2}),
                ('runtime.json', {'session_generation': True}),
                ('runtime.json', {'control_endpoint': {'transport': 'unix-websocket', 'address': '/other.sock'}}),
                ('runtime.json', {'state': 'stopped'}),
                ('runtime.json', {'navigation': {'state': 'checking'}})]
        for filename, changes in rows:
            with self.subTest(fence=changes):
                original = properties if filename == 'properties.json' else facts
                store.write(self.directory / filename, {**original, **changes})
                self.calls.clear()
                self.assertEqual(self.result().state, 'unknown')
                self.assertFalse(self.calls, 'Changed identity must not select a new receipt endpoint')
                store.write(self.directory / filename, original)
        self.confirmed.add('prompt-' + self.prompt.id)
        self.assertEqual(self.result().state, 'answered')
        path = self.directory / 'prompts' / (self.prompt.id + '.json')
        confirmed = store.read(path)
        def changed_tracking(*args):
            store.write(path, {**confirmed, 'result': {**confirmed['result'], 'state': 'submitted'}})
            return NativeResult('ok')
        with patch.object(recovery, 'check', side_effect=changed_tracking), \
                self.assertRaisesRegex(ValueError, self.prompt.id):
            navigation.begin(self.root, self.directory, self.launch, event)
        store.write(path, confirmed)
        with patch.object(recovery, 'check', return_value=NativeResult('ok')):
            ticket = navigation.begin(self.root, self.directory, self.launch, event)
        self.assertIn('token', ticket)
        self.calls.clear()
        self.assertEqual(self.result().state, 'answered')
        self.assertFalse(self.calls, 'Confirmed input must stay confirmed even after the session changes')

    def test_proved_failure_releases_navigation_but_ambiguous_errors_do_not(self):
        """Definite non-delivery must not strand navigation, while malformed or contradictory proof cannot release it."""
        cases = [(NativeResult('error', 'PRIVATE', {'definite_failure': True}), 'refused')]
        cases += [(NativeResult('error', 'PRIVATE', {'definite_failure': flag}), 'unknown')
                  for flag in (False, None, 1, 'true', {})]
        cases += [(NativeResult(state, 'PRIVATE', {'definite_failure': True}), 'unknown')
                  for state in ('unknown', 'unavailable', 'ok', 'native_queued', 'pending')]
        cases += [(NativeResult(state, 'PRIVATE', {'definite_failure': True, 'transcript_confirmed': True}), 'unknown')
                  for state in ('error', 'ok', 'native_queued', 'pending')]
        cases += [(NativeResult(state, 'PRIVATE', value), expected)
                  for state in ('ok', 'native_queued', 'pending')
                  for value, expected in (({}, 'submitted'), ({'transcript_confirmed': True}, 'answered'),
                                          ({'definite_failure': 1}, 'submitted'))]
        generation = 1
        for stage in ('send', 'receipt'):
            for reply, expected in cases:
                with self.subTest(stage=stage, reply=reply):
                    self.fresh()
                    generation += 1
                    agents.update_runtime(self.directory, self.launch, session_generation=generation)
                    immediate = reply if stage == 'send' else NativeResult('native_queued')
                    with patch.object(native, 'submit_input', return_value=immediate) as submit, \
                            patch.object(self.adapter, 'get_delivery', return_value=reply) as receipt:
                        outcome = self.answer()
                        if stage == 'receipt':
                            self.assertEqual(outcome.state, 'submitted')
                            outcome = self.result()
                        self.assertEqual(outcome.state, expected)
                        self.assertNotIn('PRIVATE', str(outcome))
                        pending = prompts.pending_inputs(self.directory, self.launch, self.thread, generation)
                        self.assertEqual(pending, None if expected in ('refused', 'answered') else self.prompt.id)
                        self.assertEqual(self.answer().cause, 'already_answered')
                        self.assertEqual(submit.call_count, 1)
                        self.assertEqual(receipt.call_count, int(stage == 'receipt'))
