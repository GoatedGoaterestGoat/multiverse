"""Native typed prompt choices are narrower than the app-server's wire union."""
from copy import deepcopy
import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from multiverse.harnesses.codex import prompt_codec as codec
from multiverse.harnesses.codex import prompt_files, prompts
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.harnesses.types import NativeEndpoint, PromptAnswer, SessionHandle
from multiverse.runtime import rpc, native as runtime_native, prompts as core_prompts
from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve
from isolation import isolate


COMMAND = 'item/commandExecution/requestApproval'
FILE = 'item/fileChange/requestApproval'
PERMISSIONS = 'item/permissions/requestApproval'
QUESTION = 'item/tool/requestUserInput'
MCP = 'mcpServer/elicitation/request'
NATIVE_VERSION = '.'.join(map(str, codec.VERSION))


def params(**values):
    return {'threadId': 'current', 'turnId': 'turn-one', 'itemId': 'item-one', **values}


def answer(choice=None, answers=None, **values):
    return {'choice': choice, 'answers': answers or {}, 'allow_rule': False, **values}


class PromptCodecTest(unittest.TestCase):
    def test_command_decisions_keep_only_offered_shapes_and_person_only_rules(self):
        """An approval enum includes persistent rules, so copying arbitrary caller decisions could widen authority."""
        rule = {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': {'command': ['echo', 'safe']}}}
        network = {'applyNetworkPolicyAmendment': {'network_policy_amendment': {'host': 'example.invalid', 'action': 'deny'}}}
        offered = ['accept', 'acceptForSession', rule, network, 'decline', 'cancel']
        offer = codec.describe(COMMAND, params(command='echo safe', availableDecisions=offered))
        self.assertTrue(offer.answerable)
        self.assertEqual([row['id'] for row in offer.choices],
                         ['allow', 'allow_session', 'allow_rule_2', 'network_rule_3', 'deny', 'stop'])
        self.assertEqual([row['saves_rule'] for row in offer.choices], [False, False, True, True, False, False])
        for action in ('allow', 'deny'):
            decision = {'applyNetworkPolicyAmendment': {'network_policy_amendment': {'host': 'example.invalid', 'action': action}}}
            network_offer = codec.describe(COMMAND, params(availableDecisions=[decision]))
            self.assertTrue(network_offer.choices[0]['saves_rule'])
            with self.assertRaisesRegex(ValueError, 'Only the person'):
                network_offer.response(answer(network_offer.choices[0]['id']))
        for row, expected in zip(offer.choices, offered):
            with self.subTest(choice=row['id']):
                if row['saves_rule']:
                    with self.assertRaisesRegex(ValueError, 'Only the person'):
                        offer.response(answer(row['id']))
                self.assertEqual(offer.response(answer(row['id'], allow_rule=True)), {'decision': expected})
        with self.assertRaises(ValueError):
            offer.response(answer('not_offered'))
        restricted = codec.describe(COMMAND, params(availableDecisions=['cancel']))
        self.assertEqual(restricted.response(answer('stop')), {'decision': 'cancel'})
        with self.assertRaises(ValueError):
            restricted.response(answer('allow'))
        human = codec.describe(COMMAND, params(kind='humanCommand', availableDecisions=['accept', 'decline', 'cancel']))
        self.assertFalse(human.answerable, 'A native fresh-human checkpoint must stay in the terminal')
        for person in (False, True):
            with self.assertRaises(ValueError):
                human.response(answer('allow', allow_rule=person))
        for kind in (None, 'futureKind'):
            self.assertFalse(codec.describe(COMMAND, params(kind=kind, availableDecisions=['accept'])).answerable)
        hidden = deepcopy(rule)
        hidden['acceptWithExecpolicyAmendment']['execpolicy_amendment']['command'] = ['echo', 'one\ntwo']
        hidden_offer = codec.describe(COMMAND, params(availableDecisions=[hidden, 'accept']))
        self.assertEqual([row['id'] for row in hidden_offer.choices], ['allow'])
        for decisions in [None, [], ['futureDecision'], [True], [{'applyNetworkPolicyAmendment': {}}]]:
            with self.subTest(decisions=decisions):
                result = codec.describe(COMMAND, params(availableDecisions=decisions))
                self.assertFalse(result.answerable)
                self.assertEqual(result.choices, [])

    def test_file_diffs_and_permission_grants_preserve_the_exact_requested_values(self):
        """A file prompt without its exact item or a reconstructed permission profile hides what is approved."""
        item = {'id': 'item-one', 'type': 'fileChange', 'changes': [
            {'path': '/workspace/file', 'kind': {'type': 'update'}, 'diff': '-old\n+new\n'}]}
        for observed, eligible in [(item, True), (None, False), ({**item, 'id': 'other'}, False)]:
            with self.subTest(item=observed):
                offer = codec.describe(FILE, params(), item=observed)
                self.assertEqual(offer.answerable, eligible)
                if eligible:
                    self.assertEqual(offer.detail['changes'], item['changes'])
                    self.assertEqual(offer.response(answer('allow_session')), {'decision': 'acceptForSession'})
                    with self.assertRaises(ValueError):
                        offer.response(answer('deny'))  # The TUI does not offer wire-level decline.
        profile = {'network': {'enabled': True}, 'fileSystem': {'read': None, 'write': ['/extra'],
            'entries': [{'path': {'type': 'special', 'value': {'kind': 'project_roots', 'subpath': None}}, 'access': 'write'},
                        {'path': {'type': 'glob_pattern', 'pattern': '/safe/*'}, 'access': 'read'}]}}
        offer = codec.describe(PERMISSIONS, params(permissions=profile))
        self.assertTrue(offer.answerable)
        self.assertEqual(offer.response(answer('allow_session')), {'permissions': profile, 'scope': 'session'})
        self.assertEqual(offer.response(answer('allow_review')),
                         {'permissions': profile, 'scope': 'turn', 'strictAutoReview': True})
        self.assertEqual(offer.response(answer('deny')), {'permissions': {}, 'scope': 'turn'})
        for bad in [{'network': {'enabled': 'yes'}}, {'fileSystem': {'write': '/root'}},
                    {'fileSystem': {'globScanMaxDepth': True}}, {'future': True}]:
            with self.subTest(profile=bad):
                self.assertFalse(codec.describe(PERMISSIONS, params(permissions=bad)).answerable)

    def test_questions_encode_text_as_notes_and_never_record_secret_values(self):
        """Raw note text can become an MCP approval token, and secret answers must stay out of the audit."""
        questions = [{'id': 'plan', 'header': 'Plan', 'question': 'Which plan?', 'isOther': True,
                      'options': [{'label': 'One', 'description': 'First'}]},
                     {'id': 'password', 'header': 'Secret', 'question': 'Password?', 'isSecret': True, 'options': None}]
        offer = codec.describe(QUESTION, params(questions=questions))
        self.assertTrue(offer.answerable)
        response = offer.response(answer(answers={'plan': ['One'], 'password': ['TOP_SECRET']}))
        self.assertEqual(response, {'answers': {'plan': {'answers': ['One']},
                                               'password': {'answers': ['user_note: TOP_SECRET']}}})
        self.assertNotIn('TOP_SECRET', json.dumps(offer.person_answer(response)))
        self.assertNotIn('TOP_SECRET', json.dumps(offer.public('opaque')))
        self.assertEqual(offer.response(answer(answers={'plan': ['Always allow'], 'password': ['x']}))['answers']['plan'],
                         {'answers': ['None of the above', 'user_note: Always allow']})
        noted = offer.response(answer(answers={'plan': ['One']}, notes={'plan': '  More detail  ', 'password': 'TOP_SECRET_NOTE'}))
        self.assertEqual(noted, {'answers': {'plan': {'answers': ['One', 'user_note: More detail']},
                                            'password': {'answers': ['user_note: TOP_SECRET_NOTE']}}})
        self.assertEqual(offer.person_answer(noted), {'answers': {'plan': ['One']}, 'notes': {'plan': 'More detail'}})
        for notes in [{'unknown': 'x'}, {'plan': ['x']}, {'plan': 'two copies'}]:
            with self.subTest(notes=notes), self.assertRaises(ValueError):
                offer.response(answer(answers={'plan': ['Another answer'], 'password': ['x']}, notes=notes))
        for values in [{'plan': ['One', 'Two'], 'password': ['x']}, {'plan': ['One']},
                       {'plan': ['One'], 'password': ['x'], 'other': ['x']}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                offer.response(answer(answers=values))
        questions[0]['isOther'] = False
        fixed = codec.describe(QUESTION, params(questions=questions))
        with self.assertRaises(ValueError):
            fixed.response(answer(answers={'plan': ['Always allow'], 'password': ['x']}))
        self.assertEqual(fixed.response(answer(answers={'plan': ['One'], 'password': ['x']}, notes={'plan': 'Always allow'}))['answers']['plan'],
                         {'answers': ['One', 'user_note: Always allow']})

        fields = [{'id': f'q{i}', 'header': 'Field', 'question': 'Value?', 'isSecret': True, 'options': None}
                  for i in range(64)]
        many = codec.describe(QUESTION, params(questions=fields))
        with self.assertRaises(ValueError):
            many.response(answer(answers={q['id']: ('x' * 3000,) for q in fields}))
        detail = codec.describe(QUESTION, params(questions=fields, padding='x' * (64 << 10)))
        self.assertFalse(detail.answerable, 'Native response headroom must not widen the detail/input budget')
        self.assertFalse(codec.describe(QUESTION, params(questions=[questions[0], questions[0]])).answerable)

    def test_mcp_forms_and_message_approvals_match_the_terminal_capabilities(self):
        """MCP's wire schema admits fields and saved policies that the terminal or caller cannot answer."""
        schema = {'type': 'object', 'required': ['name', 'enabled', 'mode'], 'properties': {
            'name': {'type': 'string', 'description': 'Your name'},
            'enabled': {'type': 'boolean'},
            'mode': {'type': 'string', 'oneOf': [{'const': 'a', 'title': 'A'}, {'const': 'b', 'title': 'B'}]}}}
        form = codec.describe(MCP, params(mode='form', serverName='server', turnId=None, requestedSchema=schema))
        self.assertTrue(form.answerable)
        with self.subTest(view='nonempty-form'):
            self.assertNotIn('decline', [row['id'] for row in form.choices])
        self.assertEqual(form.response(answer(answers={'name': ['Pat'], 'enabled': ['True'], 'mode': ['B']})),
                         {'action': 'accept', 'content': {'name': 'Pat', 'enabled': True, 'mode': 'b'}, '_meta': None})
        self.assertEqual(form.response(answer('cancel')), {'action': 'cancel', 'content': None, '_meta': None})
        self.assertEqual(form.person_answer({'action': 'accept', 'content': {'name': 'Pat', 'enabled': True, 'mode': 'b'}}),
                         {'answers': {'name': ['Pat'], 'enabled': ['True'], 'mode': ['B']}})
        for decision in ('decline', 'cancel'):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                form.response(answer(decision, answers={'name': ['Pat']}))
        for field in ({'type': 'string', 'format': 'password'}, {'type': 'string', 'minLength': True},
                      {'type': 'string', 'maxLength': -1}, {'type': 'string', 'default': False},
                      {'type': 'boolean', 'default': 'true'}, {'type': 'string', 'title': 3},
                      {'type': 'string', 'enum': ['a'], 'oneOf': []},
                      {'type': 'string', 'enum': ['a'], 'format': 'email'}):
            with self.subTest(field=field):
                malformed = codec.describe(MCP, params(mode='form', requestedSchema={
                    'type': 'object', 'properties': {'field': field}}))
                self.assertFalse(malformed.answerable)
        for format in ('email', 'uri', 'date', 'date-time'):
            valid = codec.describe(MCP, params(mode='form', requestedSchema={'type': 'object',
                'properties': {'field': {'type': 'string', 'format': format, 'minLength': 0, 'maxLength': 10}}}))
            self.assertTrue(valid.answerable)
        for mode, shape in [('url', None), ('openai/userVerification', None),
                ('form', {'type': 'object', 'properties': {'n': {'type': 'number'}}}),
                ('openai/form', {'type': 'array', 'properties': {}}),
                ('form', {'type': 'object', 'properties': {'m': {'type': 'array', 'items': {'type': 'string'}}}})]:
            with self.subTest(mode=mode, shape=shape):
                result = codec.describe(MCP, params(mode=mode, requestedSchema=shape))
                self.assertFalse(result.answerable)
                self.assertTrue(result.reason)
        for persist in [['session', 'always'], 'always', []]:
            approval = codec.describe(MCP, params(mode='openai/form', requestedSchema=None,
                _meta={'persist': persist, 'codex_approval_kind': 'mcp_tool_call', 'tool_name': 'example'}))
            self.assertTrue(approval.answerable)
            self.assertNotIn('decline', approval.results)
            cancelled = {'action': 'cancel', 'content': None,
                         '_meta': {'codex_approval_cancellation_reason': 'user'}}
            with self.subTest(cancel=persist):
                self.assertEqual(approval.response(answer('cancel')), cancelled)
                self.assertEqual(approval.person_answer(cancelled), {'choice': 'cancel'})
            if 'always' in persist:
                with self.assertRaisesRegex(ValueError, 'Only the person'):
                    approval.response(answer('allow_always'))
                self.assertEqual(approval.response(answer('allow_always', allow_rule=True)),
                                 {'action': 'accept', 'content': None, '_meta': {'persist': 'always'}})
            with self.assertRaises(ValueError):
                approval.response(answer('allow', answers={'_meta': ['persist:always']}))

    def test_bad_and_oversized_inputs_have_no_partial_answer_path(self):
        """A malformed first response consumes the native callback, so invalid choices must fail before send."""
        offer = codec.describe(COMMAND, params(command='safe', availableDecisions=['accept']))
        for value in [answer('allow', message='not offered'), answer('allow', allow_rule=1),
                      {**answer('allow'), 'answers': []}, answer({'decision': 'accept'}), answer('allow', answers={'x': ['bad']})]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                offer.response(value)
        large = codec.describe(COMMAND, params(command='x' * codec.DETAIL_BYTES, availableDecisions=['accept']))
        self.assertFalse(large.answerable)
        self.assertEqual(large.detail, {'terminal_only': True, 'truncated': True})
        self.assertEqual(large.choices, [])
        self.assertIsNone(codec.describe('unknown/request', params()))


class PromptRelayTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mv2-codex-prompt-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.launch, self.native = str(uuid4()), str(uuid4())
        root = SimpleNamespace(native_id=self.native, bound=True, launch_refusal=None,
                               settings={'navigation_cli': codec.VERSION})
        self.connection = SimpleNamespace(root=root, version=codec.VERSION, owns_primary=lambda: True,
                                          ended=False, connection_id=str(uuid4()))
        self.relay = prompts.Relay(self.connection, {'agent_directory': str(self.directory), 'launch_id': self.launch})
        self.relay.observe('outgoing', {'id': 'normal-turn', 'method': 'turn/start', 'params': {'threadId': self.native}})
        self.relay.observe('incoming', {'id': 'normal-turn', 'result': {'turn': {'id': 'turn-one'}}})
        self.session = SessionHandle('agent', 'codex', '/workspace', launch_id=self.launch, native_id=self.native,
            session_file=str(self.directory / 'session.jsonl'), state='running', status='waiting',
            endpoint=NativeEndpoint('unix-websocket', str(self.directory / 'relay')),
            native_versions={'cli': NATIVE_VERSION, 'daemon': NATIVE_VERSION}, native_relay={'state': 'ready'},
            native_mode={'mode': 'bridge', 'policy': 'required'})


    def flush(self):
        jobs, self.relay.jobs = self.relay.jobs, prompts.Jobs()
        self.relay.files.apply(jobs)

    def request(self, method=COMMAND, wire=7, **changes):
        values = params(threadId=self.native, availableDecisions=['accept', 'acceptForSession', 'decline', 'cancel'])
        values.update(changes)
        self.relay.observe('incoming', {'id': wire, 'method': method, 'params': values})
        self.flush()
        return self.relay.by_request[prompts.request_id(wire)]

    def write_answer(self, id, **changes):
        folder = self.directory / 'prompts'
        folder.mkdir(exist_ok=True, mode=0o700)
        payload = {'schema': 1, 'id': id, 'launch_id': self.launch, 'native_id': self.native,
                   'message': None, **answer('allow'), **changes}
        path = folder / (id + '.answer.json')
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
            json.dump(payload, out)
        return path

    def resolve(self, wire=7):
        self.relay.observe('incoming', {'method': 'serverRequest/resolved',
            'params': {'threadId': self.native, 'requestId': wire}})
        self.flush()

    def result(self, id):
        return prompts.outcome(self.session, SimpleNamespace(id=id)).value

    def test_both_response_orders_are_single_send_and_the_result_never_claims_acceptance(self):
        """The owning connection orders replies but native resolution cannot prove whose decision or grant scope won."""
        for first in ('person', 'mv2'):
            with self.subTest(first=first):
                id = self.request(wire=first)
                self.write_answer(id, choice='allow_session')
                candidate = self.relay.files.take(id)
                terminal = {'id': first, 'result': {'decision': 'decline'}}
                if first == 'person':
                    self.assertTrue(self.relay.terminal(terminal))
                    self.assertIsNone(self.relay.claim(candidate))
                    self.flush()
                    result = self.result(id)
                    self.assertEqual(result['state'], 'person')
                    self.assertEqual(result['person_answer'], {'choice': 'deny'})
                    self.assertEqual(result['reason'],
                        'The person replied to this prompt in the terminal first; mv2 did not send an answer.')
                else:
                    frame = self.relay.claim(candidate)
                    self.assertEqual(frame, {'jsonrpc': '2.0', 'id': first, 'result': {'decision': 'acceptForSession'}})
                    self.relay.sent(candidate, True)
                    self.assertFalse(self.relay.terminal(terminal))
                    self.resolve(first)
                    result = self.result(id)
                    self.assertEqual(result['state'], 'sent_first')
                    self.assertIn('does not confirm', result['reason'])
                    self.assertIn('session grant', result['reason'])
                    self.assertEqual(candidate.answer, {})
                self.assertFalse((self.directory / 'prompts' / (id + '.answer.json')).exists())
                self.assertNotEqual(result['state'], 'answered')

        # An untouched nonblocking question can be answered by the terminal's countdown.
        # Its empty reply is indistinguishable from the person submitting no answers.
        for secret in (False, True):
            for empty in (False, True):
                for first in ('terminal', 'mv2'):
                    with self.subTest(countdown_empty=empty, secret=secret, first=first):
                        wire = f'countdown-{secret}-{empty}-{first}'
                        id = self.request(QUESTION, wire=wire, isBlocking=False, questions=[{
                            'id': 'one', 'header': 'One', 'question': 'Value?', 'isSecret': secret, 'options': None}])
                        self.write_answer(id, choice=None, answers={'one': ['MV2 PRIVATE DRAFT']})
                        candidate = self.relay.files.take(id)
                        terminal_value = 'SECRET TERMINAL VALUE' if secret else 'PUBLIC TERMINAL VALUE'
                        native = {} if empty else {'one': {'answers': ['user_note: ' + terminal_value]}}
                        terminal = {'id': wire, 'result': {'answers': native}}
                        if first == 'terminal':
                            self.assertTrue(self.relay.terminal(terminal))
                            self.assertIsNone(self.relay.claim(candidate))
                            self.flush()
                            result = self.result(id)
                            self.assertEqual((result['state'], result['cause']), ('person', 'person'))
                            self.assertIn('did not send', result['reason'])
                            if empty:
                                self.assertIn('no answers', result['reason'])
                                self.assertIn('countdown', result['reason'])
                                self.assertIn('may also', result['reason'])
                                self.assertNotIn('The person replied', result['reason'])
                            else:
                                self.assertEqual(result['reason'],
                                    'The person replied to this prompt in the terminal first; mv2 did not send an answer.')
                            if secret:
                                self.assertFalse('SECRET TERMINAL VALUE' in repr(self.relay.records))
                                for path in self.directory.rglob('*.json'):
                                    self.assertFalse('SECRET TERMINAL VALUE' in path.read_text())
                        else:
                            self.assertIsNotNone(self.relay.claim(candidate))
                            self.relay.sent(candidate, True)
                            self.assertFalse(self.relay.terminal(terminal))
                            self.resolve(wire)
                            self.assertEqual(self.result(id)['state'], 'sent_first')
                        self.assertEqual(candidate.answer, {})
                        self.assertFalse((self.directory / 'prompts' / (id + '.answer.json')).exists())

        # Terminal-only placeholders retain the flag, never the hidden request body.
        for route in ('side', 'review'):
            for empty in (False, True):
                wire = f'{route}-countdown-{empty}'
                if route == 'side':
                    self.connection.side_id = 'side-thread'
                else:
                    self.relay.mode = 'review'
                id = self.request(QUESTION, wire=wire, isBlocking=False,
                    threadId='side-thread' if route == 'side' else self.native, questions=[{
                        'id': 'hidden', 'header': 'Hidden', 'question': 'Hidden body?', 'isSecret': True, 'options': None}])
                record = self.relay.records[id]
                self.assertFalse(record['offer'].answerable)
                self.assertEqual(record['offer'].params, {})
                result = {'answers': {}} if empty else {'answers': {'hidden': {'answers': ['SECRET SIDE VALUE']}}}
                self.assertTrue(self.relay.terminal({'id': wire, 'result': result}))
                self.flush()
                reason = self.result(id)['reason']
                self.assertEqual('countdown' in reason, empty)
                self.assertEqual(reason.startswith('The person replied'), not empty)
                self.assertFalse('SECRET SIDE VALUE' in repr(self.relay.records))
                self.relay.mode = 'normal'

        # Blocking and legacy questions have no automatic terminal countdown reply.
        for blocking in (True, 'absent'):
            wire = f'blocking-{blocking}'
            fields = {} if blocking == 'absent' else {'isBlocking': blocking}
            id = self.request(QUESTION, wire=wire, questions=[{
                'id': 'one', 'header': 'One', 'question': 'Value?', 'isSecret': False, 'options': None}], **fields)
            self.assertTrue(self.relay.terminal({'id': wire, 'result': {'answers': {}}}))
            self.flush()
            self.assertEqual(self.result(id)['reason'],
                'The person replied to this prompt in the terminal first; mv2 did not send an answer.')

        rule = {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': {'command': ['echo', 'safe']}}}
        for label, metadata, choice, allow_rule, expected in (
            ('legacy', {}, 'allow', False, {'decision': 'accept'}),
            ('person', {'caller': {'kind': 'person'}}, 'allow', False, {'decision': 'accept'}),
            ('plugin', {'caller': {'kind': 'plugin', 'name': 'example'}}, 'allow', False, {'decision': 'accept'}),
            ('person-rule', {'caller': {'kind': 'person'}}, 'allow_rule_0', False, None),
            ('plugin-rule', {'caller': {'kind': 'plugin', 'name': 'example'}}, 'allow_rule_0', False, None),
            ('authorized-rule', {'caller': {'kind': 'person'}}, 'allow_rule_0', True, {'decision': rule}),
            ('invalid-authority', {'caller': {'kind': 'person'}}, 'allow', 1, None),
            ('unknown-field', {'caller': {'kind': 'person'}, 'authority': True}, 'allow', False, None),
        ):
            with self.subTest(caller=label):
                wire = 'caller-' + label
                id = self.request(wire=wire, availableDecisions=[rule, 'accept'])
                self.write_answer(id, choice=choice, allow_rule=allow_rule, **metadata)
                candidate = self.relay.files.take(id)
                frame = self.relay.claim(candidate)
                self.flush()
                self.assertEqual(frame, None if expected is None else
                                 {'jsonrpc': '2.0', 'id': wire, 'result': expected})
                if expected is None:
                    self.assertEqual(self.result(id)['state'], 'refused')
                    self.assertEqual(candidate.answer, {})
                else:
                    self.relay.sent(candidate, True)
                    self.assertEqual(candidate.answer, {})

    def test_stale_handles_turn_end_and_current_thread_isolation_never_retarget_an_answer(self):
        """Waiting on B must never turn a late answer for closed A into permission for B."""
        old = self.request(wire=1)
        self.write_answer(old)
        candidate = self.relay.files.take(old)
        self.resolve(1)
        current = self.request(wire='1')
        self.assertNotEqual(current, old)
        self.connection.side_id = 'known-other-thread'
        self.relay.observe('incoming', {'method': 'serverRequest/resolved',
            'params': {'threadId': 'known-other-thread', 'requestId': '1'}})
        self.assertTrue(self.relay.records[current]['open'])
        self.connection.side_id = None
        self.assertIsNone(self.relay.claim(candidate))
        self.assertEqual(self.result(old)['state'], 'gone')
        self.assertEqual([row['id'] for row in prompts.catalog(self.session).value['prompts']], [current])
        self.relay.observe('incoming', {'method': 'turn/completed',
            'params': {'threadId': self.native, 'turn': {'id': 'turn-one', 'status': 'interrupted'}}})
        self.flush()
        self.assertEqual(self.result(current)['cause'], 'interrupted')
        for change in [dict(version=(0, 164, 0)), dict(owns_primary=lambda: False)]:
            before = len(self.relay.records)
            old_values = {key: getattr(self.connection, key) for key in change}
            for key, value in change.items():
                setattr(self.connection, key, value)
            self.relay.observe('incoming', {'method': COMMAND, 'id': 'foreign',
                'params': params(threadId=self.native, availableDecisions=['accept'])})
            self.assertEqual(len(self.relay.records), before)
            for key, value in old_values.items():
                setattr(self.connection, key, value)
        self.relay.observe('incoming', {'method': COMMAND, 'id': 'side',
            'params': params(threadId='side-thread', availableDecisions=['accept'])})
        self.assertNotIn(prompts.request_id('side'), self.relay.by_request)
        self.connection.side_id = 'side-thread'
        self.relay.observe('incoming', {'method': COMMAND, 'id': 'known-side',
            'params': params(threadId='side-thread', command='PRIVATE SIDE CONTENT', availableDecisions=['accept'])})
        self.flush()
        side_id = self.relay.by_request[prompts.request_id('known-side')]
        shown = next(row for row in prompts.catalog(self.session).value['prompts'] if row['id'] == side_id)
        self.assertFalse(shown['answerable'])
        self.assertIn('/btw', shown['reason'])
        self.assertNotIn('PRIVATE SIDE CONTENT', repr(self.relay.records[side_id]))
        replay = {'method': COMMAND, 'id': 'known-side', 'params': params(threadId='side-thread',
            command='PRIVATE SIDE CONTENT', availableDecisions=['accept'])}
        self.relay.observe('incoming', replay)
        self.assertFalse(self.relay.disabled)
        self.assertEqual(self.relay.by_request[prompts.request_id('known-side')], side_id)
        self.write_answer(side_id)
        candidate = self.relay.files.take(side_id)
        self.assertIsNone(self.relay.claim(candidate))
        self.assertTrue(self.relay.terminal({'id': 'known-side', 'result': {'decision': 'accept'}}))
        self.connection.side_id = None
        self.relay.observe('incoming', {'method': 'serverRequest/resolved',
            'params': {'threadId': 'side-thread', 'requestId': 'known-side'}})
        self.flush()
        self.assertFalse(self.relay.records[side_id]['open'])
        self.connection.side_id = 'side-thread'
        self.relay.observe('incoming', {**replay, 'params': {**replay['params'], 'command': 'DIFFERENT PRIVATE CONTENT'}})
        self.assertTrue(self.relay.disabled, 'Changed data under the same native request ID must still fail closed')
        self.connection.side_id = None
        self.relay.disabled = False  # Continue independent version and secondary-connection controls.
        self.assertFalse(prompts.available(replace(self.session, native_versions={'cli': '0.164.0', 'daemon': '0.166.0'})))
        for cli, daemon, expected in (('0.166.0', '0.166.0', True), ('0.167.0', '0.167.0', True),
                                      ('0.166.0', '0.167.0', False), ('0.167.0', '0.166.0', False),
                                      ('0.167.1', '0.167.1', True), ('0.167.0', '0.167.1', False),
                                      ('0.167.1', '0.167.0', False), ('0.167.2', '0.167.2', True),
                                      ('0.167.1', '0.167.2', False), ('0.167.2', '0.167.1', False),
                                      ('0.167.3', '0.167.3', False)):
            with self.subTest(prompt_versions=(cli, daemon)):
                self.assertEqual(prompts.available(replace(self.session,
                    native_versions={'cli': cli, 'daemon': daemon})), expected)
                root = SimpleNamespace(**{**vars(self.connection.root), 'settings': {
                    **self.connection.root.settings, 'navigation_cli': list(map(int, cli.split('.')))}})
                connection = SimpleNamespace(root=root, version=tuple(map(int, daemon.split('.'))),
                    owns_primary=lambda: True, ended=False, connection_id='versions')
                relay = prompts.Relay(connection, {'agent_directory': str(self.directory), 'launch_id': self.launch})
                self.assertEqual(relay.eligible(), expected)
        for malformed in ([0, 167, 0], {'version': '0.167.0'}):
            self.assertFalse(prompts.available(replace(self.session,
                native_versions={'cli': malformed, 'daemon': '0.167.0'})))
        before = self.relay.files.read('index.json')
        secondary = prompts.Relay(SimpleNamespace(root=self.connection.root, version=codec.VERSION,
            owns_primary=lambda: False, ended=False, connection_id='secondary'),
            {'agent_directory': str(self.directory), 'launch_id': self.launch})
        self.assertTrue(secondary.terminal({'id': 'unmanaged', 'result': {}}))
        secondary.disable()
        secondary.files.apply(secondary.jobs)
        self.assertEqual(self.relay.files.read('index.json'), before, 'A secondary connection cannot replace the owner inventory')

    def test_resolution_cancellation_effects_and_secret_outputs_remain_qualified(self):
        """Native effects can contradict a sent choice but cannot confirm session scope or reveal secret answers."""
        id = self.request(wire=1)
        self.write_answer(id)
        candidate = self.relay.files.take(id)
        self.relay.claim(candidate)
        self.relay.sent(candidate, True)
        self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': self.native,
            'turnId': 'turn-one', 'item': {'id': 'item-one', 'type': 'commandExecution', 'status': 'declined'}}})
        self.resolve(1)
        self.assertEqual(self.result(id)['state'], 'unknown')
        secret = 'SECRET_MUST_NOT_PERSIST'
        id = self.request(QUESTION, wire=2, questions=[{'id': 'secret', 'header': 'Secret', 'question': 'Password?',
            'isSecret': True, 'options': None}])
        self.write_answer(id, choice=None, answers={'secret': [secret]})
        candidate = self.relay.files.take(id)
        frame = self.relay.claim(candidate)
        self.relay.sent(candidate, True)
        self.relay.observe('incoming', {'method': 'rawResponseItem/completed', 'params': {'threadId': self.native,
            'turnId': 'turn-one', 'item': {'type': 'function_call_output', 'call_id': 'item-one',
                                        'output': json.dumps(frame['result'])}}})
        self.resolve(2)
        self.assertEqual(self.result(id)['state'], 'sent_first')
        for path in self.directory.rglob('*.json'):
            self.assertNotIn(secret, path.read_text())
        self.assertNotIn(secret, repr(self.relay.records))
        self.assertEqual(candidate.answer, {})
        self.assertNotEqual(self.result(id)['state'], 'answered')
        late = self.request(wire='late')
        self.write_answer(late)
        candidate = self.relay.files.take(late)
        self.relay.claim(candidate)
        self.relay.sent(candidate, True)
        self.resolve('late')
        self.assertEqual(self.result(late)['state'], 'sent_first')
        self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': self.native,
            'turnId': 'turn-one', 'item': {'id': 'item-one', 'type': 'commandExecution', 'status': 'declined'}}})
        self.flush()
        self.assertEqual(self.result(late)['state'], 'unknown')
        self.assertEqual(self.result(late)['cause'], 'contradicted')
        self.assertIn('declined', self.result(late)['reason'])
        for extra in ({'approvalId': 'child-approval'}, {'networkApprovalContext': {'host': 'example.invalid'}}):
            with self.subTest(extra=extra):
                id = self.request(wire=str(extra), itemId=str(extra), **extra)
                self.write_answer(id)
                candidate = self.relay.files.take(id)
                self.relay.claim(candidate)
                self.relay.sent(candidate, True)
                self.resolve(str(extra))
                self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': self.native,
                    'turnId': 'turn-one', 'item': {'id': str(extra), 'type': 'commandExecution', 'status': 'declined'}}})
                self.flush()
                self.assertEqual(self.result(id)['state'], 'sent_first', 'A parent command does not identify a child approval effect')

        # Comparison uses the whole nonsecret reply even when its public preview truncates.
        for privacy in ('public', 'mixed', 'secret'):
            for change in ('same', 'text', 'array_order', 'missing', 'empty'):
                with self.subTest(large_question_effect=privacy, change=change):
                    case = PromptRelayTest()
                    case.setUp()
                    try:
                        fields = [{'id': f'q{i}', 'header': 'Field', 'question': 'Value?',
                                   'isSecret': privacy == 'secret' or privacy == 'mixed' and i == 1,
                                   'isOther': True, 'options': [{'label': 'One', 'description': ''}]}
                                  for i in range(64)]
                        id = case.request(QUESTION, wire='large-projection', questions=fields)
                        values = {q['id']: ('PRIVATE_SECRET_EFFECT' if q['isSecret'] else 'v',) for q in fields}
                        def encoded(given):
                            return core_prompts._encode_answer(case.session,
                                PromptAnswer(id=id, answers=given, allow_rule=True), {'kind': 'person'})[1]
                        values['q0'] = (values['q0'][0] + 'x' * ((64 << 10) - len(encoded(values).encode())),)
                        payload = json.loads(encoded(values))
                        self.assertEqual(len(encoded(values).encode()), 64 << 10)
                        path = case.write_answer(id, **{k: v for k, v in payload.items()
                                                        if k not in ('schema', 'id', 'launch_id', 'native_id')})
                        self.assertEqual(path.stat().st_size, 64 << 10)
                        candidate = case.relay.files.take(id)
                        response = case.relay.claim(candidate)
                        self.assertIsNotNone(response)
                        native = response['result']
                        self.assertGreater(len(json.dumps(native).encode()), 64 << 10)
                        self.assertLessEqual(len(json.dumps(native).encode()), 128 << 10)
                        offer = case.relay.records[id]['offer']
                        public = offer.person_answer(native)
                        self.assertLessEqual(len(json.dumps(public).encode()), 64 << 10)
                        if privacy != 'secret':
                            self.assertTrue(public.get('truncated'))
                            self.assertNotIn('q0', public.get('notes', {}))
                        case.relay.sent(candidate, True)
                        case.resolve('large-projection')
                        observed = deepcopy(native)
                        if change == 'text':
                            note = observed['answers']['q0']['answers'][1]
                            observed['answers']['q0']['answers'][1] = note[:-1] + 'y'
                        elif change == 'array_order':
                            observed['answers']['q0']['answers'].reverse()
                        elif change == 'missing':
                            del observed['answers']['q0']
                        elif change == 'empty':
                            observed['answers']['q0']['answers'] = []
                        case.relay.observe('incoming', {'method': 'rawResponseItem/completed', 'params': {
                            'threadId': case.native, 'turnId': 'turn-one', 'item': {'type': 'function_call_output',
                                'call_id': 'item-one', 'output': json.dumps(observed)}}})
                        case.flush()
                        contradiction = change != 'same' and privacy != 'secret'
                        self.assertEqual(case.result(id)['state'], 'unknown' if contradiction else 'sent_first')
                        if contradiction:
                            self.assertEqual(case.result(id)['cause'], 'contradicted')
                        case.relay.observe('incoming', {'method': 'turn/completed', 'params': {
                            'threadId': case.native, 'turn': {'id': 'turn-one', 'status': 'interrupted'}}})
                        case.flush()
                        self.assertEqual(case.result(id)['state'], 'unknown' if contradiction else 'sent_first')
                        self.assertEqual(candidate.answer, {})
                        self.assertNotIn('PRIVATE_SECRET_EFFECT', repr(case.relay.records))
                        for file in case.directory.rglob('*.json'):
                            self.assertNotIn('PRIVATE_SECRET_EFFECT', file.read_text())
                            self.assertNotIn(values['q0'][0], file.read_text())
                    finally:
                        case.doCleanups()

        for cause in ('interrupted', 'turn_ended', 'interrupt_request', 'switched', 'connection_lost',
                      'unrelated_turn', 'foreign_thread'):
            for effect in (False, True):
                with self.subTest(cancellation=cause, prior_effect=effect):
                    case = PromptRelayTest()
                    case.setUp()
                    try:
                        id = case.request()
                        case.write_answer(id)
                        candidate = case.relay.files.take(id)
                        self.assertIsNotNone(case.relay.claim(candidate))
                        case.relay.sent(candidate, True)
                        case.resolve()
                        self.assertEqual(case.result(id)['state'], 'sent_first')
                        completed = {'method': 'item/completed', 'params': {'threadId': case.native,
                            'turnId': 'turn-one', 'item': {'id': 'item-one', 'type': 'commandExecution',
                                                         'status': 'completed'}}}
                        if effect:
                            case.relay.observe('incoming', completed)
                        if cause == 'switched':
                            case.connection.root.native_id = str(uuid4())
                            case.relay.synchronize()
                        elif cause == 'connection_lost':
                            asyncio.run(case.relay.close())
                        elif cause == 'interrupt_request':
                            case.relay.observe('outgoing', {'id': 'interrupt', 'method': 'turn/interrupt',
                                'params': {'threadId': case.native, 'turnId': 'turn-one'}})
                        else:
                            case.relay.observe('incoming', {'method': 'turn/completed', 'params': {
                                'threadId': str(uuid4()) if cause == 'foreign_thread' else case.native,
                                'turn': {'id': 'different-turn' if cause == 'unrelated_turn' else 'turn-one',
                                         'status': 'completed' if cause == 'turn_ended' else 'interrupted'}}})
                        case.flush()
                        uncertain = not effect and cause not in ('unrelated_turn', 'foreign_thread')
                        self.assertEqual(case.result(id)['state'], 'unknown' if uncertain else 'sent_first')
                        if uncertain:
                            expected = 'interrupted' if cause == 'interrupt_request' else cause
                            self.assertEqual(case.result(id)['cause'], expected)
                            # Late resolution and a matching effect cannot erase an observed cancellation.
                            if cause not in ('switched', 'connection_lost'):
                                case.resolve()
                                case.relay.observe('incoming', completed)
                                case.flush()
                                self.assertEqual(case.result(id)['state'], 'unknown')
                            self.assertIsNone(case.relay.claim(candidate))
                    finally:
                        case.doCleanups()

    def test_review_intervals_and_mid_turn_rejoins_never_authorize_child_prompts(self):
        """Inline review forwards child requests through the primary, so a matching thread ID alone is insufficient."""
        def marker(type, turn, item):
            for method in ('item/started', 'item/completed'):
                self.relay.observe('incoming', {'method': method, 'params': {'threadId': self.native,
                    'turnId': turn, 'item': {'id': item, 'type': type}}})
        def offered(wire):
            id = self.request(wire=wire, turnId='delegated-turn', command='PRIVATE REVIEW CONTENT')
            return self.relay.records[id]['offer']
        self.relay.mode = 'unknown'
        self.assertFalse(offered('mid-review-rejoin').answerable)
        self.relay.observe('incoming', {'method': 'turn/started',
            'params': {'threadId': self.native, 'turn': {'id': 'unproven-child'}}})
        self.assertFalse(offered('unproven-start').answerable)
        self.relay.observe('outgoing', {'id': 90, 'method': 'turn/start', 'params': {'threadId': self.native}})
        self.relay.observe('incoming', {'id': 90, 'result': {'turn': {'id': 'normal'}}})
        self.assertTrue(offered('normal').answerable)
        self.relay.observe('outgoing', {'id': 91, 'method': 'review/start', 'params': {'threadId': self.native}})
        pending = offered('pending-review')
        self.assertFalse(pending.answerable)
        self.assertIn('review', pending.reason)
        self.assertNotIn('PRIVATE REVIEW CONTENT', repr(pending))
        self.relay.observe('incoming', {'id': '91', 'result': {'reviewThreadId': self.native, 'turn': {'id': 'wrong-type'}}})
        self.assertFalse(offered('still-pending').answerable)
        self.relay.observe('incoming', {'id': 91, 'result': {'reviewThreadId': self.native, 'turn': {'id': 'review-A'}}})
        marker('enteredReviewMode', 'review-A', 'entered-A')
        active_review = offered('child-turn')
        self.assertFalse(active_review.answerable)
        self.assertIn('review', active_review.reason)
        self.relay.observe('incoming', {'method': 'turn/started',
            'params': {'threadId': self.native, 'turn': {'id': 'review-child'}}})
        self.assertTrue(self.relay.records[self.relay.by_request[prompts.request_id('child-turn')]]['open'])
        self.assertFalse(offered('review-child-start').answerable)
        marker('exitedReviewMode', 'review-A', 'exited-A')
        self.assertFalse(self.relay.records[self.relay.by_request[prompts.request_id('review-child-start')]]['open'])
        self.assertTrue(offered('after-review').answerable)
        marker('enteredReviewMode', 'review-B', 'entered-B')
        marker('enteredReviewMode', 'review-A', 'entered-A')
        marker('exitedReviewMode', 'review-A', 'exited-A')
        self.relay.observe('incoming', {'method': 'turn/completed',
            'params': {'threadId': self.native, 'turn': {'id': 'review-A'}}})
        self.assertFalse(offered('late-A-cannot-clear-B').answerable)
        self.relay.observe('incoming', {'method': 'turn/completed',
            'params': {'threadId': self.native, 'turn': {'id': 'review-B'}}})
        self.assertTrue(offered('after-B').answerable)
        self.relay.observe('outgoing', {'id': 92, 'method': 'review/start', 'params': {'threadId': self.native}})
        self.relay.observe('incoming', {'id': 92, 'result': {'reviewThreadId': self.native, 'turn': {'id': 'new-review'}}})
        newer = self.request(wire='newer-review-before-delayed-old', turnId='delegated-newer')
        marker('enteredReviewMode', 'first-seen-delayed-old', 'delayed-enter')
        marker('exitedReviewMode', 'first-seen-delayed-old', 'delayed-exit')
        self.assertTrue(self.relay.records[newer]['open'])
        marker('enteredReviewMode', 'new-review', 'new-enter')
        self.assertFalse(offered('new-review-survives-first-seen-old').answerable)

    def test_pickup_requires_winning_the_atomic_file_claim_and_never_follows_special_files(self):
        """Core can discard an unclaimed answer, so a read copy is never authority after unlink loses the race."""
        publishing = self.request(wire='core-publishing')
        path = self.directory / 'prompts' / (publishing + '.answer.json')
        payload = {'schema': 1, 'id': publishing, 'launch_id': self.launch, 'native_id': self.native,
                   'message': None, **answer('allow')}
        observed = {}
        def published():
            observed['links'] = path.stat().st_nlink
            try:
                with patch.object(prompt_files.os, 'fdopen', side_effect=AssertionError('An unpublished hardlink must not be read')):
                    observed['candidate'] = self.relay.files.take(publishing)
            except (OSError, ValueError) as error:
                observed['error'] = type(error).__name__
        core_prompts._exclusive(path, payload, published=published)
        self.assertEqual(observed['links'], 2)
        self.assertNotIn('error', observed)
        self.assertIsNone(observed['candidate'], 'An in-progress publication is not permission to read a hardlink')
        self.assertFalse(self.relay.records[publishing]['attempted'])
        candidate = self.relay.files.take(publishing)
        self.assertEqual(candidate.answer, payload)
        self.assertEqual(self.relay.claim(candidate)['result'], {'decision': 'accept'})
        self.relay.sent(candidate, True)
        id = self.request()
        path = self.write_answer(id)
        real_stat = prompt_files.os.stat
        def core_wins(name, *args, **kwargs):
            value = real_stat(name, *args, **kwargs)
            if name == path.name and kwargs.get('dir_fd') is not None:
                os.unlink(name, dir_fd=kwargs['dir_fd'])
            return value
        with patch.object(prompt_files.os, 'stat', side_effect=core_wins):
            self.assertIsNone(self.relay.files.take(id))
        outside = self.directory / 'private-copy'
        outside.write_text(json.dumps({'private': 'valid structured sentinel'}))
        for kind in ('symlink', 'hardlink', 'fifo', 'oversized', 'duplicate'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    path.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, path)
                elif kind == 'fifo':
                    os.mkfifo(path)
                elif kind == 'oversized':
                    path.write_bytes(b'x' * (prompt_files.ANSWER_LIMIT + 1))
                else:
                    path.write_text('{"allow_rule":false,"allow_rule":true}')
                if kind == 'hardlink':
                    with patch.object(prompt_files.os, 'fdopen', side_effect=AssertionError('An arbitrary hardlink must not be read')):
                        self.assertIsNone(self.relay.files.take(id), 'An unverifiable hardlink stays unclaimed and unread')
                else:
                    with self.assertRaises((OSError, ValueError)):
                        self.relay.files.take(id)
                path.unlink()
        self.assertEqual(json.loads(outside.read_text()), {'private': 'valid structured sentinel'})

    def test_failed_transport_and_optional_pickup_leave_terminal_replies_available(self):
        """A failed answer write is uncertain and never retried, while optional pickup loss cannot disable the TUI."""
        id = self.request(wire=1)
        self.write_answer(id)
        candidate = self.relay.files.take(id)
        self.relay.claim(candidate)
        self.relay.sent(candidate, False)
        self.resolve(1)
        self.assertEqual(self.result(id)['state'], 'unknown')
        self.assertIsNone(self.relay.claim(candidate))
        id = self.request(wire=2)
        self.relay.disable()
        self.flush()
        self.assertEqual(self.result(id)['state'], 'refused')
        self.assertTrue(self.relay.terminal({'id': 2, 'result': {'decision': 'accept'}}))
        self.flush()
        self.assertEqual(self.result(id)['state'], 'person')

    def test_an_invalid_first_file_never_becomes_permission_to_retry_or_change_prompts(self):
        """One exclusive attempt remains one attempt even when a malformed file is followed by a plausible replacement."""
        id = self.request(wire=1)
        self.write_answer(id, schema=True)
        candidate = self.relay.files.take(id)
        self.assertIsNone(self.relay.claim(candidate))
        self.assertEqual(candidate.answer, {})
        self.flush()
        self.assertEqual(self.result(id)['state'], 'refused')
        self.assertFalse(prompts.catalog(self.session).value['prompts'][0]['answerable'])
        self.write_answer(id)
        candidate = self.relay.files.take(id)
        self.assertIsNone(self.relay.claim(candidate))
        self.assertEqual(candidate.answer, {})
        self.assertTrue(self.relay.terminal({'id': 1, 'result': {'decision': 'accept'}}))
        self.flush()
        self.assertEqual(self.result(id)['state'], 'person')

    def test_cancelled_off_loop_publication_cannot_overwrite_the_final_closed_snapshot(self):
        """Cancelling an asyncio wait cannot cancel its file worker, so old jobs need a publication fence."""
        started, release = threading.Event(), threading.Event()
        errors = []
        files = self.relay.files
        original = files.write
        def delayed(name, value):
            if value.get('old'):
                started.set()
                if not release.wait(3):
                    raise ValueError('test publication barrier expired')
            original(name, value)
        def apply(jobs, **kwargs):
            try:
                files.apply(jobs, **kwargs)
            except Exception as error:
                errors.append(type(error).__name__)
        with patch.object(files, 'write', side_effect=delayed):
            old = threading.Thread(target=apply, args=([('write', 'index.json', {'active': True, 'old': True}),
                                                       ('write', 'stale.open.json', {'stale': True})],))
            old.start()
            try:
                self.assertTrue(started.wait(2))
                files.stop_publications()
                final = threading.Thread(target=apply, args=([('write', 'index.json', {'active': False})],),
                                         kwargs={'final': True})
                final.start()
            finally:
                release.set()
                old.join(3)
                if 'final' in locals():
                    final.join(3)
            self.assertFalse(old.is_alive())
            self.assertFalse(final.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(files.read('index.json'), {'active': False})
        with self.assertRaises(FileNotFoundError):
            files.read('stale.open.json')


class PromptTransportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        isolate(self)
        self.temporary = tempfile.TemporaryDirectory(prefix='mv2-prompts-', dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / 'agent'
        self.directory.mkdir(mode=0o700)
        self.launch, self.native = str(uuid4()), str(uuid4())
        self.upstreams, self.frames, self.pending = [], [], {}
        self.consumed_elsewhere, self.ignored_replies = {}, []

        async def native(connection):
            self.upstreams.append(connection)
            async for payload in connection:
                frame = json.loads(payload)
                self.frames.append(frame)
                if frame.get('method') == 'initialize':
                    result = {'userAgent': 'codex/' + NATIVE_VERSION}
                elif frame.get('method') == 'thread/resume':
                    result = {'thread': {'id': self.native, 'cwd': '/workspace', 'status': {'type': 'idle'}},
                        'sandbox': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                        'activePermissionProfile': None, 'runtimeWorkspaceRoots': []}
                elif 'method' not in frame:
                    request = self.pending.get(prompts.request_id(frame.get('id')))
                    if request is not None:
                        if prompts.request_id(frame['id']) in self.consumed_elsewhere:
                            self.ignored_replies.append(frame['id'])  # No callback remains to consume this response.
                        await connection.send(json.dumps({'method': 'serverRequest/resolved',
                            'params': {'threadId': request['threadId'], 'requestId': frame['id']}}))
                    continue
                else:
                    result = {'echo': frame['id']}
                await connection.send(json.dumps({'id': frame['id'], 'result': result}))

        self.server = await unix_serve(native, str(self.root / 'native'))
        async def close_server():
            self.server.close()
            await self.server.wait_closed()
        self.addAsyncCleanup(close_server)
        protocol = LaunchProtocol({'native_id': self.native, 'workspace': '/workspace', 'bootstrap': '', 'roots': [],
            'resume_permissions': {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}, 'navigation_cli': codec.VERSION})
        self.errors, self.bindings = [], []
        self.proxy = rpc.LaunchProxy({'socket_path': str(self.root / 'relay'), 'upstream_path': str(self.root / 'native')},
            protocol, self.bindings.append, self.errors.append,
            relay_context={'agent_directory': str(self.directory), 'launch_id': self.launch})
        await asyncio.to_thread(self.proxy.start)
        self.addAsyncCleanup(asyncio.to_thread, self.proxy.close)
        self.client = await unix_connect(str(self.root / 'relay'))
        self.addAsyncCleanup(self.client.close)
        self.session = SessionHandle('agent', 'codex', '/workspace', launch_id=self.launch, native_id=self.native,
            session_file=str(self.directory / 'session.jsonl'), state='running', status='waiting',
            endpoint=NativeEndpoint('unix-websocket', str(self.root / 'relay')),
            native_versions={'cli': NATIVE_VERSION, 'daemon': NATIVE_VERSION}, native_relay={'state': 'ready'},
            native_mode={'mode': 'bridge', 'policy': 'required'})
        (self.directory / 'properties.json').write_text(json.dumps({'id': 'agent', 'name': 'worker',
            'harness': 'codex', 'workspace': '/workspace', 'native_session_id': self.native}))
        self.adapter = CodexAdapter('codex', {'spawn': ['unexecuted-codex']})
        self.executor = runtime_native.NativeExecutor('/workspace')
        self.session_checks = self.enterContext(patch.object(self.executor, 'check_session'))
        self.enterContext(patch.object(core_prompts.universe, 'resolve', return_value=self.root))
        self.enterContext(patch.object(core_prompts.agents, 'directory', return_value=self.directory))
        self.enterContext(patch.object(core_prompts.agents, '_event'))
        self.enterContext(patch.object(core_prompts, '_connect', return_value=(self.adapter, self.session, self.executor)))
        self.enterContext(patch.object(core_prompts, '_reader', return_value=(self.adapter, self.executor, False)))
        await self.exchange({'id': 1, 'method': 'initialize', 'params': {}})
        await self.exchange({'id': 2, 'method': 'thread/resume', 'params': {'threadId': self.native}})
        self.assertEqual(self.bindings, [self.native])

    async def exchange(self, frame):
        await self.client.send(json.dumps(frame))
        async with asyncio.timeout(5):
            while True:
                value = json.loads(await self.client.recv())
                if 'method' not in value and value.get('id') == frame.get('id'):
                    return value

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    async def request_prompt(self, wire, method=COMMAND, **changes):
        native_params = params(threadId=self.native, availableDecisions=['accept', 'acceptForSession', 'decline', 'cancel'])
        native_params['itemId'] = 'item-' + str(wire)
        native_params.update(changes)
        self.pending[prompts.request_id(wire)] = native_params
        await self.upstreams[0].send(json.dumps({'id': wire, 'method': method, 'params': native_params}))
        async with asyncio.timeout(5):
            while True:
                value = json.loads(await self.client.recv())
                if value.get('method') == method and value.get('id') == wire:
                    break
        await self.until(lambda: any(p['detail'].get('itemId') == native_params['itemId']
                                    for p in prompts.catalog(self.session).value['prompts']))
        found = prompts.catalog(self.session).value['prompts']
        return next(p['id'] for p in found if p['detail'].get('itemId') == native_params['itemId'])

    def write_answer(self, id, **changes):
        folder = self.directory / 'prompts'
        folder.mkdir(exist_ok=True, mode=0o700)
        value = {'schema': 1, 'id': id, 'launch_id': self.launch, 'native_id': self.native,
                 'message': None, **answer('allow'), **changes}
        with os.fdopen(os.open(folder / (id + '.answer.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
            json.dump(value, out)
        return folder / (id + '.answer.json')

    def result(self, id):
        return prompts.outcome(self.session, SimpleNamespace(id=id)).value

    async def test_answer_file_budget_and_typed_response_headroom_do_not_burn_a_correctable_answer(self):
        """Metadata and native framing can strand an admitted answer after its only prompt attempt was reserved."""
        for shape in ('one-question', '64-questions', 'mapped-form'):
            with self.subTest(shape=shape):
                wire = 'envelope-' + shape
                prefix = 'PRIVATE_ENVELOPE_VALUE_é"\\'
                if shape == 'mapped-form':
                    literal = 'OFFERED_' + 'N' * (60 << 10)
                    schema = {'type': 'object', 'required': ['mapped', 'free'], 'properties': {
                        'mapped': {'type': 'string', 'oneOf': [{'title': 'Mapped', 'const': literal}]},
                        'free': {'type': 'string'}}}
                    id = await self.request_prompt(wire, method=MCP, mode='form', serverName='fixture',
                                                   requestedSchema=schema)
                    values, key = {'mapped': ('Mapped',), 'free': (prefix,)}, 'free'
                else:
                    fields = [{'id': f'q{i}', 'header': 'Field', 'question': 'Value?',
                               'isSecret': True, 'options': None}
                              for i in range(64 if shape == '64-questions' else 1)]
                    id = await self.request_prompt(wire, method=QUESTION, questions=fields)
                    values, key = {q['id']: ('v',) for q in fields}, 'q0'
                    values[key] = (prefix,)

                def encoded(given):
                    request = PromptAnswer(id=id, answers=given, allow_rule=True)
                    value = {'schema': 1, **asdict(request), 'launch_id': self.launch,
                             'native_id': self.native, 'caller': {'kind': 'person'}}
                    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')

                values[key] = (prefix + 'x' * ((64 << 10) - len(encoded(values))),)
                self.assertEqual(len(encoded(values)), 64 << 10)
                oversized = {**values, key: (values[key][0] + 'x',)}
                self.assertEqual(len(encoded(oversized)), (64 << 10) + 1)
                event_count = core_prompts.agents._event.call_count
                refused = core_prompts.answer(self.root, 'agent', id, answers=oversized, wait=False)
                self.assertEqual(refused.state, 'refused')
                self.assertIn('64 KiB', refused.reason)
                self.assertFalse('PRIVATE_ENVELOPE_VALUE' in str(refused))
                self.assertFalse((self.directory / 'prompts' / (id + '.json')).exists())
                self.assertFalse((self.directory / 'prompts' / (id + '.answer.json')).exists())
                self.assertEqual(core_prompts.agents._event.call_count, event_count)
                self.assertFalse([f for f in self.frames if 'method' not in f and f.get('id') == wire])

                accepted = core_prompts.answer(self.root, 'agent', id, answers=values, wait=False)
                self.assertEqual(accepted.state, 'submitted', accepted)
                try:
                    await self.until(lambda: self.result(id).get('state') in ('sent_first', 'refused'))
                except TimeoutError:
                    self.fail('The admitted bounded envelope did not reach a native response outcome')
                self.assertEqual(self.result(id)['state'], 'sent_first', self.result(id).get('reason'))
                self.assertEqual(core_prompts.result(self.root, 'agent', id).state, 'sent_first')
                self.assertEqual(core_prompts.answer(self.root, 'agent', id, answers=values, wait=False).cause,
                                 'already_answered')
                responses = [f for f in self.frames if 'method' not in f and f.get('id') == wire]
                self.assertEqual(len(responses), 1)
                result = responses[0]['result']
                size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                self.assertLessEqual(size, 128 << 10)
                if shape != 'one-question':
                    self.assertGreater(size, 64 << 10)
                if shape == 'mapped-form':
                    self.assertTrue(result == {'action': 'accept', 'content': {
                        'mapped': literal, 'free': values[key][0]}, '_meta': None})
                else:
                    self.assertTrue(result == {'answers': {q: {'answers': ['user_note: ' + value[0]]}
                                                           for q, value in values.items()}})
                if shape == '64-questions':
                    oversized_result = deepcopy(result)
                    oversized_result['answers'][key]['answers'][0] += 'x' * (128 << 10)
                    with patch.object(codec.Offer, 'question_effect', return_value=None) as observe:
                        await self.upstreams[0].send(json.dumps({'method': 'rawResponseItem/completed', 'params': {
                            'threadId': self.native, 'turnId': 'turn-one', 'item': {'type': 'function_call_output',
                                'call_id': 'item-' + wire, 'output': json.dumps(oversized_result, ensure_ascii=False)}}}))
                        async with asyncio.timeout(5):
                            while True:
                                seen = json.loads(await self.client.recv())
                                if (seen.get('method') == 'rawResponseItem/completed'
                                        and seen['params']['item'].get('call_id') == 'item-' + wire):
                                    break
                        self.assertTrue(all(call.args[-1] is None for call in observe.call_args_list),
                                        'Oversized native result text must not be decoded as prompt evidence')
                    await self.upstreams[0].send(json.dumps({'method': 'rawResponseItem/completed', 'params': {
                        'threadId': self.native, 'turnId': 'turn-one', 'item': {'type': 'function_call_output',
                            'call_id': 'item-' + wire, 'output': json.dumps(result, ensure_ascii=False)}}}))
                    try:
                        await self.until(lambda: 'question result' in self.result(id).get('reason', ''))
                    except TimeoutError:
                        self.fail('A bounded large typed result must retain the native effect observation')
                for path in self.directory.rglob('*.json'):
                    self.assertFalse('PRIVATE_ENVELOPE_VALUE' in path.read_text(),
                                     'Submitted answer text must not enter retained prompt records')

    async def test_real_relay_and_private_answer_files_preserve_one_connection_and_the_two_id_namespaces(self):
        """The unit arbiter is insufficient unless real relay forwarding, atomic pickup and result publication agree."""
        id = await self.request_prompt(3)
        submitted = core_prompts.answer(self.root, 'agent', id, choice='allow_session', wait=False)
        self.assertEqual(submitted.state, 'submitted', submitted)
        await self.until(lambda: self.result(id).get('state') == 'sent_first')
        self.assertEqual(core_prompts.result(self.root, 'agent', id).state, 'sent_first')
        self.assertEqual(core_prompts.answer(self.root, 'agent', id, choice='allow_session', wait=False).cause,
                         'already_answered')
        self.assertGreaterEqual(self.session_checks.call_count, 4)
        await self.client.send(json.dumps({'id': 3, 'result': {'decision': 'decline'}}))
        # ID 3 is also valid in the client-request namespace; it must still work.
        self.assertEqual((await self.exchange({'id': 3, 'method': 'echo', 'params': {}}))['result'], {'echo': 3})
        replies = [v for v in self.frames if 'method' not in v and v.get('id') == 3]
        self.assertEqual(replies, [{'jsonrpc': '2.0', 'id': 3, 'result': {'decision': 'acceptForSession'}}])
        self.assertIn('does not confirm', self.result(id)['reason'])
        next_id = await self.request_prompt('person')
        await self.client.send(json.dumps({'id': 'person', 'result': {'decision': 'decline'}}))
        await self.until(lambda: self.result(next_id).get('state') == 'person')
        late = self.write_answer(next_id)
        await self.until(lambda: not late.exists())
        await self.exchange({'id': 4, 'method': 'echo', 'params': {}})
        replies = [v for v in self.frames if 'method' not in v and v.get('id') == 'person']
        self.assertEqual(replies, [{'id': 'person', 'result': {'decision': 'decline'}}])
        for first in ('mv2', 'person'):
            wire = 'outside-won-' + first
            outside = await self.request_prompt(wire)
            # Another subscriber consumes the native callback first, but its
            # resolved notification is withheld until this socket writes.
            self.consumed_elsewhere[prompts.request_id(wire)] = {'decision': 'decline'}
            if first == 'mv2':
                self.assertEqual(core_prompts.answer(self.root, 'agent', outside, choice='allow', wait=False).state,
                                 'submitted')
                expected = 'sent_first'
            else:
                await self.client.send(json.dumps({'id': wire, 'result': {'decision': 'accept'}}))
                expected = 'person'
            await self.until(lambda: self.result(outside).get('state') == expected and wire in self.ignored_replies)
            self.assertEqual(self.consumed_elsewhere[prompts.request_id(wire)], {'decision': 'decline'})
            self.assertNotEqual(self.result(outside)['state'], 'answered')
            if first == 'mv2':
                self.assertIn('does not confirm', self.result(outside)['reason'])
            else:
                self.assertEqual(self.result(outside)['person_answer'], {'choice': 'allow'})
                self.assertIn('did not send', self.result(outside)['reason'])
            self.assertEqual(len([v for v in self.frames if 'method' not in v and v.get('id') == wire]), 1)
        question = await self.request_prompt('question', method=QUESTION, questions=[
            {'id': 'pick', 'header': 'Pick', 'question': 'Which?', 'isOther': False,
             'options': [{'label': 'One', 'description': ''}]},
            {'id': 'secret', 'header': 'Secret', 'question': 'Password?', 'isSecret': True, 'options': None}])
        submitted = core_prompts.answer(self.root, 'agent', question, answers={'pick': 'One'},
            notes={'pick': 'PRIVATE_SUBMITTED_NOTE', 'secret': 'SECRET_PRIVATE_NOTE'}, wait=False)
        self.assertEqual(submitted.state, 'submitted')
        await self.until(lambda: self.result(question).get('state') == 'sent_first')
        self.assertEqual(core_prompts.result(self.root, 'agent', question).state, 'sent_first')
        replies = [v for v in self.frames if 'method' not in v and v.get('id') == 'question']
        self.assertEqual(replies, [{'jsonrpc': '2.0', 'id': 'question', 'result': {'answers': {
            'pick': {'answers': ['One', 'user_note: PRIVATE_SUBMITTED_NOTE']},
            'secret': {'answers': ['user_note: SECRET_PRIVATE_NOTE']}}}}])
        for path in self.directory.rglob('*.json'):
            self.assertNotIn('PRIVATE_SUBMITTED_NOTE', path.read_text())
            self.assertNotIn('SECRET_PRIVATE_NOTE', path.read_text())
        await self.upstreams[0].send(json.dumps({'method': 'turn/completed', 'params': {
            'threadId': self.native, 'turn': {'id': 'turn-one', 'status': 'interrupted'}}}))
        try:
            await self.until(lambda: self.result(id).get('state') == 'unknown')
        except TimeoutError:
            self.fail('The observed interruption did not qualify the earlier resolved submission')
        settled = core_prompts.result(self.root, 'agent', id)
        self.assertEqual((settled.state, settled.cause), ('unknown', 'interrupted'))
        self.assertEqual(core_prompts.answer(self.root, 'agent', id, choice='allow', wait=False).cause, 'already_answered')
        self.assertEqual(len([v for v in self.frames if 'method' not in v and v.get('id') == 3]), 1)
        self.assertEqual(len(self.upstreams), 1)
        self.assertFalse(self.errors)
