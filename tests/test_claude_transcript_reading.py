"""Reading a Claude session's transcript: labels, history, positions and copies (harnesses/claude/transcripts.py)."""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses import claude
from multiverse.harnesses.claude import NO_STORAGE, transcripts as t
from multiverse.harnesses.types import SessionHandle, TranscriptRowsChanged
from multiverse.runtime import prompt_events, transcripts as core
from multiverse.store import read, write
from multiverse.tools import runtime_update
from multiverse.universe import agents
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
        t._usage.clear()
        self.addCleanup(t._transcripts.clear)
        self.addCleanup(t._usage.clear)

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

    def test_a_read_past_its_deadline_starts_no_further_step_keeps_nothing_and_its_retry_reads_it_all(self):
        """A turn event's read has 0.5 s and a cold index of a large transcript takes tens of seconds: one deadline must
        stop every step that can block or grow, wherever the time runs out, and leave nothing partial behind, or a
        report overruns its 10 s kill or a later read trusts half a transcript."""
        import contextlib

        class Cut(Exception):
            pass

        rows, parent = [], None
        for n in range(600):
            kind = 'user' if n % 2 == 0 else 'assistant'
            rows.append({'type': kind, 'uuid': f'00000000-0000-4000-8000-{n:012d}', 'parentUuid': parent,
                         'sessionId': self.native, 'message': {'role': kind, 'content': f'row {n}'}})
            parent = rows[-1]['uuid']
        self.write(rows)
        other = self.project.with_name(self.project.name + '-other')  # two project folders: the scan loops
        other.mkdir()
        self.addCleanup(other.rmdir)
        adapter = self.adapter()
        t._transcripts.clear()
        fresh = adapter.transcript_view(self.session())
        clock, log = [0.0], []
        state = {'slow': None, 'when': None}  # the step made slow, and on which call

        class Reader:  # core's reader: one deadline, which the fake clock passes when the slow step runs
            deadline = 1.0

            def check(self):
                if clock[0] >= self.deadline:
                    raise Cut()

        def timed(name, call):  # a step: logs when it starts; the slow one takes the rest of the budget
            def run(*args, **kwargs):
                log.append((name, clock[0]))
                calls = sum(1 for each, _ in log if each == name)  # this call's number
                late = name == state['slow'] and clock[0] < Reader.deadline
                if late and state['when'] == 'start' and calls == 1:
                    clock[0] = 2.0
                result = call(*args, **kwargs)
                if late and state['when'] == calls:
                    clock[0] = 2.0  # at the end of that call: it finished, and nothing after it may start
                return result
            return run

        real_scandir, real_open = os.scandir, t.open_regular

        def listing(path):
            opened = timed('opening the listing', real_scandir)(path)

            class Taken:
                def __iter__(self):
                    for item in opened:
                        timed('taking an entry', lambda: None)()
                        yield item

                def close(self):
                    opened.close()
            return Taken()

        @contextlib.contextmanager
        def opening(path):
            with timed('opening the transcript', real_open)(path) as handle:
                class Reading:
                    def __getattr__(self, name):
                        return getattr(handle, name)

                    def read(self, *args):
                        return timed('reading a chunk', handle.read)(*args)
                yield Reading()

        steps = {'finding the project keys': (t, 'project_keys'), 'opening the listing': None, 'taking an entry': None,
                 'checking a project folder': (t, 'holds_name'), 'opening the transcript': None,
                 'reading a chunk': None, 'parsing a row': (t, 'entry'), 'labelling': (t, 'labels'),
                 'the history walk': (t, 'walk'), 'the history membership': (t, '_on_history'),
                 'the context': (t, 'in_context'), 'the history refs': (t, '_history_refs'),
                 "the view's start of context": (t, 'context_from'), 'a view fact': (claude, '_fact')}
        analysis_steps = set(list(steps)[:steps.__len__() - 2])

        def read(reader, bodies=None):
            t._transcripts.clear()
            clock[0], log[:] = 0.0, []
            patches = [patch.object(claude, 'time', SimpleNamespace(monotonic=lambda: clock[0])),
                       patch.object(t.os, 'scandir', listing), patch.object(t, 'open_regular', opening)]
            patches += [patch.object(module, name, timed(step, getattr(module, name)))
                        for step, (module, name) in ((s, w) for s, w in steps.items() if w)]
            with contextlib.ExitStack() as stack:
                for each in patches:
                    stack.enter_context(each)
                if bodies is not None:  # the body read: its own index refresh, then the body file
                    return adapter.transcript_bodies(self.session(), fresh, bodies, max_bytes=1 << 20, reader=reader)
                return adapter.transcript_view(self.session(), reader=reader)

        def boundaries():
            # Every boundary between two steps in a whole read: each distinct pair of steps one after the other,
            # at its first occurrence, as (the earlier step, its call number then). The time runs out at that end.
            seen, calls, out = set(), {}, [(name, 'start') for name in dict.fromkeys(name for name, _ in log)]
            for (before, _), (after, _) in zip(log, log[1:]):
                calls[before] = calls.get(before, 0) + 1
                if (before, after) not in seen:
                    seen.add((before, after))
                    out.append((before, calls[before]))
            return out

        read(None)  # a dry run: the order the steps run in
        self.assertEqual({name for name, _ in log}, set(steps))  # every step runs in a whole read
        for slow, when in boundaries():
            with self.subTest(slow=slow, when=when):
                state.update(slow=slow, when=when)
                with self.assertRaises(Cut):
                    read(Reader())
                state.update(slow=None)
                self.assertIn(slow, [name for name, _ in log])
                self.assertEqual([name for name, at in log if at >= Reader.deadline], [])  # nothing started after
                kept = t._transcripts.get(self.path)
                if kept is not None:  # no view, no part of a read, and an analysis only if it finished in time
                    self.assertEqual(kept.views, {})
                    self.assertIn(kept.offset, (0, self.path.stat().st_size))
                    if slow in analysis_steps:
                        self.assertIsNone(getattr(kept, 'analysis', None))
                view = read(None)
                self.assertEqual((view.rows, view.history), (fresh.rows, fresh.history))

        everything = list(range(len(fresh.rows)))
        read(None, everything)
        for slow, when in boundaries():
            with self.subTest(bodies=slow, when=when):
                # A body read keeps nothing, so one whose last step ends past the deadline may return; but no
                # step starts after it.
                state.update(slow=slow, when=when)
                try:
                    read(Reader(), everything)
                except Cut:
                    pass
                state.update(slow=None)
                self.assertEqual([name for name, at in log if at >= Reader.deadline], [])
                self.assertEqual(len(read(None, everything)), len(fresh.rows))

        with self.subTest(case='a read past its deadline never waits on another read holding the index lock'):
            import threading
            t._transcripts.clear()
            clock[0], outcome = 0.0, []

            def waiting():
                try:
                    with patch.object(claude, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
                        adapter.transcript_view(self.session(), reader=Reader())
                except Cut:
                    outcome.append('cut')
            with t._transcripts_lock:
                worker = threading.Thread(target=waiting)
                worker.start()
                time.sleep(.1)  # it reaches the lock and waits
                clock[0] = 2.0  # the deadline passes while it waits
                worker.join(5)
                self.assertFalse(worker.is_alive())  # it gave up without the lock being released
            self.assertEqual(outcome, ['cut'])

        with self.subTest(case='a read cut off after half was read keeps exactly that half'):
            class Halfway:
                calls = 0
                deadline = 0

                def check(self):
                    Halfway.calls += 1
                    if Halfway.calls > 40:  # inside the second half's rows
                        raise Cut()
            t._transcripts.clear()
            self.write(rows[:300])
            index = t._index(self.path, rows=True).read(prove=True)
            half = (len(index.entries), index.offset, index.prefix.hexdigest(), index.chain)
            self.write(rows)
            with self.assertRaises(Cut):
                index.read(check=Halfway().check)
            self.assertEqual((len(index.entries), index.offset, index.prefix.hexdigest(), index.chain), half)
            self.assertEqual([e.ref for e in index.read().entries], [e.ref for e in t.Transcript(self.path, True).read().entries])

        with self.subTest(case='a deadline that is an OSError, raised mid-discovery, comes out as itself'):
            class Expiring:
                calls = 0
                deadline = 0

                def check(self):
                    Expiring.calls += 1
                    if Expiring.calls > 1:  # the first check is transcript_view's own, at its start
                        raise TimeoutError('deadline')
            t._transcripts.clear()
            with self.assertRaises(TimeoutError):
                adapter.transcript_view(self.session(), reader=Expiring())

    def test_fresh_task_start_waits_for_its_transcript_row(self):
        """The launch-task start can arrive before the transcript exists or its first row can be read."""
        agent = self.root / 'state' / 'agents' / str(uuid4())
        launch, row_ref = str(uuid4()), str(uuid4())
        write(agent / 'properties.json', {'id': agent.name, 'name': 'worker', 'harness': 'claude',
              'workspace': str(self.work), 'native_session_id': self.native, 'native_session_home': str(self.storage)})
        write(agent / 'runtime.json', {'launch_id': launch, 'state': 'starting'})
        write(agent / 'launches' / launch / 'request.json', {'task_launch': True})
        edge = {'type': 'started', 'turn': row_ref, 'ref': row_ref, 'kind': 'prompt',
                'source': 'person', 'first_task': True}
        start = time.monotonic()
        elapsed = 0.0

        def make_row_visible(delay):
            nonlocal elapsed
            self.assertEqual(read(agent / 'runtime.json')['status'], 'working')
            status.assert_called_once_with(agent, 'agent.status', launch, {'from': None, 'to': 'working'})
            elapsed += delay
            if self.path.exists():
                self.write([{'type': 'user', 'uuid': row_ref, 'sessionId': self.native,
                             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': 'Launch task'}}])
            else:
                self.path.touch()

        with patch.object(core, 'time') as clock, patch.object(agents, '_event') as status, \
                patch.object(prompt_events, '_journal_event', return_value='retry'), \
                patch.object(runtime_update, 'callback', return_value=(self.root, agent, launch)):
            clock.monotonic.side_effect = lambda: start + elapsed
            clock.sleep.side_effect = make_row_visible
            runtime_update.run(status='working', turn_session_id=self.native, turn_events_json=json.dumps([edge]))

        [event] = read(agent / 'prompts' / 'journal' / 'events.json')['pending']
        self.assertEqual(event['kind'], 'turn.started')
        self.assertEqual((event['data']['source'], event['data']['message'], event['data']['first_task']),
                         ('mv2', None, True))
        self.assertEqual(clock.sleep.call_count, 2)
        page = core.rows(self.root, agent, bodies=False)
        [row] = page.rows
        self.assertEqual((event['data']['turn'], event['data']['ref'], event['data']['generation']),
                         (row.turn, row.cursor, page.generation))

    def test_each_row_is_labelled_by_claudes_own_fields(self):
        """Labels decide what a plugin counts as a prompt or a turn's end; fragile because Claude marks them only by
        loose fields (origin, promptId, attachment types, a compaction's span, a parent link), and a wrong rule turns a
        compaction's hundreds of stored rows into mid-turn messages, an interrupted turn into one that never ends, or a
        local command's report into the turn before it."""
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
            ('a local command', {'type': 'system', 'subtype': 'local_command', 'uuid': 'l1', 'parentUuid': None,
                                 'content': '<command-name>/context</command-name>'}, ('command', None)),
            ('its stdout', {'type': 'system', 'subtype': 'local_command', 'uuid': 'l2', 'parentUuid': 'l1',
                            'content': '<local-command-stdout>Context usage</local-command-stdout>'}, ('command_output', None)),
            # /context's report for the model: a meta row under the command's output, so it is the command's too.
            ('its report', {'type': 'user', 'uuid': 'l3', 'parentUuid': 'l2', 'promptId': 'p4', 'isMeta': True,
                            'message': {'role': 'user', 'content': '## Context Usage'}}, ('command_output', None)),
            ('a meta row under anything else', {'type': 'user', 'uuid': 'l4', 'parentUuid': 'u3', 'promptId': 'p3', 'isMeta': True,
                                                'message': {'role': 'user', 'content': 'A reminder'}}, ('injected', None)),
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
        typed /compact row, and a gap behind Claude's own compaction must start at its summary, not be reported: its
        summary may sit under the turn's attachment rows, and an automatic compaction's boundary links forward."""
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
        # Claude's own compaction (GitHub issue #40): its summary is under the boundary, or under the attachment
        # rows a compaction run as a turn starts stores between them (seen on 2.1.319 and 2.1.322), never under a
        # message row; an automatic compaction's boundary links to a row stored after its summary, which a read can
        # catch as the newest row (the reviewer's prefix of a real 2.1.322 transcript).
        boundary = lambda logical: {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'b2', 'logicalParentUuid': logical,
                                    'compactMetadata': {'preservedMessages': {'uuids': ['kept']}}}
        attach = lambda uuid, parent, kind: {'type': 'attachment', 'uuid': uuid, 'parentUuid': parent, 'attachment': {'type': kind}}
        between = [attach('i1', 'b2', 'instructions'), attach('i2', 'i1', 'session_context'), attach('i3', 'i2', 'date')]
        summary = lambda parent: user('summary', parent, 'Summary of earlier work', isCompactSummary=True)
        later = lambda parent: user('later', parent, 'Go on', origin={'kind': 'human'}, promptId='p9')
        cases = [  # (case, rows, the history's refs, gap, summary)
            ('a gap behind it, the summary under the boundary',
             [user('kept', 'missing', 'Kept verbatim'), boundary('kept'), summary('b2'), later('summary')],
             ['b2', 'summary', 'kept', 'later'], None, 'summary'),
            ('a gap behind it, attachment rows between',
             [user('kept', 'missing', 'Kept verbatim'), boundary('kept'), *between, summary('i3'), later('summary')],
             ['b2', 'i1', 'i2', 'i3', 'summary', 'kept', 'later'], None, 'summary'),
            ('no gap: the boundary links to a row after its summary',
             [user('start', None, 'Read it', origin={'kind': 'human'}, promptId='p1'), user('kept', 'start', 'Kept verbatim'),
              boundary('tail'), *between, summary('i3'), attach('tail', 'summary', 'total_tokens_reminder'), later('tail')],
             ['b2', 'i1', 'i2', 'i3', 'summary', 'kept', 'tail', 'later'], None, 'summary'),
            ('read before a row hangs off the one the boundary links to: still the newest',
             [user('start', None, 'Read it', origin={'kind': 'human'}, promptId='p1'), user('kept', 'start', 'Kept verbatim'),
              boundary('tail'), *between, summary('i3'), attach('tail', 'summary', 'total_tokens_reminder')],
             ['b2', 'i1', 'i2', 'i3', 'summary', 'kept', 'tail'], None, 'summary'),
            ('no gap: the history goes on across it',
             [user('start', None, 'Read it', origin={'kind': 'human'}, promptId='p1'), boundary('start'), *between,
              summary('i3'), later('summary')],
             ['start', 'b2', 'i1', 'i2', 'i3', 'summary', 'later'], None, None),
            ('a message row between: not its summary, the gap is reported',
             [user('kept', 'missing', 'Kept verbatim'), boundary('kept'),
              user('typed', 'b2', 'Something else', origin={'kind': 'human'}, promptId='p2'), summary('typed'), later('summary')],
             ['kept', 'b2', 'typed', 'summary', 'later'], 'missing', None)]
        for case, rows, refs, gap, named in cases:
            with self.subTest(case):
                self.write(rows)
                index, analysis = self.read()
                self.assertEqual([index.entries[i].ref for i in analysis.history.rows], refs)
                self.assertEqual((analysis.history.gap, analysis.history.summary), (gap, named))
        # An idle or automatic compaction keeps the end of the last turn (preservedMessages, Claude 2.1.323): the kept
        # rows keep their old parents before the boundary, and the live session holds them after Claude's summary.
        # GOAT reads from the live session's last message, the kept reply; read from its old parents, the boundary
        # and the summary were missing, and GOAT refused every tree compaction of the session (compaction, 2026-11-13).
        keeps = lambda uuid, logical, kept: {'type': 'system', 'subtype': 'compact_boundary', 'uuid': uuid,
                                             'logicalParentUuid': logical,
                                             'compactMetadata': {'preservedMessages': {'uuids': kept, 'allUuids': kept}}}
        note = lambda uuid, parent: user(uuid, parent, 'Summary of earlier work', isCompactSummary=True)
        start = [user('start', None, 'Read it', origin={'kind': 'human'}, promptId='p1'),
                 call('k1', 'start', [], 'm1'), call('k2', 'k1', [], 'm2')]
        idle = [*start, keeps('b4', 'k2', ['k1', 'k2']), note('s4', 'b4'), attach('i4', 's4', 'date'),
                user('rewound', 'i4', 'A turn the live session no longer holds', origin={'kind': 'human'}, promptId='p2')]
        tipped = [  # (case, rows, tip, the history's refs)
            ('a kept tip reads from the summary: the boundary, the summary and the kept rows, nothing stored after',
             idle, 'k2', ['start', 'b4', 's4', 'k1', 'k2']),
            ('the newest read has the kept rows right after the summary too',
             idle, None, ['start', 'b4', 's4', 'k1', 'k2', 'i4', 'rewound']),
            ('kept by two compactions in a row: the later one is the live one',
             [*start, keeps('b4', 'k2', ['k1', 'k2']), note('s4', 'b4'), keeps('b5', 'k2', ['k1', 'k2']), note('s5', 'b5')],
             'k2', ['start', 'b5', 's5', 'k1', 'k2']),
            # A response's other block and a parallel result join after the walk; they must follow the summary too.
            ('kept blocks and parallel results the walk adds later come after the summary too',
             [user('start', None, 'Read it', origin={'kind': 'human'}, promptId='p1'), call('a1', 'start', ['c1'], 'm1'),
              call('a2', 'a1', ['c2'], 'm1'), result('r1', 'a2', 'c1'), result('r2', 'a1', 'c2'), call('k2', 'r2', [], 'm2'),
              keeps('b4', 'k2', ['a1', 'a2', 'r1', 'r2', 'k2']), note('s4', 'b4')],
             'k2', ['start', 'b4', 's4', 'a1', 'a2', 'r1', 'r2', 'k2']),
            ('a row two walked compactions kept follows the later summary',
             [*start[:2], keeps('b4', 'k1', ['k1']), note('s4', 'b4'), user('meta', 's4', 'Context', isMeta=True),
              keeps('b5', 'meta', ['k1']), note('s5', 'b5')],
             'k1', ['start', 'b4', 's4', 'meta', 'b5', 's5', 'k1']),
            ('a kept row off the old parent chain is on the history too: the live session holds it',
             [user('start', None, 'Read it', origin={'kind': 'human'}, promptId='p1'), call('k1', 'start', [], 'm1'),
              call('k2', 'start', [], 'm2'), keeps('b4', 'k2', ['k1', 'k2']), note('s4', 'b4')],
             'k2', ['start', 'b4', 's4', 'k1', 'k2']),
            ('a boundary without Claude\'s summary leaves the tip read as before',
             [*start, keeps('b4', 'k2', ['k1', 'k2']), user('s4', 'b4', 'Not a summary', origin={'kind': 'human'}, promptId='p3')],
             'k2', ['start', 'k1', 'k2'])]
        for case, rows, tip, refs in tipped:
            with self.subTest(case):
                self.write(rows)
                index, analysis = self.read(tip=tip)
                self.assertEqual([index.entries[i].ref for i in analysis.history.rows], refs)
                self.assertEqual((analysis.history.tip, analysis.history.gap), (tip or refs[-1], None))
        with self.subTest('a compaction that puts rows before ones already read raises the history version'):
            # Its boundary and summary come before the rows it kept: a reader taking the history as grown at its end
            # would skip them.
            self.write(start)
            _, before = self.read()
            with self.path.open('a') as handle:
                handle.write(''.join(json.dumps(row) + '\n' for row in (keeps('b4', 'k2', ['k1', 'k2']), note('s4', 'b4'))))
            index, after = self.read()
            self.assertEqual([index.entries[i].ref for i in after.history.rows], ['start', 'b4', 's4', 'k1', 'k2'])
            self.assertGreater(after.version, before.version)
            with self.path.open('a') as handle:
                handle.write(json.dumps(user('next', 's4', 'Go on', origin={'kind': 'human'}, promptId='p5')) + '\n')  # Claude chains on from the summary
            _, grown = self.read()
            self.assertEqual(grown.version, after.version)  # growing at the end keeps it
        with self.subTest("the newest leaf is the last stored, though an install's rows carry older timestamps"):
            # Read right after a tree compaction installed its rows, before Claude stores the next one: Claude's own
            # note from just before the boundary is a dead branch with a newer timestamp.
            self.write([user('start', None, 'Start', origin={'kind': 'human'}, promptId='p1', timestamp='2026-11-11T10:00:00Z'),
                        {'type': 'system', 'subtype': 'informational', 'uuid': 'note', 'parentUuid': 'start',
                         'timestamp': '2026-11-11T10:00:05Z'},
                        {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'b3', 'logicalParentUuid': 'start',
                         'timestamp': '2026-11-11T10:00:05Z'},
                        user('installed', 'b3', 'Installed turn', timestamp='2026-11-11T10:00:04Z')])
            index, analysis = self.read()
            self.assertEqual([index.entries[i].ref for i in analysis.history.rows], ['start', 'b3', 'installed'])
        with self.subTest('a gap with no compaction is reported'):
            self.write([user('orphan', 'never-stored', 'After a failed write', origin={'kind': 'human'}, promptId='p1')])
            _, analysis = self.read()
            self.assertEqual(analysis.history.gap, 'never-stored')

    def test_pages_of_an_unchanged_transcript_share_one_view_and_check_only_the_deadline(self):
        """A read takes many pages of one transcript; fragile because a view kept across pages must never outlive a
        change to the file, and checking only the deadline inside a page must still stop a page that ran out of time."""
        user = lambda uuid, parent: {'type': 'user', 'uuid': uuid, 'parentUuid': parent,
                                     'message': {'role': 'user', 'content': 'x' * 40}}
        self.write([user(f'u{n}', f'u{n - 1}' if n else None) for n in range(20)])
        adapter, session = self.adapter(), self.session()
        view = adapter.transcript_view(session)
        self.assertIs(adapter.transcript_view(session), view)  # an unchanged transcript: the same view
        self.assertIsNot(adapter.transcript_view(session, tip='u5'), view)  # another tip: a view of its own
        with open(self.path, 'ab') as handle:
            handle.write((json.dumps(user('u20', 'u19')) + '\n').encode())
        grown = adapter.transcript_view(session)
        self.assertEqual((len(grown.rows), grown.tip), (21, 'u20'))
        text = self.path.read_bytes()
        self.path.write_bytes(text.replace(b'"u3"', b'"v3"', 1))  # same length, before the end: Claude rewrote it
        os.utime(self.path, ns=(self.path.stat().st_atime_ns, self.path.stat().st_mtime_ns + 1000))
        rewritten = adapter.transcript_view(session)
        self.assertIsNot(rewritten, grown)
        self.assertEqual(rewritten.rows[3].ref, 'v3')

        class Reader:
            def __init__(self, deadline):
                self.deadline, self.checks = deadline, 0

            def check(self):
                self.checks += 1
                if time.monotonic() >= self.deadline:
                    raise TimeoutError('deadline')
        reader = Reader(time.monotonic() + 60)
        adapter.transcript_bodies(session, rewritten, list(range(21)), max_bytes=1 << 20, reader=reader)
        self.assertEqual(reader.checks, 1)  # once before the page, not once per row
        clock = [0.0]

        def tick():  # time passes as the page reads its rows: the deadline runs out partway
            clock[0] += 1
            return clock[0]

        late = Reader(5)
        with patch.object(time, 'monotonic', tick), self.assertRaises(TimeoutError):
            adapter.transcript_bodies(session, rewritten, list(range(21)), max_bytes=1 << 20, reader=late)
        self.assertLess(clock[0], 21)  # stopped inside the page, not after it

    def test_a_position_finds_a_rewrite_and_usage_starts_over(self):
        """A cursor must notice when Claude rewrites the file in place; fragile because truncating at a tombstoned
        row keeps the inode, and a file that grows back past the old offset would otherwise read shifted rows as new,
        and usage would keep a removed response's tokens (reset on a same-inode rewrite from this commit on)."""
        response = lambda uuid, mid, tokens: {'type': 'assistant', 'uuid': uuid, 'message': {
            'id': mid, 'usage': {'input_tokens': tokens}}}
        first = [response('a', 'm1', 10), response('b', 'm2', 20), response('c', 'm3', 30)]
        self.write(first)
        index, analysis = self.read()
        before, version = [t.position_of(item) for item in index.entries], analysis.version
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
        self.assertGreater(analysis.version, version)  # the history changed other than by growing
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
        """Usage reads the shared index on every listing; fragile because proving the whole prefix on every change
        there costs a full read and goes unnoticed, while an earlier same-length edit must still be seen: by usage
        when the change added no bytes or a minute has passed, and at once by a transcript read."""
        # The index's clock is the test's, in whole seconds: a test step never becomes due by itself on a slow
        # machine, and `proven_at + PROOF_INTERVAL` is exact (on the real clock, just below a power of two, the sum
        # rounds and comes out a hair short of due).
        clock = [1000.0]
        patcher = patch.object(t, 'monotonic', lambda: clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        rows = [{'type': 'assistant', 'uuid': f'u{n}', 'message': {'id': f'm{n}', 'usage': {'input_tokens': n},
                 'content': 'x' * 500}} for n in range(400)]  # well over TAIL
        self.write(rows)
        index = t._transcripts[self.path] = t.Transcript(self.path).read()  # usage and the reads share this proof
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
        with self.subTest('a change that adds no bytes is proved in full, even by usage'):
            self.read()
            text = self.path.read_bytes()
            changed = text.replace(b'"input_tokens": 3}', b'"input_tokens": 4}', 1)  # same length, before the tail
            self.assertEqual((len(changed), changed != text), (len(text), True))
            self.path.write_bytes(changed)
            os.utime(self.path, ns=(self.path.stat().st_atime_ns, self.path.stat().st_mtime_ns + 2000))
            self.assertEqual((index.read().rewrites, index.responses['m3']['input_tokens']), (2, 4))
        with self.subTest('a growing file: a transcript read proves it at once, usage within a minute'):
            self.read()
            text = self.path.read_bytes()
            self.path.write_bytes(text.replace(b'"input_tokens": 5}', b'"input_tokens": 6}', 1)
                                  + (json.dumps(rows[0] | {'uuid': 'grown'}) + '\n').encode())
            index.read()  # the edit is before the tail, and the file grew: the tail check alone, for now
            self.assertEqual(index.rewrites, 2)
            self.assertEqual(self.read()[0].rewrites, 3)  # the transcript read, through newest_copy
            text = self.path.read_bytes()
            self.path.write_bytes(text.replace(b'"input_tokens": 7}', b'"input_tokens": 8}', 1)
                                  + (json.dumps(rows[0] | {'uuid': 'grown again'}) + '\n').encode())
            clock[0] = index.proven_at + t.PROOF_INTERVAL
            self.assertEqual((index.read().rewrites, index.responses['m7']['input_tokens']), (4, 8))
        with self.subTest('a file that went quiet after a tail-only read is still proved once due'):
            text = self.path.read_bytes()
            self.path.write_bytes(text.replace(b'"input_tokens": 9}', b'"input_tokens": 1}', 1)
                                  + (json.dumps(rows[0] | {'uuid': 'last'}) + '\n').encode())
            index.read()  # tail only, and then the file stops changing
            self.assertEqual(index.responses['m9']['input_tokens'], 9)
            clock[0] = index.proven_at + 2 * t.PROOF_INTERVAL
            self.assertEqual((index.read().rewrites, index.responses['m9']['input_tokens']), (5, 1))
        with self.subTest('a fully proved, unchanged file still costs no read'):
            clock[0] = index.proven_at + 10 * t.PROOF_INTERVAL
            with patch.object(t.Transcript, '_intact', side_effect=AssertionError('proved an unchanged file')):
                index.read()

    def test_a_damaged_field_never_breaks_usage_or_the_index(self):
        """Usage reads every row through the shared index; fragile because one malformed field on an unrelated row
        made the whole read fail, and the half-updated index then counted rows twice once the file was fixed."""
        response = lambda uuid, mid, tokens: {'type': 'assistant', 'uuid': uuid, 'message': {
            'id': mid, 'usage': {'input_tokens': tokens}}}
        boundary = {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'b'}
        for name, damaged in [('a list where a map belongs', boundary | {'compactMetadata': {'preservedMessages': ['x']}}),
                              ('a string there', boundary | {'compactMetadata': {'preservedMessages': 'x'}}),
                              ('compactMetadata a list', boundary | {'compactMetadata': ['x']})]:
            with self.subTest(name):
                self.write([response('a', 'm1', 15), damaged, response('c', 'm2', 5)])
                index = t.Transcript(self.path).read()
                self.assertEqual({key: value['input_tokens'] for key, value in index.responses.items()}, {'m1': 15, 'm2': 5})
                self.assertEqual((len(index.entries), index.entries[1].kind, index.entries[1].preserved),
                                 (3, 'system/compact_boundary', ()))  # still a boundary, with nothing preserved
        with self.subTest('any other shape the parser does not expect makes that row unreadable'):
            self.write([response('a', 'm1', 15), boundary, response('c', 'm2', 5)])
            real_fields = t._fields
            odd = lambda offset, line, digest, chain, row: (real_fields(offset, line, digest, chain, row)
                                                           if row.get('uuid') != 'b' else row['missing'])
            with patch.object(t, '_fields', odd):
                index = t.Transcript(self.path).read()
            self.assertEqual(([item.kind for item in index.entries], sorted(index.responses)),
                             (['assistant', 'unreadable', 'assistant'], ['m1', 'm2']))
        with self.subTest('a parse that fails midway commits nothing, so a reread never counts a row twice'):
            self.write([response('a', 'm1', 15), response('c', 'm2', 5)])
            index, real, calls = t.Transcript(self.path), t.entry, []

            def flaky(*args):
                calls.append(args)
                if len(calls) == 2:
                    raise RuntimeError('unexpected')
                return real(*args)
            with patch.object(t, 'entry', flaky), self.assertRaises(RuntimeError):
                index.read()
            self.assertEqual((index.offset, index.entries, index.responses), (0, [], {}))
            offsets = [item.offset for item in index.read().entries]
            self.assertEqual((len(offsets), len(set(offsets))), (2, 2))

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
