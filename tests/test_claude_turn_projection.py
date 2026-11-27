"""The compact Claude turn reader must make the same claims as public history."""

import json
import time
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.claude import transcripts as claude
from multiverse.harnesses.types import TurnEnded, TurnJoined, TurnStarted
from multiverse.plugins import events
from multiverse.plugins.testing import ClaudeRows
from multiverse.runtime import transcripts, turn_events
from multiverse.store import read, write
from multiverse.tools import runtime_update
from test_multiverse import BaseTest


class ClaudeTurnProjectionTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.native, self.launch, name = (str(uuid4()) for _ in range(3))
        self.agent = self.root / 'state' / 'agents' / name
        self.home = self.work / 'claude'
        project = self.home / 'projects' / claude.project_key(str(self.work))
        project.mkdir(parents=True)
        self.path = project / f'{self.native}.jsonl'
        write(self.agent / 'properties.json', {'id': name, 'name': 'turn-test', 'harness': 'claude',
              'workspace': str(self.work), 'native_session_id': self.native, 'native_session_home': str(self.home)})
        write(self.agent / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'idle'})
        self.adapter = harnesses.load(self.root, 'claude')
        _, self.session, _ = transcripts.transcript_snapshot(self.root, self.agent)
        claude._transcripts.clear()
        self.addCleanup(claude._transcripts.clear)

    def append(self, *rows):
        with self.path.open('a') as stream:
            for row in rows:
                stream.write(json.dumps({**row, 'sessionId': self.native}) + '\n')

    def check_projection(self, expected):
        page = transcripts.rows(self.root, self.agent, bodies=False)
        by_ref = {row.ref: row for row in page.rows}
        turns = {turn.id: turn for turn in transcripts.turns(self.root, self.agent).turns}
        with patch.object(transcripts, '_load', side_effect=AssertionError('Cold view used for indexed evidence')):
            actual = transcripts.turn_references(self.root, self.agent, self.launch, self.native, 0,
                                                 tuple(expected), storage=self.session,
                                                 deadline=time.monotonic() + 5)
        for edge, item in zip(expected, actual, strict=True):
            row = by_ref[edge.ref]
            turn = turns[row.turn]
            self.assertEqual(item, transcripts.TurnReference(turn=turn.id, ref=row.cursor,
                                                           generation=page.generation, kind=turn.kind))

    def test_index_preserves_public_membership_across_overlapping_command_and_prompt(self):
        """Stored order alone could wrongly associate a later prompt reply with an intervening command."""
        fixture = ClaudeRows()
        first = fixture.prompt('First')['uuid']
        command = fixture.command('/context', output='Report')['uuid']
        output = fixture.rows[-1]['uuid']
        join = fixture.steer('A steer')['uuid']
        fixture.response('Still part of the prompt')
        end = fixture.turn_end()['uuid']
        second = fixture.prompt('Second')['uuid']
        self.append(*fixture.rows)
        finish = TurnEnded(turn=first, ref=end, kind='prompt', state='finished')
        self.check_projection((
            TurnStarted(turn=first, ref=first, source='person', kind='prompt'),
            TurnJoined(turn=first, ref=join, source='person'),
            TurnEnded(turn=command, ref=output, kind='command', state='finished'),
            finish,
        ))
        wrong = TurnEnded(turn=first, ref=second, kind='prompt', state='finished')
        self.assertIsNone(self.adapter.turn_cursor_projection(self.session, (wrong,)))

    def test_plain_append_uses_only_new_rows_but_a_new_branch_has_to_fall_back(self):
        """A late assistant row can displace the former leaf without a new user prompt."""
        fixture = ClaudeRows()
        first = fixture.prompt('First')
        after = fixture.response('First response')
        self.append(*fixture.rows)
        self.adapter.transcript_view(self.session)
        joined = fixture.steer('New input')
        self.append(joined)
        edge = TurnJoined(turn=first['uuid'], ref=joined['uuid'], source='person')
        with patch.object(claude, 'entry', wraps=claude.entry) as parsed:
            proved, = self.adapter.turn_cursor_projection(self.session, (edge,))
        self.assertEqual(parsed.call_count, 1)
        self.assertIsNotNone(proved.row_position)
        fixture.rewind(first)
        self.append(fixture.response('New branch'))
        old = TurnEnded(turn=first['uuid'], ref=after['uuid'], kind='prompt', state='finished')
        self.assertIsNone(self.adapter.turn_cursor_projection(self.session, (old,)))
        page = transcripts.rows(self.root, self.agent, bodies=False)
        self.assertTrue(next(row for row in page.rows if row.ref == after['uuid']).abandoned)
        self.assertIsNone(self.adapter.turn_cursor_projection(self.session, (old,)))

    def test_an_appended_local_command_opened_by_its_caveat_is_never_claimed_from_new_rows_alone(self):
        """A caveat and its '<command-name>' row are labelled by the rows before them: read from appended rows alone,
        the stamped rows of a command mv2 ran would count as prompt turns, and a plugin from before the caveat opened
        command turns, reporting the name row as a prompt's start, would have it resolved to a turn the history doesn't
        have."""
        fixture = ClaudeRows()
        fixture.prompt('First')
        fixture.response('First response')
        fixture.turn_end()
        self.append(*fixture.rows)
        self.adapter.transcript_view(self.session)  # the saved index: the rows below are an append
        start = len(fixture.rows)
        fixture._prompt = str(uuid4())
        ours = {'kind': 'plugin', 'name': 'multiverse'}
        caveat = fixture.add('user', isMeta=True, origin=ours,
                             message={'role': 'user', 'content': '<local-command-caveat>Caveat</local-command-caveat>'})
        name = fixture.add('user', origin=ours, message={'role': 'user', 'content': '<command-name>/reload-plugins</command-name>'})
        output = fixture.add('system', subtype='local_command', content='<local-command-stdout>Reloaded</local-command-stdout>')
        after = fixture.prompt('Second')
        self.append(*fixture.rows[start:])
        edges = (TurnStarted(turn=caveat['uuid'], ref=caveat['uuid'], source='person', kind='command'),
                 TurnEnded(turn=caveat['uuid'], ref=output['uuid'], kind='command', state='finished'),
                 TurnStarted(turn=after['uuid'], ref=after['uuid'], source='person', kind='prompt'))
        page = transcripts.rows(self.root, self.agent, bodies=False)
        by_ref = {row.ref: row for row in page.rows}
        turns = {turn.id: turn for turn in transcripts.turns(self.root, self.agent).turns}
        self.assertEqual(turns[by_ref[caveat['uuid']].turn].kind, 'command')
        # First, while the saved index still ends before these rows (a full read since may save a newer one).
        claude._transcripts.clear()
        old = TurnStarted(turn=name['uuid'], ref=name['uuid'], source='harness', kind='prompt')
        resolved, = transcripts.turn_references(self.root, self.agent, self.launch, self.native, 0, (old,),
                                                storage=self.session, deadline=time.monotonic() + 5)
        self.assertIn(resolved.turn, (None, *turns))  # never a turn the public history doesn't have
        for batch in (edges, edges[2:]):  # the next prompt alone too: nothing else in the batch to refuse
            claude._transcripts.clear()  # what a new process starts from: the saved index and the appended rows
            actual = transcripts.turn_references(self.root, self.agent, self.launch, self.native, 0, batch,
                                                 storage=self.session, deadline=time.monotonic() + 5)
            for edge, item in zip(batch, actual, strict=True):
                row = by_ref[edge.ref]
                turn = turns[row.turn]
                self.assertEqual(item, transcripts.TurnReference(turn=turn.id, ref=row.cursor,
                                                               generation=page.generation, kind=turn.kind))

    def test_a_command_turn_opened_by_a_caveat_is_named_by_its_echo_and_finished_only_at_its_output(self):
        """The caveat names no command and the echo after it is command_output: counted finished at the echo, GOAT
        would build a /multiverse inbox run before its output, with no leaves, and the turn would name no command.
        Claude's older layout names the command twice, the typed row and its echo: it too is finished only after both."""
        for case, origin in (('typed', None), ('run by mv2', {'kind': 'plugin', 'name': 'multiverse'})):
            with self.subTest(case):
                self.path.unlink(missing_ok=True)
                claude._transcripts.clear()
                fixture = ClaudeRows()
                fixture.prompt('First')
                fixture.response('First response')
                fixture.turn_end()
                fixture._prompt = str(uuid4())
                stamp = {'origin': origin} if origin else {}
                caveat = fixture.add('user', isMeta=True, **stamp,
                                     message={'role': 'user', 'content': '<local-command-caveat>Caveat</local-command-caveat>'})
                naming_text = '<command-name>/multiverse</command-name>\n<command-args>inbox</command-args>'
                fixture.add('user', **stamp, message={'role': 'user', 'content': naming_text})
                self.append(*fixture.rows)
                turn = next(turn for turn in transcripts.turns(self.root, self.agent).turns if turn.id == caveat['uuid'])
                self.assertEqual((turn.kind, turn.command, turn.args, turn.finished), ('command', '/multiverse', 'inbox', False))
                self.assertEqual(turn.preview, naming_text)
                self.append(fixture.add('system', subtype='local_command', content='<local-command-stdout>Inbox</local-command-stdout>'))
                turn = next(turn for turn in transcripts.turns(self.root, self.agent).turns if turn.id == caveat['uuid'])
                self.assertTrue(turn.finished)
        with self.subTest('the older layout: a typed command row, then its echo'):
            self.path.unlink(missing_ok=True)
            claude._transcripts.clear()
            fixture = ClaudeRows()
            fixture.prompt('First')
            fixture.response('First response')
            fixture.turn_end()
            fixture._prompt = str(uuid4())
            typed = fixture.add('user', message={'role': 'user', 'content': '/compact keep the plan'})
            fixture.add('user', message={'role': 'user', 'content': '<command-name>/compact</command-name>\n<command-args>keep the plan</command-args>'})
            self.append(*fixture.rows)
            turn = next(turn for turn in transcripts.turns(self.root, self.agent).turns if turn.id == typed['uuid'])
            self.assertEqual((turn.kind, turn.command, turn.args, turn.finished), ('command', '/compact', 'keep the plan', False))
            self.append(fixture.add('user', message={'role': 'user', 'content': '<local-command-stdout>Compacted</local-command-stdout>'}))
            turn = next(turn for turn in transcripts.turns(self.root, self.agent).turns if turn.id == typed['uuid'])
            self.assertTrue(turn.finished)

    def test_a_working_compact_s_caveat_is_in_the_compact_s_own_turn(self):
        """A /compact that worked stores its caveat in its compaction's span, before the row that names it; read as
        output, core put it in the last command turn before (an old /reload-plugins), which gained a row after its
        end, and the /compact's turn lacked its first row (qa-claude's Q-4)."""
        def user(uuid, parent, text, prompt, **more):
            return {'type': 'user', 'uuid': uuid, 'parentUuid': parent, 'promptId': prompt, 'timestamp': '2026-11-27T10:00:00Z',
                    'message': {'role': 'user', 'content': text}, **more}
        caveat = '<local-command-caveat>Caveat</local-command-caveat>'
        self.append(
            user('r1', None, caveat, 'q1', isMeta=True),
            user('r2', 'r1', '<command-name>/reload-plugins</command-name>', 'q1'),
            {'type': 'system', 'subtype': 'local_command', 'uuid': 'r3', 'parentUuid': 'r2',
             'content': '<local-command-stdout>Reloaded</local-command-stdout>'},
            user('p1', 'r3', 'Say one.', 'q2', origin={'kind': 'plugin'}),
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'p1',
             'message': {'id': 'm1', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'one'}], 'usage': {'output_tokens': 1}}},
            {'type': 'system', 'subtype': 'turn_duration', 'uuid': 'd1', 'parentUuid': 'a1'},
            {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'b1', 'parentUuid': None, 'logicalParentUuid': 'd1'},
            user('s1', 'b1', 'This session is being continued.', 'q3', isCompactSummary=True),
            user('c1', 's1', caveat, 'q3', isMeta=True),
            user('c2', 'c1', '<command-name>/compact</command-name>', 'q3'),
            user('c3', 'c2', '<local-command-stdout>Compacted</local-command-stdout>', 'q3'),
            user('t1', 'c3', caveat, 'q4', isMeta=True),
            user('t2', 't1', '<command-name>/reload-plugins</command-name>', 'q4'),
            {'type': 'system', 'subtype': 'local_command', 'uuid': 't3', 'parentUuid': 't2',
             'content': '<local-command-stdout>Reloaded</local-command-stdout>'})
        page = transcripts.rows(self.root, self.agent, bodies=False)
        turn = {row.ref: row.turn for row in page.rows}
        self.assertEqual({ref: turn[ref] for ref in ('c1', 'c2', 'c3')}, {'c1': 'c1', 'c2': 'c1', 'c3': 'c1'})
        self.assertEqual([ref for ref, of in turn.items() if of == 'r1'], ['r1', 'r2', 'r3'])  # the old command keeps its own
        turns = {item.id: item for item in transcripts.turns(self.root, self.agent).turns}
        self.assertEqual((turns['c1'].kind, turns['c1'].command, turns['c1'].finished), ('command', '/compact', True))
        self.assertEqual([turn[ref] for ref in ('t1', 't2', 't3')], ['t1', 't1', 't1'])  # the command typed next: its own

    def test_appended_system_caveat_keeps_the_system_command_opening(self):
        """The new user caveat label must not promote legacy system command output to an opener."""
        fixture = ClaudeRows()
        fixture.prompt('Initial input')
        command = fixture.add('system', subtype='local_command', content='<command-name>/context</command-name>')
        self.append(*fixture.rows)
        self.adapter.transcript_view(self.session)
        caveat = fixture.add('system', subtype='local_command',
                             content='<local-command-caveat>Notice</local-command-caveat>')
        self.append(caveat)
        edge = TurnEnded(turn=command['uuid'], ref=caveat['uuid'], kind='command', state='finished')
        page = transcripts.rows(self.root, self.agent, bodies=False)
        row = next(row for row in page.rows if row.ref == caveat['uuid'])
        with self.subTest(reader='public history'):
            self.assertEqual(row.turn, command['uuid'])
        with self.subTest(reader='indexed append'):
            proof = self.adapter.turn_cursor_projection(self.session, (edge,))
            self.assertIsNotNone(proof)
            self.assertEqual(proof[0].row_nearest, caveat['uuid'])
            self.assertEqual(proof[0].turn_nearest, command['uuid'])

    def test_worker_builds_missing_index_before_requesting_a_full_view(self):
        """Preparing a missing index must use the worker's deadline and stay off the status path."""
        fixture = ClaudeRows()
        first = fixture.prompt('First')['uuid']
        self.append(*fixture.rows)
        edge = TurnStarted(turn=first, ref=first, source='person', kind='prompt')
        deadline = time.monotonic() + 5
        original = type(self.adapter).transcript_index

        def prepare(adapter, session, *, reader):
            self.assertEqual(reader.deadline, deadline)
            return original(adapter, session, reader=reader)

        with patch.object(type(self.adapter), 'transcript_index', autospec=True, side_effect=prepare) as build, \
                patch.object(transcripts, '_load', side_effect=AssertionError('Cold view used for indexed evidence')):
            with patch.object(runtime_update, 'callback', return_value=(self.root, self.agent, self.launch)), \
                    patch.object(events, 'subscribers', return_value=['projection-fixture']):
                runtime_update.run(status='working', turn_session_id=self.native,
                                   turn_events_json=json.dumps([turn_events.encode(edge)]))
            self.assertEqual(read(self.agent / 'runtime.json')['status'], 'working')
            with self.assertRaises(transcripts.TurnIndexRequired):
                transcripts.turn_references(self.root, self.agent, self.launch, self.native, 0, (edge,),
                                            storage=self.session, deadline=deadline, indexed_only=True)
            build.assert_not_called()
            proved, = transcripts.turn_references(self.root, self.agent, self.launch, self.native, 0, (edge,),
                                                  storage=self.session, deadline=deadline, prepare_index=True)
            build.assert_called_once()
        self.assertEqual(proved.turn, first)
        self.assertEqual(len(list((self.agent / claude.ROW_STORE).glob('*.idx'))), 1)
