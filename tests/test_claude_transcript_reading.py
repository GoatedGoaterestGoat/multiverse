"""Reading a Claude session's transcript: labels, history, positions and copies (harnesses/claude/transcripts.py)."""
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.claude import NO_STORAGE, transcripts as t
from multiverse.harnesses.types import SessionHandle, TranscriptRowsChanged
from test_multiverse import BaseTest


class ClaudeTranscriptReadingTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.storage = self.work / 'claude-config'
        self.environment = {'CLAUDE_CONFIG_DIR': str(self.storage)}
        self.native = str(uuid4())
        self.project = self.storage / 'projects' / t.project_key(os.path.realpath(self.work))
        self.project.mkdir(parents=True)
        self.path = self.project / f'{self.native}.jsonl'
        t._transcripts.clear()
        self.addCleanup(t._transcripts.clear)

    def adapter(self):
        return harnesses.load(self.root, 'claude')

    def session(self):
        agent = self.work / 'agent'
        agent.mkdir(exist_ok=True)
        return SessionHandle(str(uuid4()), 'claude', str(self.work), native_id=self.native, home=str(self.storage),
                             session_file=str(agent / 'session.jsonl'))

    def write(self, rows, path=None):
        (path or self.path).write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def read(self, tip=None):
        index = t.newest_copy(self.native, self.environment, (str(self.work),))
        return index, t.analyse(index, tip)

    def test_each_row_is_labelled_by_claudes_own_fields(self):
        """Labels decide what a plugin counts as a prompt or a turn's end; fragile because Claude marks them only by
        loose fields (origin, promptId, attachment types, a compaction's span), and a wrong rule turns a compaction's
        hundreds of stored rows into mid-turn messages, or an interrupted turn into one that never ends."""
        rows = [
            ('typed prompt', {'type': 'user', 'uuid': 'u1', 'promptId': 'p1', 'origin': {'kind': 'human'},
                              'message': {'role': 'user', 'content': 'Fix the bug.'}}, ('prompt', 'human')),
            ('response', {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1', 'message': {'id': 'm1', 'stop_reason': 'tool_use',
                          'content': [{'type': 'tool_use', 'id': 'c1', 'name': 'Bash', 'input': {}}],
                          'usage': {'input_tokens': 5, 'output_tokens': 7}}}, ('response', None)),
            ('tool result', {'type': 'user', 'uuid': 'r1', 'parentUuid': 'a1', 'promptId': 'p1', 'toolUseResult': {},
                             'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c1'}]}},
             ('tool_result', None)),
            ('mid-turn steer', {'type': 'attachment', 'uuid': 'q1', 'parentUuid': 'r1', 'attachment': {
                'type': 'queued_command', 'commandMode': 'prompt', 'prompt': 'Message from lead:\nRebase.',
                'origin': {'kind': 'peer'}}}, ('mid_turn', 'peer')),
            ('merged background notice', {'type': 'attachment', 'uuid': 'q2', 'parentUuid': 'q1', 'attachment': {
                'type': 'queued_command', 'commandMode': 'task-notification', 'prompt': 'done'}}, ('injected', None)),
            ('reminder', {'type': 'attachment', 'uuid': 'x1', 'parentUuid': 'q2', 'attachment': {'type': 'date'}}, ('injected', None)),
            ('skill instructions', {'type': 'user', 'uuid': 'k1', 'parentUuid': 'x1', 'promptId': 'p1', 'isMeta': True,
                                    'turnCompanion': True, 'sourceToolUseID': 'c1',
                                    'message': {'role': 'user', 'content': 'Base directory for this skill'}}, ('injected', None)),
            ('interruption', {'type': 'user', 'uuid': 'i1', 'parentUuid': 'k1', 'promptId': 'p1',
                              'message': {'role': 'user', 'content': [{'type': 'text', 'text': '[Request interrupted by user]'}]}},
             ('injected', None)),
            ('turn end', {'type': 'system', 'subtype': 'turn_duration', 'uuid': 'd1', 'parentUuid': 'i1'}, ('meta', None)),
            ('typed command', {'type': 'user', 'uuid': 'c2', 'parentUuid': 'd1', 'promptId': 'p2',
                               'message': {'role': 'user', 'content': '/compact'}}, ('command', None)),
            ('its echo', {'type': 'user', 'uuid': 'c3', 'parentUuid': 'c2', 'promptId': 'p2',
                          'message': {'role': 'user', 'content': '<command-name>/compact</command-name>'}}, ('command_output', None)),
            ('boundary', {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'b1', 'logicalParentUuid': 'd1'},
             ('compaction', None)),
            ('a stored row', {'type': 'user', 'uuid': 's1', 'parentUuid': 'b1', 'promptId': 'p2',
                              'message': {'role': 'user', 'content': 'Fix the bug.'}}, ('compaction', None)),
            ('a stored response', {'type': 'assistant', 'uuid': 's2', 'parentUuid': 's1', 'message': {
                'id': 'm9', 'model': '<synthetic>', 'usage': {'input_tokens': 0, 'output_tokens': 0}, 'content': []}},
             ('compaction', None)),
            ('a stored tool result', {'type': 'user', 'uuid': 's3', 'parentUuid': 's2', 'promptId': 'p2',
                                      'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c9'}]}},
             ('compaction', None)),
            ('the stdout after it', {'type': 'user', 'uuid': 'o1', 'parentUuid': 's3', 'promptId': 'p2',
                                     'message': {'role': 'user', 'content': '<local-command-stdout>Compacted</local-command-stdout>'}},
             ('command_output', None)),
            ('the next real prompt ends the span', {'type': 'user', 'uuid': 'u2', 'parentUuid': 'o1', 'promptId': 'p3',
                                                    'origin': {'kind': 'plugin'}, 'message': {'role': 'user', 'content': 'Next.'}},
             ('prompt', 'plugin')),
            ('a later row of that turn', {'type': 'user', 'uuid': 'u3', 'parentUuid': 'u2', 'promptId': 'p3',
                                          'message': {'role': 'user', 'content': 'Also this.'}}, ('injected', None)),
            ('a title', {'type': 'ai-title', 'aiTitle': 'x'}, ('meta', None)),
        ]
        self.write([row for _, row, _ in rows])
        index, analysis = self.read()
        for (name, _, expected), got in zip(rows, analysis.labels):
            with self.subTest(name):
                self.assertEqual(got, expected)
        ends = [name for (name, _, _), item in zip(rows, index.entries) if t.ends_turn(item)]
        self.assertEqual(ends, ['interruption', 'turn end'])  # an Esc usually writes no turn_duration

    def test_the_history_follows_claudes_links(self):
        """The history decides which rows a plugin reads as the conversation; fragile because Claude's links are
        loose: a rewind leaves a branch behind, parallel results branch off their own calls, a boundary links past the
        typed /compact row, and a gap behind Claude's own compaction must start at its summary, not be reported."""
        def user(uuid, parent, text, **more):
            return {'type': 'user', 'uuid': uuid, 'parentUuid': parent, 'message': {'role': 'user', 'content': text}, **more}
        call = lambda uuid, parent, ids, mid: {'type': 'assistant', 'uuid': uuid, 'parentUuid': parent, 'message': {
            'id': mid, 'content': [{'type': 'tool_use', 'id': i} for i in ids], 'usage': {'output_tokens': 1}}}
        result = lambda uuid, parent, answers: {'type': 'user', 'uuid': uuid, 'parentUuid': parent, 'toolUseResult': {},
                                                'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': answers}]}}
        rows = [user('u1', None, 'Start', origin={'kind': 'human'}, promptId='p1', timestamp='2026-10-25T10:00:00Z'),
                call('a1', 'u1', ['c1', 'c2'], 'm1'),
                result('r1', 'a1', 'c1'), result('r2', 'a1', 'c2'),             # parallel: r1 branches off its own call
                user('old', 'r2', 'Abandoned branch', origin={'kind': 'human'}, promptId='p2', timestamp='2026-10-25T10:01:00Z'),
                user('new', 'r2', 'Rewound to here', origin={'kind': 'human'}, promptId='p3', timestamp='2026-10-25T10:02:00Z'),
                user('typed', 'new', '/compact', promptId='p4', timestamp='2026-10-25T10:03:00Z'),
                {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'b1', 'logicalParentUuid': 'new',
                 'timestamp': '2026-10-25T10:04:00Z'},
                user('after', 'b1', 'Next', origin={'kind': 'human'}, promptId='p5', timestamp='2026-10-25T10:05:00Z')]
        rows.insert(5, {'type': 'api-request', 'note': 'for the abandoned branch'})  # right after 'old'
        rows.append({'type': 'last-prompt', 'note': 'after the newest row'})
        self.write(rows)
        index, analysis = self.read()
        refs = [index.entries[i].ref for i in analysis.history.rows]
        # A row without a ref follows the branch of the row before it: the one after 'old' is abandoned with it.
        self.assertEqual(refs, ['u1', 'a1', 'r1', 'r2', 'new', 'typed', 'b1', 'after', None])
        self.assertEqual(index.entries[analysis.history.rows[-1]].kind, 'last-prompt')
        self.assertEqual((analysis.history.tip, analysis.history.gap), ('after', None))
        # A tip names the branch only the live session knows is current, right after a rewind.
        _, tipped = self.read(tip='old')
        self.assertEqual([index.entries[i].kind for i in tipped.history.rows][-2:], ['user', 'api-request'])  # 'old', then its row
        with self.subTest('a gap behind Claude own compaction'):
            rows = [user('kept', 'missing', 'Kept verbatim'),
                    {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'b2', 'logicalParentUuid': 'kept',
                     'compactMetadata': {'preservedMessages': {'uuids': ['kept']}}},
                    user('summary', 'b2', 'Summary of earlier work', isCompactSummary=True),
                    user('later', 'summary', 'Go on', origin={'kind': 'human'}, promptId='p9')]
            self.write(rows)
            index, analysis = self.read()
            self.assertEqual([index.entries[i].ref for i in analysis.history.rows], ['b2', 'summary', 'kept', 'later'])
            self.assertEqual((analysis.history.gap, analysis.history.summary), (None, 'summary'))
        with self.subTest('a gap with no compaction is reported'):
            self.write([user('orphan', 'never-stored', 'After a failed write', origin={'kind': 'human'}, promptId='p1')])
            _, analysis = self.read()
            self.assertEqual(analysis.history.gap, 'never-stored')

    def test_a_position_finds_a_rewrite_and_usage_starts_over(self):
        """A cursor must notice when Claude rewrites the file in place; fragile because truncating at a tombstoned
        row keeps the inode, and a file that grows back past the old offset would otherwise read shifted rows as new,
        and usage would keep a removed response's tokens (reset on a same-inode rewrite from this commit on)."""
        response = lambda uuid, mid, tokens: {'type': 'assistant', 'uuid': uuid, 'message': {
            'id': mid, 'usage': {'input_tokens': tokens}}}
        first = [response('a', 'm1', 10), response('b', 'm2', 20), response('c', 'm3', 30)]
        self.write(first)
        index, analysis = self.read()
        before = [t.position_of(item) for item in index.entries]
        self.assertEqual(sorted(index.responses), ['m1', 'm2', 'm3'])
        view = self.adapter().transcript_view(self.session())
        inode = self.path.stat().st_ino
        with open(self.path, 'r+b') as handle:  # tombstone m2: the rest moves up in place, then the file grows back
            handle.truncate(0)
            handle.write(''.join(json.dumps(row) + '\n' for row in [first[0], first[2], response('d', 'm4', 400000)]).encode())
        self.assertEqual(self.path.stat().st_ino, inode)
        index, analysis = self.read()
        self.assertEqual((index.rewrites, sorted(index.responses)), (1, ['m1', 'm3', 'm4']))
        self.assertEqual(t.position_of(index.entries[0]), before[0])  # rows before the change keep their position
        self.assertNotEqual(t.position_of(index.entries[1]), before[2])  # 'c' moved up: its position changed
        self.assertEqual(analysis.version, 1)
        with self.assertRaises(TranscriptRowsChanged):  # a body from the old view is refused, never mixed in
            self.adapter().transcript_bodies(self.session(), view, [1], max_bytes=1 << 20)
        with self.subTest('an equal-length rewrite of an earlier row'):
            view = self.adapter().transcript_view(self.session())
            before = [fact.position for fact in view.rows]
            text = self.path.read_bytes()
            changed = text.replace(b'"input_tokens": 10}', b'"input_tokens": 11}', 1)  # same length, same offsets
            self.assertEqual((len(changed), changed != text), (len(text), True))
            status = self.path.stat()
            self.path.write_bytes(changed)
            os.utime(self.path, ns=(status.st_atime_ns, status.st_mtime_ns + 1000))
            index, _ = self.read()
            after = [t.position_of(item) for item in index.entries]
            self.assertEqual(index.rewrites, 2)
            self.assertNotEqual(after[1:], before[1:])  # every later row's position changes: its offset didn't
            with self.assertRaises(TranscriptRowsChanged):
                self.adapter().transcript_bodies(self.session(), view, [2], max_bytes=1 << 20)
        with self.subTest('an append changes no earlier position'):
            index, _ = self.read()
            kept = [t.position_of(item) for item in index.entries]
            with open(self.path, 'ab') as handle:
                handle.write((json.dumps(response('e', 'm5', 5)) + '\n').encode())
            index, _ = self.read()
            self.assertEqual(([t.position_of(item) for item in index.entries][:len(kept)], index.rewrites), (kept, 2))

    def test_usage_checks_a_tail_and_a_transcript_read_proves_the_rest(self):
        """Usage reads the shared index on every listing; fragile because proving the whole prefix there costs a full
        read per change and goes unnoticed, while a transcript read must still catch an earlier same-length edit."""
        rows = [{'type': 'assistant', 'uuid': f'u{n}', 'message': {'id': f'm{n}', 'usage': {'input_tokens': n},
                 'content': 'x' * 500}} for n in range(400)]  # well over TAIL
        self.write(rows)
        index = t._transcripts[self.path] = t.Transcript(self.path).read()  # the index usage and the reads share
        self.assertGreater(index.offset, 2 * t.TAIL)
        with patch.object(t.Transcript, '_intact', side_effect=AssertionError('usage proved the whole prefix')):
            with open(self.path, 'ab') as handle:
                handle.write((json.dumps(rows[0] | {'uuid': 'more'}) + '\n').encode())
            index.read()
        self.assertEqual((index.rewrites, len(index.entries)), (0, 401))
        with self.subTest('a rewrite inside the tail starts usage over'):
            text = self.path.read_bytes()
            self.path.write_bytes(text[:-10] + b'y' * 9 + b'\n')  # same size, new modification time
            os.utime(self.path, ns=(self.path.stat().st_atime_ns, self.path.stat().st_mtime_ns + 1000))
            self.assertEqual(index.read().rewrites, 1)
        with self.subTest('a transcript read proves what usage only tail-checked'):
            self.read()
            text = self.path.read_bytes()
            changed = text.replace(b'"input_tokens": 3}', b'"input_tokens": 4}', 1)  # same length, before the tail
            self.assertEqual((len(changed), changed != text), (len(text), True))
            self.path.write_bytes(changed)
            os.utime(self.path, ns=(self.path.stat().st_atime_ns, self.path.stat().st_mtime_ns + 2000))
            index.read()  # usage: the edit is before the tail, so it is not seen here
            self.assertEqual(index.rewrites, 1)
            self.assertEqual(self.read()[0].rewrites, 2)  # the transcript read, through newest_copy

    def test_older_copies_must_be_part_of_the_newest(self):
        """The reads take the newest copy only while older copies hold a uuid prefix of its rows; fragile because
        Claude moves a transcript by renaming it, so a second copy is an anomaly, and reading past one that disagrees
        would show a conversation that isn't the session's."""
        rows = [{'type': 'user', 'uuid': f'u{n}', 'message': {'role': 'user', 'content': str(n)}} for n in range(3)]
        other = self.storage / 'projects' / (t.project_key(os.path.realpath(self.work)) + '-old')
        other.mkdir()
        older = other / self.path.name
        self.write(rows[:2], older)
        self.write(rows)
        os.utime(older, (1, 1))
        self.assertEqual(t.newest_copy(self.native, self.environment, (str(self.work),)).path, self.path)
        self.write([rows[0], {'type': 'user', 'uuid': 'elsewhere'}], older)
        os.utime(older, (1, 1))
        t._transcripts.clear()
        with self.assertRaises(t.CantTell) as raised:
            t.newest_copy(self.native, self.environment, (str(self.work),))
        self.assertIn(str(older), str(raised.exception))

    def test_the_provider_gives_facts_and_bodies_from_recorded_storage_only(self):
        """Core builds cursors, turns and pages from the view and asks for bodies by index; fragile because the facts it
        can't derive itself (a command's name and args, a result's call, the context start as a history index) must be
        right, and storage must come from the agent's record or its own session, never from the reading process."""
        rows = [{'type': 'user', 'uuid': 'u1', 'promptId': 'p1', 'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': 'Go.'}},
                {'type': 'user', 'uuid': 'z1', 'parentUuid': 'u1', 'promptId': 'pz', 'origin': {'kind': 'human'},
                 'message': {'role': 'user', 'content': 'A branch a rewind left behind'}},
                {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1', 'message': {'id': 'm1', 'stop_reason': 'tool_use',
                 'content': [{'type': 'tool_use', 'id': 'c1'}], 'usage': {'output_tokens': 3}}},
                {'type': 'user', 'uuid': 'r1', 'parentUuid': 'a1', 'promptId': 'p1', 'toolUseResult': {},
                 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c1'}]}},
                {'type': 'user', 'uuid': 'k1', 'parentUuid': 'r1', 'promptId': 'p2', 'message': {'role': 'user', 'content': '/compact keep the tests'}},
                {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'b1', 'logicalParentUuid': 'r1'},
                {'type': 'user', 'uuid': 'u2', 'parentUuid': 'b1', 'promptId': 'p3', 'origin': {'kind': 'plugin'},
                 'message': {'role': 'user', 'content': 'x' * 300}},
                ['not', 'an', 'object']]
        self.write(rows)
        view = self.adapter().transcript_view(self.session())
        self.assertEqual((view.source.state, view.source.home, view.source.format), ('supported', str(self.storage), 'claude-jsonl'))
        facts = {fact.ref: fact for fact in view.rows}
        self.assertEqual((facts['k1'].label, facts['k1'].command, facts['k1'].args), ('command', '/compact', 'keep the tests'))
        self.assertEqual((facts['r1'].call_id, facts['a1'].stop, facts['u1'].preview), ('c1', 'tool_use', 'Go.'))
        self.assertEqual(len(facts['u2'].preview), 200)
        self.assertEqual(view.rows[-1].kind, 'unreadable')
        history = [view.rows[i].ref for i in view.history]
        self.assertEqual(history, ['u1', 'a1', 'r1', 'k1', 'b1', 'u2', None])  # the unreadable row follows u2
        self.assertEqual(history[view.context_from], 'b1')  # an index into the history, at the latest boundary
        bodies = self.adapter().transcript_bodies(self.session(), view, [0, 6, 7], max_bytes=200)
        self.assertEqual((bodies[0]['uuid'], bodies[1], bodies[2]), ('u1', None, None))  # over the cap; not an object
        again = self.adapter().transcript_bodies(self.session(), view, [0], max_bytes=1 << 20)[0]
        again['uuid'] = 'changed'
        self.assertEqual(self.adapter().transcript_bodies(self.session(), view, [0], max_bytes=1 << 20)[0]['uuid'], 'u1')
        with self.subTest('storage'):
            from dataclasses import replace
            unrecorded = replace(self.session(), home=None)
            outside = self.adapter().transcript_view(unrecorded).source  # never this process's own storage
            self.assertEqual((outside.state, outside.reason), ('unavailable', NO_STORAGE))
            agent = Path(unrecorded.session_file).parent
            with patch.dict(os.environ, {'MV2_AGENT_DIR': str(agent), 'CLAUDE_CONFIG_DIR': 'claude-config'}):
                own = self.adapter().transcript_view(unrecorded)  # inside its own session: the launch's environment
            self.assertEqual((own.source.state, own.source.home), ('supported', os.path.realpath(self.storage)))
            self.assertIs(self.adapter().transcript_readable(self.session()), True)
            self.assertIs(self.adapter().transcript_readable(replace(self.session(), native_id=str(uuid4()))), False)
        with self.subTest("core's check runs between the steps of a read"):
            class Expired(Exception):
                pass
            class Reader:
                deadline, calls = 0.0, 0
                def check(self):
                    self.calls += 1
                    if self.calls > 1:
                        raise Expired()
            with self.assertRaises(Expired):  # the first check passes; the one after indexing stops the read
                self.adapter().transcript_view(self.session(), reader=Reader())
