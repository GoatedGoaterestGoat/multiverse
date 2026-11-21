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
