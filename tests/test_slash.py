"""Command refusals, one submission across timeouts, and generation-bound outcomes."""
from dataclasses import asdict
import json
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.types import NativeResult, SessionHandle, SlashCommand
from multiverse.plugins import access
from multiverse.runtime import native, slash
from multiverse.store import read, write, now
from multiverse.tools import slash as tool
from multiverse.universe import agents, focus
from test_multiverse import BaseTest


class SlashTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.launch, self.session = (str(uuid4()) for _ in range(3))
        self.directory = self.root / 'state/agents' / self.agent
        write(self.directory / 'properties.json', {'id': self.agent, 'name': 'worker', 'harness': 'claude',
              'workspace': str(self.work), 'native_session_id': self.session, 'model': 'old', 'effort': 'low'})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running'})
        self.sent, self.receipts = [], {}
        self.immediate = NativeResult('unknown')
        self.catalog = [SlashCommand(name=name, needs_args=name in ('model', 'effort', 'plugin'),
                                    changes_settings=name in ('model', 'effort', 'plugin'))
                        for name in ('compact', 'clear', 'model', 'effort', 'plugin', 'restart', 'multiverse',
                                     'login', 'logout', 'exit', 'quit', 'resume')]
        self.catalog += [SlashCommand(name='new-builtin', refused='not classified for Claude Code VERSION'),
                         SlashCommand(name='my-skill', source='user')]
        owner = self
        class Adapter:
            name = family = 'claude'
            def slash_commands(self, session, executor):
                return NativeResult('ok', value=owner.catalog)
            def run_slash(self, session, request, executor):
                owner.sent.append(request)
                return owner.immediate
            def slash_result(self, session, request, executor):
                return owner.receipts.get(request.id, NativeResult('unknown', value={'absent': True}))
        self.adapter = Adapter()
        self.snapshot = native.session_snapshot
        self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))
        self.enterContext(patch.object(native, 'session_snapshot', side_effect=lambda root, directory, props, facts, **kw: (props, facts)))
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', side_effect=lambda props, facts: {
            'live': 'alive' if facts.get('state') == 'running' else 'stopped'}))
        self.ended = self.enterContext(patch.object(slash.delivery_evidence, 'launch_ended', return_value=False))
        self.events = self.enterContext(patch.object(agents, '_event'))

    def test_refusals_never_submit_or_create_a_run_and_focus_distinguishes_people(self):
        """A command refusal must precede submission, or an idle picker or lifecycle command can strand an agent."""
        cases = [('exit', ''), ('quit', ''), ('resume', 'other'), ('login', ''), ('logout', ''),
                 ('restart', ''), ('multiverse', 'send'), ('plugin', 'disable multiverse'),
                 ('plugin', 'uninstall multiverse@local'), ('plugin', 'disable --all'),
                 ('model', ''), ('plugin', ''), ('new-builtin', ''), ('q', ''), ('missing', '')]
        for command, args in cases:
            with self.subTest(command=command, args=args):
                outcome = slash.run(self.root, self.agent, command, args, wait=False)
                self.assertEqual(outcome.state, 'refused')
                self.assertTrue(outcome.reason)
                self.assertFalse(outcome.id)
                self.assertEqual(self.sent, [])
                self.assertFalse((self.directory / 'slash').exists())
        self.assertEqual(slash.run(self.root, self.agent, '/exit', wait=False).state, 'refused')
        for caller in (object(), {'kind': 'plugin', 'name': 'planner'}):
            with self.subTest(settings_caller=type(caller).__name__), \
                    patch.object(slash.agent_identity, 'sender_actor', return_value=caller):
                for command, args in (('model', 'new'), ('effort', 'high'), ('plugin', 'enable other')):
                    result = slash.run(self.root, self.agent, command, args, wait=False)
                    self.assertEqual(result.state, 'refused')
                    self.assertIn('mv2 resume worker --model X --effort Y', result.reason)
                    self.assertFalse(result.id)
        self.assertEqual(self.sent, [])
        self.assertFalse((self.directory / 'slash').exists())
        write(self.directory / 'focus.json', {'since': now(), 'by': 'agent'})
        with patch.object(slash.agent_identity, 'sender_actor', return_value=object()):
            self.assertEqual(slash.run(self.root, self.agent, 'compact', wait=False).state, 'refused')
        self.assertEqual(self.sent, [])
        # An operator may run the same command while focus remains active.
        self.assertEqual(slash.run(self.root, self.agent, '/compact', wait=False).state, 'queued')
        self.assertEqual(len(self.sent), 1)

        self.assertEqual(self.sent[0].command, 'compact')
        self.assertIsNotNone(focus.current(self.directory))
        self.assertEqual(slash.run(self.root, self.agent, 'compact', wait=False,
                                  caller={'kind': 'plugin', 'name': 'planner'}).state, 'refused')
        with patch.object(harnesses, 'load', return_value=object()):
            self.assertEqual(slash.commands(self.root, self.agent)['state'], 'unavailable')
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'stopped'})
        self.assertEqual(slash.commands(self.root, self.agent)['state'], 'unavailable')
        self.assertEqual(slash.run(self.root, self.agent, 'compact', wait=False).state, 'unavailable')
        self.assertEqual(len(self.sent), 1)

    def test_caller_attribution_survives_a_different_result_reader(self):
        """A plugin's queued command must not later be credited to the person or agent reading its result."""
        actor = slash.agent_identity.AgentActor(self.root, self.directory, self.agent, 'worker')
        for sender, explicit, expected in (
                (None, None, {'kind': 'person'}),
                (actor, None, {'kind': 'agent', **actor.sender()}),
                (None, {'kind': 'plugin', 'name': 'planner'}, {'kind': 'plugin', 'name': 'planner'})):
            with self.subTest(caller=expected['kind']), \
                    patch.object(slash.agent_identity, 'sender_actor', return_value=sender):
                queued = slash.run(self.root, self.agent, 'compact', 'private arguments', wait=False,
                                   caller=explicit)
            record = read(self.directory / 'slash' / (queued.id + '.json'))
            self.assertEqual(record['caller'], expected)
            self.assertEqual(self.events.call_args.args[3],
                             {'command': 'compact', 'state': 'queued', 'caller': expected})
            self.receipts[queued.id] = NativeResult('ok', value={'state': 'ran', 'output': 'private output'})
            with patch.object(slash.agent_identity, 'sender_actor', side_effect=AssertionError('No new caller')):
                self.assertEqual(slash.result(self.root, self.agent, queued.id).state, 'ran')
            self.assertEqual(self.events.call_args.args[3],
                             {'command': 'compact', 'state': 'ran', 'caller': expected})

    def test_pending_receipts_settle_in_snapshots_without_scanning_command_history(self):
        """No-wait settings changes must reach listings without another submission or an unbounded history scan."""
        self.catalog.append(SlashCommand(name='display name (MCP)', source='mcp'))
        names = [entry['name'] for entry in slash.commands(self.root, self.agent)['commands']]
        self.assertIn('model', names)
        self.assertNotIn('display name (MCP)', names)
        queued = slash.run(self.root, self.agent, 'model', 'new', wait=False)
        self.receipts[queued.id] = NativeResult('ok', value={'state': 'ran', 'model': 'new', 'session_id': self.session})
        with patch.object(native, 'session_snapshot', self.snapshot), \
                patch.object(type(self.directory), 'glob', side_effect=AssertionError('No command history scan')):
            properties, _ = self.snapshot(self.root, self.directory, read(self.directory / 'properties.json'),
                                           agents.runtime(self.directory), adapter=self.adapter)
        self.assertEqual(properties['model'], 'new')
        self.assertFalse((self.directory / 'slash/pending.json').exists())
        count = self.events.call_count
        self.assertEqual(slash.result(self.root, self.agent, queued.id).state, 'ran')
        self.assertEqual(self.events.call_count, count)
        self.assertEqual(len(self.sent), 1)

    def test_timeout_late_receipt_and_end_proof_never_resubmit_or_lose_unreadable_outcomes(self):
        """Late command receipts must survive timeouts and launch changes without resending or inventing loss."""
        queued = slash.run(self.root, self.agent, 'compact', 'private arguments', timeout=.01)
        self.assertEqual(queued.state, 'queued')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(slash.result(self.root, self.agent, queued.id).state, 'queued')
        self.receipts[queued.id] = NativeResult('ok', value={'state': 'ran', 'output': 'private output',
                                                          'context': 'never expose this', 'prompted': False})
        done = slash.result(self.root, self.agent, queued.id)
        self.assertEqual(done.output, 'private output')
        self.assertEqual(slash.result(self.root, self.agent, queued.id), done)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn('private', str([call.args[3] for call in self.events.call_args_list]))
        self.assertNotIn('never expose', json.dumps(asdict(done)))
        for readable in (False, True):
            with self.subTest(readable=readable):
                queued = slash.run(self.root, self.agent, 'compact', wait=False)
                self.ended.return_value = True
                self.receipts[queued.id] = (NativeResult('unknown', value={'absent': True}) if readable else
                                           NativeResult('unknown', 'receipt unreadable'))
                before = self.events.call_count
                outcome = slash.result(self.root, self.agent, queued.id)
                self.assertEqual(outcome.state, 'lost' if readable else 'queued')
                again = slash.result(self.root, self.agent, queued.id)
                self.assertEqual(again, outcome)
                self.assertEqual(self.events.call_count - before, 1 if readable else 0)
                self.ended.return_value = False
        queued = slash.run(self.root, self.agent, 'compact', wait=False)
        def publication(session, request, executor):
            self.ended.return_value = True
            self.receipts[request.id] = NativeResult('ok', value={'state': 'ran', 'output': 'late'})
            return NativeResult('unknown', value={'absent': True})
        with patch.object(self.adapter, 'slash_result', side_effect=publication):
            self.assertEqual(slash.result(self.root, self.agent, queued.id).state, 'queued')
        self.assertEqual(slash.result(self.root, self.agent, queued.id).output, 'late')

    def test_readbacks_follow_lineage_and_cannot_overwrite_newer_settings_or_launches(self):
        """A command result must publish its settings before returning, without rewriting a newer launch or outcome."""
        first = slash.run(self.root, self.agent, 'model', 'first', wait=False)
        second = slash.run(self.root, self.agent, 'effort', 'high', wait=False)
        for command, model, effort in ((first, 'first', 'medium'), (second, 'second', 'high')):
            self.receipts[command.id] = NativeResult('ok', value={'state': 'ran', 'model': model, 'effort': effort,
                                                                'session_id': self.session})
        self.assertEqual(slash.result(self.root, self.agent, second.id).changed, ('model', 'effort'))
        self.assertTrue(slash.result(self.root, self.agent, second.id).saved_default)
        self.assertEqual(slash.result(self.root, self.agent, first.id).changed, ())
        properties = read(self.directory / 'properties.json')
        self.assertEqual((properties['model'], properties['effort']), ('second', 'high'))
        model_only = slash.run(self.root, self.agent, 'model', 'third', wait=False)
        self.receipts[model_only.id] = NativeResult('ok', value={'state': 'ran', 'model': 'third',
                                                              'session_id': self.session})
        self.assertEqual(slash.result(self.root, self.agent, model_only.id).changed, ('model',))
        properties = read(self.directory / 'properties.json')
        self.assertEqual((properties['model'], properties['effort']), ('third', 'high'))
        self.assertIn("Also saved as the person's default for this harness.",
                      tool.cli_run(self.root, agent=self.agent, result=model_only.id))
        cleared = slash.run(self.root, self.agent, 'clear', wait=False)
        new_session = str(uuid4())
        self.receipts[cleared.id] = NativeResult('ok', value={'state': 'ran', 'session_id': new_session})
        self.assertEqual(slash.result(self.root, self.agent, cleared.id).state, 'queued')
        write(self.directory / 'properties.json', {**properties, 'native_session_id': new_session})
        self.assertEqual(slash.result(self.root, self.agent, cleared.id).changed, ('session',))
        late = slash.run(self.root, self.agent, 'model', 'late', wait=False)
        self.receipts[late.id] = NativeResult('ok', value={'state': 'ran', 'model': 'late', 'effort': None,
                                                        'session_id': new_session})
        write(self.directory / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'running'})
        self.assertEqual(slash.result(self.root, self.agent, late.id).changed, ())
        self.assertEqual(read(self.directory / 'properties.json')['model'], 'third')

    def test_cli_and_observe_access_cannot_turn_a_result_read_into_a_submission(self):
        """Listing and delayed result reads must remain observe-only while running a command needs act."""
        from multiverse.cli import parser
        # Exercise the actual CLI parser and wrapper, including an argument beginning with a slash.
        parsed = vars(parser().parse_args(['slash', 'worker', 'my-skill', 'literal /arg', '--no-wait']))
        parsed.pop('action')
        parsed['root'] = self.root
        self.immediate = NativeResult('ok', value={'state': 'ran', 'output': 'ignored', 'prompted': True})
        found = tool.run(**parsed)
        self.assertTrue(found['prompted'])
        self.assertIsNone(found['output'])
        with access.within('observe', 'test plugin'):
            self.assertEqual(slash.commands(self.root, self.agent)['state'], 'ok')
            self.assertEqual(slash.result(self.root, self.agent, found['id']).state, 'ran')
            with self.assertRaises(access.Denied):
                slash.run(self.root, self.agent, 'compact')
        self.assertEqual(len(self.sent), 1)
        self.immediate = NativeResult('ok', value={'state': 'ran', 'output': 'é' * slash.OUTPUT_BYTES})
        bounded = slash.run(self.root, self.agent, 'compact', wait=False)
        self.assertEqual(len(bounded.output.encode()), slash.OUTPUT_BYTES)
