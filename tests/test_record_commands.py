"""The commands that printed raw records (qa-lifecycle LC-4, LC-6; qa-claude CLAUDE-7) still carry their facts.

kill and stop say what they could not confirm, `mv2 ask` and `mv2 get-usage` fail (exit 1) when they have no answer
or no measurement, and --json and the
Python `run` functions the dashboard and scripts call keep returning the records.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import cli
from multiverse.runtime import lifecycle, native
from multiverse.store import write
from multiverse.tools import ask, capabilities, get_usage, init, kill, stop, trash
from test_multiverse import BaseTest

AGENT_ID = str(uuid4())


class RecordCommandsTest(BaseTest):
    def setUp(self):
        super().setUp()
        directory = self.root / 'state/agents' / AGENT_ID
        write(directory / 'properties.json', {'id': AGENT_ID, 'name': 'worker', 'harness': 'codex', 'phase': 'done',
                                              'summary': '', 'workspace': str(self.work)})

    def command(self, *arguments):
        """The command line, as `mv2` runs it: what it prints, what it prints on stderr, and its exit code."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                with patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *arguments]):
                    cli.main()
            except SystemExit as exit:
                code = exit.code or 0
        return out.getvalue().strip(), err.getvalue().strip(), code

    def killed(self, state='ok', reason='Core stops the owned terminal process', **details):
        return {'id': AGENT_ID, 'state': 'dormant', 'native_cancellation': {'state': state, 'reason': reason, **details}}

    def harness(self, name, definition):
        directory = self.root / 'harnesses' / name
        directory.mkdir(parents=True, exist_ok=True)
        write(directory / 'harness.json', definition)

    def test_record_commands_report_their_facts_and_fail_honestly(self):
        """kill and stop say when cancellation is unconfirmed or startup messages were dropped, ask exits 1 without an
        answer and get-usage without a measurement, capabilities --probe marks a harness with a missing executable unavailable, and --json and the Python
        functions keep returning records; kept for the qa findings these fixed, since agents act on this output and the
        exit code (LC-4, LC-6, CLAUDE-7, e2dafba)."""
        for name in ('kill', 'stop'):
            with self.subTest(command=name):
                with patch.object(lifecycle, 'kill', return_value=self.killed(cancelled_startup_messages=2)):
                    out, _, code = self.command(name, 'worker')
                self.assertEqual(code, 0)
                self.assertRegex(out, r'\b2 messages\b.*not delivered')
                unconfirmed = self.killed('unknown', 'Native cancellation unconfirmed: socket closed. Retry stop '
                                          'when the native service is reachable.', cancelled_startup_messages=1)
                with patch.object(lifecycle, 'kill', return_value=unconfirmed):
                    out, _, code = self.command(name, 'worker')
                    self.assertIn("couldn't confirm", out)
                    self.assertIn('socket closed', out)
                    self.assertEqual(json.loads(self.command(name, 'worker', '--json')[0]), unconfirmed)
                attachment = self.killed(reason='Only this terminal attachment was closed; conversation work was not '
                                                'cancelled')
                with patch.object(lifecycle, 'kill', return_value=attachment):
                    self.assertIn('conversation work was not cancelled',
                                  self.command(name, 'worker', '--attachment-only')[0])
        answered = {'state': 'ok', 'reason': 'Answered', 'value': {'answer': 'The parser.', 'usage': {}}}
        with self.subTest(ask='ok'), patch.object(native, 'operation', return_value=answered):
            self.assertEqual(self.command('ask', 'worker', '--question', 'what?'), ('The parser.', '', 0))
            self.assertEqual(json.loads(self.command('ask', 'worker', '--question', 'what?', '--json')[0]),
                             {**answered, 'ok': True})
        # The last row is qa-lifecycle's LC-6 case: a harness with no ask implementation.
        for state, reason in (('unavailable', 'This agent is launch-only: its harness sets inbox_bridge to false'),
                              ('unknown', 'No answer within 120 s; the question may still be answered'),
                              ('error', 'Claude did not answer: api-error'),
                              ('unavailable', 'No isolated native side-question implementation; main conversation '
                                              'unchanged')):
            with self.subTest(ask=state, reason=reason):
                result = {'state': state, 'reason': reason, 'value': None}
                with patch.object(native, 'operation', return_value=result):
                    out, err, code = self.command('ask', 'worker', '--question', 'what?')
                    self.assertEqual((out, code), ('', 1))
                    self.assertIn(reason, err)
                    out, _, code = self.command('ask', 'worker', '--question', 'what?', '--json')
                self.assertEqual((json.loads(out), code), ({**result, 'ok': False}, 1))
        # qa-claude's 9f0331f retest: get-usage said "state: error" in JSON and exited 0.
        measured = {'state': 'ok', 'reason': 'Reported by the multiverse mod',
                    'value': {'context': {'tokens': 82000, 'window': 200000}, 'cost_usd': 1.234, 'total_tokens': 1200000}}
        with self.subTest(usage='ok'), patch.object(native, 'operation', return_value=measured):
            self.assertEqual(self.command('get-usage', 'worker'),
                             ('worker: Context: 82k/200k (41%) · Cost: $1.23 · Total: 1.2m tokens', '', 0))
            self.assertEqual(json.loads(self.command('get-usage', 'worker', '--json')[0]), {**measured, 'ok': True})
        for state, reason in (('unavailable', 'Claude has not saved a transcript for this session yet'),
                              ('error', "Can't read /home/u/.claude/projects: Permission denied")):
            with self.subTest(usage=state):
                result = {'state': state, 'reason': reason, 'value': None}
                with patch.object(native, 'operation', return_value=result):
                    self.assertEqual(self.command('get-usage', 'worker'),
                                     ('', f'mv2: worker: usage unavailable: {reason}', 1))
                    out, _, code = self.command('get-usage', 'worker', '--json')
                self.assertEqual((json.loads(out), code), ({**result, 'ok': False}, 1))
        # qa-claude CLAUDE-7: with --probe, a harness whose executable spawn can't find can do nothing, as spawn says.
        self.harness('claude-missing', {'schema': 2, 'adapter': 'claude',
                                        'settings': {'executable': '/nonexistent/claude'}})
        self.harness('custom-missing', {'schema': 1, 'spawn': ['/nonexistent/tool'], 'resume': ['/nonexistent/tool']})
        self.harness('custom-here', {'schema': 1, 'spawn': ['/bin/sleep', '1'], 'resume': ['/bin/sleep', '1']})
        for name, executable in (('claude-missing', '/nonexistent/claude'), ('custom-missing', '/nonexistent/tool')):
            with self.subTest(probe=name):
                found = capabilities.run(self.root, harness=name, probe=True)
                for operation, value in found.items():
                    self.assertEqual((operation, value['state'], value['reason']),
                                     (operation, 'unavailable', f'Harness executable not found: {executable}'))
                # Without --probe nothing runs or is looked up, as before.
                self.assertEqual(capabilities.run(self.root, harness=name)['spawn']['state'], 'supported')
        self.assertEqual(capabilities.run(self.root, harness='custom-here', probe=True)['spawn']['state'], 'supported')
        with self.subTest('the Python functions keep returning records'):
            with patch.object(lifecycle, 'kill', return_value=self.killed()):
                self.assertEqual(kill.run(self.root, agent='worker'), self.killed())
                self.assertEqual(stop.run(self.root, agent='worker'), self.killed())
            with patch.object(native, 'operation', return_value={'state': 'unavailable', 'reason': 'x', 'value': None}):
                self.assertEqual(ask.run(self.root, agent='worker', question='q'),
                                 {'state': 'unavailable', 'reason': 'x', 'value': None})
                self.assertEqual(get_usage.run(self.root, agent='worker'),
                                 {'state': 'unavailable', 'reason': 'x', 'value': None})
            with patch.object(native, 'capabilities', return_value={}):
                self.assertEqual(capabilities.run(self.root, agent='worker'), {})
            self.assertIsInstance(trash.run(self.root), dict)
            target = self.work / 'py'
            target.mkdir()
            self.assertEqual(init.run(directory=str(target))['name'], 'py')
        for arguments in (['kill', 'x'], ['stop', 'x'], ['remove', 'x'], ['trash'], ['init'],
                          ['ask', 'x', '--question', 'q'], ['get-usage', 'x'], ['subverse', 'x'], ['move', 'x', 'y'],
                          ['capabilities', '--agent', 'x'], ['session-bind', '--session-id', str(uuid4())]):
            with self.subTest(arguments=arguments):
                self.assertTrue(hasattr(cli.tool(arguments[0]), 'cli_run'))
                self.assertFalse(cli.parser().parse_args(['-u', str(self.root), *arguments]).json)
                self.assertTrue(cli.parser().parse_args(['-u', str(self.root), *arguments, '--json']).json)
