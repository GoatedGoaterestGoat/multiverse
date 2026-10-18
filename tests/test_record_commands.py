"""Readable output for the commands that printed raw records (qa-lifecycle LC-4, LC-5, LC-6).

Each prints sentences by default and its original record with --json; the Python `run` functions the dashboard
and scripts call are unchanged. `mv2 ask` fails (exit 1) when it has no answer.
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
from multiverse.tools import ask, capabilities, init, inbox, kill, move, remove, session_bind, stop, subverse, trash
from multiverse.universe import relocate, trash as trash_store
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

    def test_kill_and_stop_say_what_happened(self):
        for name in ('kill', 'stop'):
            with self.subTest(command=name):
                with patch.object(lifecycle, 'kill', return_value=self.killed(cancelled_startup_messages=0)):
                    self.assertEqual(self.command(name, 'worker'), ('Stopped worker.', '', 0))
                with patch.object(lifecycle, 'kill', return_value=self.killed(cancelled_startup_messages=2)):
                    self.assertEqual(self.command(name, 'worker')[0],
                                     'Stopped worker. 2 messages waiting for it to start were not delivered.')
                unconfirmed = self.killed('unknown', 'Native cancellation unconfirmed: socket closed. Retry stop '
                                          'when the native service is reachable.', cancelled_startup_messages=1)
                with patch.object(lifecycle, 'kill', return_value=unconfirmed):
                    out, _, code = self.command(name, 'worker')
                self.assertTrue(out.startswith("Stopped worker, but couldn't confirm its work was cancelled"), out)
                self.assertIn('socket closed', out)
                self.assertNotIn(AGENT_ID, out)
                with patch.object(lifecycle, 'kill', return_value=unconfirmed):
                    out, _, code = self.command(name, 'worker', '--json')
                self.assertEqual(json.loads(out), unconfirmed)
                attachment = self.killed(reason='Only this terminal attachment was closed; conversation work was not cancelled')
                with patch.object(lifecycle, 'kill', return_value=attachment):
                    self.assertEqual(self.command(name, 'worker', '--attachment-only')[0],
                                     "Closed worker's terminal. Only this terminal attachment was closed; "
                                     'conversation work was not cancelled.')

    def test_remove_and_trash_say_what_moved_and_what_is_there(self):
        record = {'kind': 'agent', 'name': 'worker', 'id': AGENT_ID, 'origin': '/x', 'removed_at': '2026-10-18T00:30:54+00:00',
                  'trash': str(self.root / 'trash/20261018T003054-worker')}
        subverse = {**record, 'kind': 'subverse', 'name': 'review'}
        with patch.object(relocate, 'remove_agent', return_value=record):
            self.assertEqual(self.command('remove', 'worker'), ('Moved worker to the trash.', '', 0))
            self.assertEqual(json.loads(self.command('remove', 'worker', '--json')[0]), record)
        with patch.object(relocate, 'exact', return_value=self.root), \
                patch.object(relocate, 'remove_subverse', return_value=subverse):
            self.assertEqual(self.command('remove', '--subverse', 'review')[0], 'Moved subverse review to the trash.')
        with patch.object(trash_store, 'contents', return_value={'entries': [], 'emptied': False}):
            self.assertEqual(self.command('trash')[0], 'The trash is empty.')
        with patch.object(trash_store, 'contents', return_value={'entries': [record, subverse], 'emptied': False}):
            out = self.command('trash')[0]
            self.assertEqual(out.splitlines(), ['agent worker, removed 2026-10-18 00:30 UTC',
                                                'subverse review, removed 2026-10-18 00:30 UTC'])
            self.assertEqual(json.loads(self.command('trash', '--json')[0])['entries'], [record, subverse])
        with patch.object(trash_store, 'contents', return_value={'entries': [record, subverse], 'emptied': True}):
            self.assertEqual(self.command('trash', '--empty')[0], 'Deleted 2 entries from the trash for good.')
        with patch.object(trash_store, 'contents', return_value={'entries': [], 'emptied': True}):
            self.assertEqual(self.command('trash', '--empty')[0], 'The trash was already empty.')

    def test_session_bind_and_init_answer_in_a_sentence(self):
        bound = {'id': AGENT_ID, 'name': 'worker', 'native_session_id': str(uuid4())}
        with patch.object(session_bind, 'run', wraps=session_bind.run), \
                patch('multiverse.tools.session_bind.callback', return_value=(self.root, None, 'launch')), \
                patch('multiverse.universe.agents.bind_session', return_value=bound):
            out, _, code = self.command('session-bind', '--agent', 'worker', '--session-id', bound['native_session_id'])
            self.assertEqual((out, code), ("Recorded worker's session.", 0))
            self.assertNotIn(bound['native_session_id'], out)
            self.assertEqual(json.loads(self.command('session-bind', '--agent', 'worker', '--session-id',
                                                     bound['native_session_id'], '--json')[0]), bound)
        target = self.work / 'fresh'
        target.mkdir()
        out, _, code = self.command('init', str(target), '--name', 'scratch')
        self.assertEqual((out, code), (f'Created universe scratch in {target / ".multiverse"}.', 0))
        other = self.work / 'other'
        other.mkdir()
        record = json.loads(self.command('init', str(other), '--json')[0])
        self.assertEqual((record['name'], record['universe']), ('other', str(other / '.multiverse')))

    def test_subverse_and_move_answer_in_a_sentence(self):
        out, _, code = self.command('subverse', 'docs')
        self.assertEqual((out, code), (f'Created subverse docs in {self.root / "subverses/docs"}.', 0))
        record = json.loads(self.command('subverse', 'notes', '--json')[0])
        self.assertEqual((record['name'], record['universe']), ('notes', str(self.root / 'subverses/notes')))
        self.command('subverse', 'review')
        review = self.root / 'subverses/review'
        moved = {'id': AGENT_ID, 'name': 'worker', 'harness': 'codex', 'universe': str(review)}
        with patch.object(relocate, 'move_agent', return_value=moved):
            self.assertEqual(self.command('move', 'worker', 'review'), (f'Moved worker to review ({review}).', '', 0))
            self.assertEqual(json.loads(self.command('move', 'worker', 'review', '--json')[0]), moved)
        target = self.work / 'elsewhere/.multiverse'
        subverse = {'universe': str(target), 'name': 'verify', 'schema': 1}
        with patch.object(relocate, 'exact', side_effect=lambda value, root=None: Path(value)), \
                patch.object(relocate, 'move_subverse', return_value=subverse):
            self.assertEqual(self.command('move', '--subverse', 'review/verify', str(self.work / 'elsewhere'))[0],
                             f'Moved subverse verify to {target}.')

    def test_ask_prints_the_answer_and_fails_when_there_is_none(self):
        answered = {'state': 'ok', 'reason': 'Answered', 'value': {'answer': 'The parser.', 'usage': {}}}
        with patch.object(native, 'operation', return_value=answered):
            self.assertEqual(self.command('ask', 'worker', '--question', 'what?'), ('The parser.', '', 0))
            self.assertEqual(json.loads(self.command('ask', 'worker', '--question', 'what?', '--json')[0]),
                             {**answered, 'ok': True})
        for state, reason in (('unavailable', 'This agent is launch-only: its harness sets inbox_bridge to false'),
                              ('unknown', 'No answer within 120 s; the question may still be answered'),
                              ('error', 'Claude did not answer: api-error')):
            with self.subTest(state=state):
                result = {'state': state, 'reason': reason, 'value': None}
                with patch.object(native, 'operation', return_value=result):
                    self.assertEqual(self.command('ask', 'worker', '--question', 'what?'),
                                     ('', f"mv2: worker can't answer: {reason}", 1))
                    out, _, code = self.command('ask', 'worker', '--question', 'what?', '--json')
                self.assertEqual((json.loads(out), code), ({**result, 'ok': False}, 1))
        # qa-lifecycle's LC-6 case: a harness with no ask implementation.
        none = {'state': 'unavailable', 'reason': 'No isolated native side-question implementation; main conversation unchanged', 'value': None}
        with patch.object(native, 'operation', return_value=none):
            self.assertEqual(self.command('ask', 'worker', '--question', 'what?')[2], 1)

    def test_capabilities_name_each_operation_in_words(self):
        found = {'send_steer': {'state': 'supported', 'reason': '', 'mechanism': 'claude-peer-inbox', 'required': True},
                 'ask': {'state': 'unavailable', 'reason': 'This agent is launch-only: its harness sets inbox_bridge to false',
                         'mechanism': None, 'required': False},
                 'get_usage': {'state': 'error', 'reason': 'Transcript unreadable', 'mechanism': None, 'required': False},
                 'compaction': {'state': 'supported', 'reason': '', 'mechanism': 'claude-mod-session-compact', 'required': False}}
        with patch.object(native, 'capabilities', return_value=found):
            out, _, code = self.command('capabilities', '--agent', 'worker')
            self.assertEqual(out.splitlines(), ['steer: supported',
                                                'ask: unavailable: This agent is launch-only: its harness sets inbox_bridge to false',
                                                'usage: error: Transcript unreadable',
                                                'compaction: supported'])
            self.assertEqual(json.loads(self.command('capabilities', '--agent', 'worker', '--json')[0]), found)

    def test_inbox_history_that_matches_nothing_says_what_was_asked(self):
        empty = {'messages': [], 'cursor': 0}
        with patch.object(inbox, 'run', side_effect=lambda root, **options: options['_present'](root, empty) or empty):
            self.assertEqual(self.command('inbox', '--agent', 'worker')[0], 'No unread messages.')
            self.assertEqual(self.command('inbox', '--agent', 'worker', '--all')[0], 'No messages.')
            self.assertEqual(self.command('inbox', '--agent', 'worker', '--all', '--since', '2099-01-01T00:00:00Z')[0],
                             'No messages since 2099-01-01T00:00:00Z.')
            self.assertEqual(self.command('inbox', '--agent', 'worker', '--all', '--since', '2h')[0],
                             'No messages in the last 2h.')
            self.assertEqual(self.command('inbox', '--agent', 'worker', '--all', '--last', '3')[0], 'No messages.')

    def test_the_python_functions_keep_returning_records(self):
        with patch.object(lifecycle, 'kill', return_value=self.killed()):
            self.assertEqual(kill.run(self.root, agent='worker'), self.killed())
            self.assertEqual(stop.run(self.root, agent='worker'), self.killed())
        with patch.object(native, 'operation', return_value={'state': 'unavailable', 'reason': 'x', 'value': None}):
            self.assertEqual(ask.run(self.root, agent='worker', question='q'), {'state': 'unavailable', 'reason': 'x', 'value': None})
        with patch.object(native, 'capabilities', return_value={}):
            self.assertEqual(capabilities.run(self.root, agent='worker'), {})
        self.assertIsInstance(trash.run(self.root), dict)
        target = self.work / 'py'
        target.mkdir()
        self.assertEqual(init.run(directory=str(target))['name'], 'py')
        for arguments in (['kill', 'x'], ['stop', 'x'], ['remove', 'x'], ['trash'], ['init'], ['ask', 'x', '--question', 'q'],
                          ['subverse', 'x'], ['move', 'x', 'y'],
                          ['capabilities', '--agent', 'x'], ['session-bind', '--session-id', str(uuid4())]):
            with self.subTest(arguments=arguments):
                self.assertTrue(hasattr(cli.tool(arguments[0]), 'cli_run'))
                self.assertFalse(cli.parser().parse_args(['-u', str(self.root), *arguments]).json)
                self.assertTrue(cli.parser().parse_args(['-u', str(self.root), *arguments, '--json']).json)


class ProbeExecutableTest(BaseTest):
    """qa-claude CLAUDE-7: with --probe, a harness whose executable spawn can't find can do nothing, as spawn says."""

    def harness(self, name, definition):
        directory = self.root / 'harnesses' / name
        directory.mkdir(parents=True, exist_ok=True)
        write(directory / 'harness.json', definition)

    def test_a_missing_executable_makes_the_whole_harness_unavailable_under_probe(self):
        self.harness('claude-missing', {'schema': 2, 'adapter': 'claude', 'settings': {'executable': '/nonexistent/claude'}})
        self.harness('custom-missing', {'schema': 1, 'spawn': ['/nonexistent/tool'], 'resume': ['/nonexistent/tool']})
        self.harness('custom-here', {'schema': 1, 'spawn': ['/bin/sleep', '1'], 'resume': ['/bin/sleep', '1']})
        for name, executable in (('claude-missing', '/nonexistent/claude'), ('custom-missing', '/nonexistent/tool')):
            with self.subTest(harness=name):
                found = capabilities.run(self.root, harness=name, probe=True)
                for operation, value in found.items():
                    self.assertEqual((operation, value['state'], value['reason']),
                                     (operation, 'unavailable', f'Harness executable not found: {executable}'))
                # Without --probe nothing runs or is looked up, as before.
                self.assertEqual(capabilities.run(self.root, harness=name)['spawn']['state'], 'supported')
        self.assertEqual(capabilities.run(self.root, harness='custom-here', probe=True)['spawn']['state'], 'supported')
