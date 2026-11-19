"""Claude usage from its transcripts and the mod's reports."""
import json
import os
from unittest.mock import patch
import unittest
from uuid import uuid4

from multiverse.harnesses.types import SessionHandle
from multiverse.store import read, write
from multiverse.harnesses.claude import measured_usage
from multiverse import harnesses
from test_multiverse import BaseTest


ABSENT = object()


class ClaudeUsageTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.config = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))

    def response(self, id, input_tokens, output_tokens, cache_read=0, cache_write=0, block='text'):
        usage = {'input_tokens': input_tokens, 'output_tokens': output_tokens,
                 'cache_read_input_tokens': cache_read, 'cache_creation_input_tokens': cache_write}
        return json.dumps({'type': 'assistant', 'timestamp': f'2026-09-30T00:00:0{len(id)}Z',
                           'message': {'id': id, 'usage': usage, 'content': [{'type': block}]}}) + '\n'

    def agent(self):
        agent = str(uuid4())
        directory = self.root / 'state/agents' / agent
        write(directory / 'properties.json', {'id': agent, 'name': 'worker', 'harness': 'claude'})
        write(directory / 'runtime.json', {'launch_id': 'current', 'state': 'running', 'status': 'idle'})
        return agent, directory

    def transcript(self, native_id, *records):
        project = self.config / 'projects' / '-work-project'
        (project / native_id / 'subagents').mkdir(parents=True, exist_ok=True)
        (project / f'{native_id}.jsonl').write_text(''.join(records))
        return project

    def response_with(self, id, input_tokens, read, written):
        usage = {'input_tokens': input_tokens, 'output_tokens': 7, 'cache_read_input_tokens': read,
                 'cache_creation_input_tokens': written}
        return json.dumps({'type': 'assistant', 'message': {'id': id, 'usage': usage, 'content': [{'type': 'text'}]}}) + '\n'

    def test_transcript_usage_counts_each_response_once(self):
        """Claude transcript usage counts each response once though it is stored once per content block, keeps
        subagents out of context, waits on partial lines, never takes a zero-usage row as latest, and reads the launch's
        own storage; a break goes unnoticed because a double-counted number, or another storage's, still looks plausible
        (bb0ec1a, LC-26)."""
        with self.subTest('a response stored once per content block, and subagents'):
            adapter = harnesses.ClaudeAdapter.configured('claude', {'inbox_bridge': False})
            native_id = str(uuid4())
            project = self.config / 'projects' / '-work-project'
            project.mkdir(parents=True, exist_ok=True)
            main = project / f'{native_id}.jsonl'
            # One response is recorded once per content block, each with the same usage.
            main.write_text(json.dumps({'type': 'user', 'message': {'content': 'hi'}}) + '\n'
                            + self.response('msg_1', 10, 5, cache_read=100, cache_write=20, block='thinking')
                            + self.response('msg_1', 10, 5, cache_read=100, cache_write=20) + 'not json\n')
            session = SessionHandle('agent', 'claude', str(self.work), native_id=native_id)
            result = adapter.get_usage(session)
            self.assertEqual(result.state, 'ok')
            self.assertEqual({key: result.value[key] for key in ('input_tokens', 'cached_input_tokens', 'cache_write_tokens',
                                                                 'output_tokens', 'total_tokens')},
                             {'input_tokens': 130, 'cached_input_tokens': 100, 'cache_write_tokens': 20,
                              'output_tokens': 5, 'total_tokens': 135})
            self.assertEqual(adapter.capabilities(session)['get_usage'].state, 'supported')
            # Growth is read incrementally; an unfinished line waits for its newline.
            partial = self.response('msg_2', 1, 2)
            with main.open('a') as handle:
                handle.write(partial[:20])
            self.assertEqual(measured_usage(native_id).total_tokens, 135)
            with main.open('a') as handle:
                handle.write(partial[20:])
            subagents = project / native_id / 'subagents'
            subagents.mkdir(parents=True)
            (subagents / 'agent-a.jsonl').write_text(self.response('msg_3', 40, 60))
            self.assertEqual(measured_usage(native_id).total_tokens, 135 + 3 + 100)
            self.assertEqual(adapter.get_usage(SessionHandle('agent', 'claude', str(self.work), native_id=str(uuid4()))).state,
                             'unavailable')
            self.assertEqual(adapter.get_usage(SessionHandle('agent', 'claude', str(self.work))).state, 'unavailable')
        with self.subTest("the launch's own storage, not this process's"):
            # LC-26: the dashboard or a CLI reading usage may run with another CLAUDE_CONFIG_DIR than the launch had;
            # session.home is the launch's own, pinned before it started.
            adapter = harnesses.ClaudeAdapter.configured('claude', {'inbox_bridge': False})
            native_id = str(uuid4())
            launch_home = self.work / 'launch-claude'
            project = launch_home / 'projects' / '-work-project'
            project.mkdir(parents=True)
            (project / f'{native_id}.jsonl').write_text(self.response_with('msg_9', 7, 3, 2))
            session = SessionHandle('agent', 'claude', str(self.work), native_id=native_id, home=str(launch_home))
            result = adapter.get_usage(session)  # this process's CLAUDE_CONFIG_DIR has no such transcript
            self.assertEqual((result.state, result.value.get('total_tokens') if result.value else None,
                              (result.value or {}).get('context', {}).get('tokens')), ('ok', 12 + 7, 12))
        with self.subTest('context is the last main-thread request'):
            agent, directory = self.agent()
            native_id = str(uuid4())
            project = self.transcript(native_id, self.response_with('msg_1', 10, 1000, 50), self.response_with('msg_2', 5, 80000, 2000))
            (project / native_id / 'subagents' / 'agent-1.jsonl').write_text(self.response_with('sub_1', 999999, 0, 0))
            session = SessionHandle(agent, 'claude', str(self.work), launch_id='current', native_id=native_id,
                                    session_file=str(directory / 'session.jsonl'))
            value = harnesses.load(self.root, 'claude').get_usage(session).value
            self.assertEqual(value['context'], {'tokens': 82005})
            # The cumulative total stays available beside it.
            self.assertGreater(value['total_tokens'], 82005)
        with self.subTest('a row with zero usage is never the latest response'):
            from multiverse.harnesses.claude import context_reading
            agent, directory = self.agent()
            native_id = str(uuid4())
            project = self.transcript(native_id, self.stamped('msg_1', '2026-10-04T00:00:00Z', 10, 150000, 0),
                                      self.built('2026-10-04T00:10:00.500Z'), self.built('2026-10-04T00:10:00.501Z'))
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}):
                self.assertEqual(context_reading(native_id)[:2], (150010, '2026-10-04T00:00:00Z'))
                with (project / f'{native_id}.jsonl').open('a') as handle:
                    handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
                self.assertEqual(context_reading(native_id)[:2], (32005, '2026-10-04T00:11:00Z'))

    def stamped(self, id, stamp, input_tokens=1, read=0, written=0):
        usage = {'input_tokens': input_tokens, 'output_tokens': 7, 'cache_read_input_tokens': read,
                 'cache_creation_input_tokens': written}
        return json.dumps({'type': 'assistant', 'timestamp': stamp,
                           'message': {'id': id, 'usage': usage, 'content': [{'type': 'text'}]}}) + '\n'

    def built(self, stamp):
        """A row a compaction's install stores: a new ID, Claude's synthetic model and zero usage."""
        usage = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
        return json.dumps({'type': 'assistant', 'timestamp': stamp, 'message': {
            'id': str(uuid4()), 'model': '<synthetic>', 'usage': usage, 'content': [{'type': 'text', 'text': 'kept'}]}}) + '\n'

    def turn(self, id, stamp, model, usage=1, sidechain=False, **effort):
        """A main-thread response with its model (ABSENT: no model key), effort fields as given, and usage."""
        row = {'type': 'assistant', 'timestamp': stamp, **effort, 'message': {
            'id': id, 'model': model, 'content': [{'type': 'text'}],
            'usage': {'input_tokens': usage, 'output_tokens': usage, 'cache_read_input_tokens': 0,
                      'cache_creation_input_tokens': 0}}}
        if model is ABSENT:
            del row['message']['model']
        return json.dumps({**row, 'isSidechain': True} if sidechain else row) + '\n'

    def test_the_observed_turn_is_the_latest_real_main_thread_response(self):
        """A wrong row or effort field in the latest-turn reading shows a plausible model the session isn't using."""
        old = self.turn('msg_1', '2026-11-12T10:00:00Z', 'claude-opus-5-5', effort='high', perTurnEffort='high')
        sonnet = self.turn('msg_2', '2026-11-12T10:05:00Z', 'claude-sonnet-5-5', effort='low', perTurnEffort='low')
        cases = [
            ('the latest real response wins', [old, sonnet], ('claude-sonnet-5-5', 'low', '2026-11-12T10:05:00Z')),
            ("Claude's own rows (API errors, an install) never count",
             # Every synthetic row seen has zero usage; the model alone must still keep one out.
             [sonnet, self.turn('msg_3', '2026-11-12T10:06:00Z', '<synthetic>', usage=1, perTurnEffort='xhigh'),
              self.built('2026-11-12T10:07:00Z')], ('claude-sonnet-5-5', 'low', '2026-11-12T10:05:00Z')),
            ('a sidechain row never counts',
             [sonnet, self.turn('msg_4', '2026-11-12T10:06:00Z', 'claude-haiku-5-5', sidechain=True, effort='medium')],
             ('claude-sonnet-5-5', 'low', '2026-11-12T10:05:00Z')),
            ("a compaction's kept copy (zero usage) never counts",
             [sonnet, self.turn('msg_1', '2026-11-12T10:06:00Z', 'claude-opus-5-5', usage=0, effort='high')],
             ('claude-sonnet-5-5', 'low', '2026-11-12T10:05:00Z')),
            ('perTurnEffort, when present, is the effort', [self.turn('msg_5', '2026-11-12T10:08:00Z',
             'claude-opus-5-5', effort='high', perTurnEffort='max')], ('claude-opus-5-5', 'max', '2026-11-12T10:08:00Z')),
            ('a null perTurnEffort is no effort, whatever effort says', [self.turn('msg_6', '2026-11-12T10:08:00Z',
             'claude-haiku-4-5-20251001', effort='high', perTurnEffort=None)], ('claude-haiku-4-5-20251001', None, '2026-11-12T10:08:00Z')),
            ('without perTurnEffort, effort is', [self.turn('msg_7', '2026-11-12T10:08:00Z', 'claude-opus-5',
             effort='xhigh')], ('claude-opus-5', 'xhigh', '2026-11-12T10:08:00Z')),
            ('no effort recorded', [self.turn('msg_8', '2026-11-12T10:08:00Z', 'claude-opus-5-5')],
             ('claude-opus-5-5', None, '2026-11-12T10:08:00Z')),
            *((f'a latest reply with model {label} keeps its own effort and time, never an older model',
               [old, self.turn('msg_9', '2026-11-12T10:09:00Z', model, perTurnEffort='low')],
               (None, 'low', '2026-11-12T10:09:00Z'))
              for label, model in (('absent', ABSENT), ('null', None), ('empty', ''), ('not a string', 123))),
        ]
        adapter = harnesses.ClaudeAdapter.configured('claude', {'inbox_bridge': False})
        for name, rows, expected in cases:
            with self.subTest(name):
                native_id = str(uuid4())
                project = self.transcript(native_id, *rows)
                # A subagent's newer turn is its own conversation's.
                (project / native_id / 'subagents' / 'agent-1.jsonl').write_text(
                    self.turn('sub_1', '2026-11-12T11:00:00Z', 'claude-haiku-5-5', effort='medium'))
                value = adapter.get_usage(SessionHandle('agent', 'claude', str(self.work), native_id=native_id)).value
                self.assertEqual((value.get('model'), value.get('effort'), value.get('observed_at')), expected)
        def observed(native_id):
            value = adapter.get_usage(SessionHandle('agent', 'claude', str(self.work), native_id=native_id)).value or {}
            return value.get('model'), value.get('effort'), value.get('observed_at')
        mine = ('claude-sonnet-5-5', 'low', '2026-11-12T10:05:00Z')
        for name, session, expected in (("another conversation's reply", str(uuid4()), mine),
                                        ('a reply whose sessionId is null', None, mine),
                                        ("this conversation's reply", 'SELF', ('claude-haiku-5-5', 'medium', '2026-11-12T10:06:00Z'))):
            with self.subTest(name):
                native_id = str(uuid4())
                self.transcript(native_id, sonnet, self.turn('msg_s', '2026-11-12T10:06:00Z', 'claude-haiku-5-5',
                                effort='medium', sessionId=native_id if session == 'SELF' else session))
                self.assertEqual(observed(native_id), expected)
        with self.subTest('a complete unreadable row after the latest reply makes it unknown, until a later reply'):
            native_id = str(uuid4())
            project = self.transcript(native_id, sonnet)
            self.assertEqual(observed(native_id), mine)
            with (project / f'{native_id}.jsonl').open('a') as handle:
                handle.write('{"type": "assistant", "message": {"mod')  # unfinished: Claude is still writing it
            self.assertEqual(observed(native_id), mine)
            with (project / f'{native_id}.jsonl').open('a') as handle:
                handle.write('\n')  # now a complete row that can't be read
            self.assertEqual(observed(native_id), (None, None, None))
            with (project / f'{native_id}.jsonl').open('a') as handle:
                handle.write(self.turn('msg_n', '2026-11-12T10:07:00Z', 'claude-opus-5-5', perTurnEffort='high'))
            self.assertEqual(observed(native_id), ('claude-opus-5-5', 'high', '2026-11-12T10:07:00Z'))
        with self.subTest('before any turn, nothing is observed'):
            native_id = str(uuid4())
            self.transcript(native_id, json.dumps({'type': 'user', 'message': {'content': 'hi'}}) + '\n')
            value = adapter.get_usage(SessionHandle('agent', 'claude', str(self.work), native_id=native_id)).value
            self.assertEqual([key for key in ('model', 'effort', 'observed_at') if value and value.get(key)], [])

    def test_usage_reads_keep_no_rows_and_transcript_reads_keep_a_few(self):
        """The dashboard reads every agent's usage on each listing; an index that kept an entry per row for each
        transcript, never evicted, held about 1.5 times every transcript in memory (0.7 GB on one machine)."""
        from multiverse.harnesses.claude import transcripts as t
        self.enterContext(patch.object(t, '_usage', t.OrderedDict()))
        self.enterContext(patch.object(t, '_transcripts', t.OrderedDict()))
        adapter = harnesses.ClaudeAdapter.configured('claude', {'inbox_bridge': False})
        natives = [str(uuid4()) for _ in range(t.FULL_INDEXES + 3)]
        for native_id in natives:
            project = self.transcript(native_id, self.turn('m1', '2026-11-13T10:00:00Z', 'claude-opus-5-5', effort='high'),
                                      self.turn('m2', '2026-11-13T10:01:00Z', 'claude-sonnet-5-5', effort='low'))
            (project / native_id / 'subagents' / 'agent-1.jsonl').write_text(self.response('sub_1', 3, 4))
            value = adapter.get_usage(SessionHandle('agent', 'claude', str(self.work), native_id=native_id)).value
            self.assertEqual((value['total_tokens'], value['model'], value['effort']), (2 + 2 + 7, 'claude-sonnet-5-5', 'low'))
        self.assertEqual(len(t._usage), 2 * len(natives))  # each main and subagent transcript
        self.assertEqual([len(index.entries) for index in t._usage.values()], [0] * len(t._usage))
        self.assertEqual(len(t._transcripts), 0)  # usage builds no full index
        for native_id in natives:
            self.assertEqual(len(t.newest_copy(native_id, None, ()).entries), 2)  # a transcript read: every row
        self.assertEqual(len(t._transcripts), t.FULL_INDEXES)
        # A full index read as the file grows hashes its rows as a fresh read does (cursors and positions use them).
        grown = t.newest_copy(natives[0], None, ())
        path = grown.path
        with path.open('a') as handle:
            handle.write(self.turn('m3', '2026-11-13T10:02:00Z', 'claude-opus-5-5'))
        self.assertEqual([e.chain for e in grown.read().entries], [e.chain for e in t.Transcript(path).read().entries])
        # An index evicted and built again starts above every version a reader could hold: a history that changed
        # while no index watched it (a compaction putting rows before ones already read) is never read as grown.
        held = t.newest_copy(natives[-1], None, ())
        seen = t.analyse(held).version
        for native_id in natives[:t.FULL_INDEXES]:
            t.newest_copy(native_id, None, ())
        self.assertNotIn(held.path, t._transcripts)
        self.assertGreater(t.analyse(t.newest_copy(natives[-1], None, ())).version, seen)
        self.enterContext(patch.object(t, 'USAGE_INDEXES', 4))
        adapter.get_usage(SessionHandle('agent', 'claude', str(self.work), native_id=natives[0]))
        self.assertEqual(len(t._usage), 4)

    def test_a_new_process_takes_over_saved_usage_only_where_it_proves(self):
        """A new dashboard parsed every transcript from its start (about a second per large agent); a saved index
        taken without proof would count a rewritten transcript's old usage, plausibly, and unnoticed."""
        from multiverse.harnesses.claude import transcripts as t
        adapter = harnesses.ClaudeAdapter.configured('claude', {'inbox_bridge': False})
        agent, directory = self.agent()
        native_id = str(uuid4())
        session = SessionHandle(agent, 'claude', str(self.work), native_id=native_id,
                                session_file=str(directory / 'session.jsonl'))
        store = directory / t.USAGE_STORE
        prompt = json.dumps({'type': 'user', 'message': {'content': 'SECRET-PROMPT-TEXT'}}) + '\n'
        rows = [prompt, self.turn('msg_1', '2026-11-13T10:00:00Z', 'claude-opus-5-5', 10, effort='high'),
                self.turn('msg_2', '2026-11-13T10:01:00Z', 'claude-sonnet-5-5', 20, effort='low')]
        project = self.transcript(native_id, *rows)
        main, sub = project / f'{native_id}.jsonl', project / native_id / 'subagents' / 'agent-1.jsonl'
        sub.write_text(self.response('sub_1', 3, 4))

        def fresh_process(**patches):
            """Usage read as by a new process: no index in memory, nothing written yet; (value, lines parsed)."""
            parsed = []
            real = t.entry
            with patch.object(t, '_usage', t.OrderedDict()), patch.object(t, '_saved_at', {}), \
                    patch.object(t, 'entry', lambda *a: parsed.append(1) or real(*a)), \
                    patch.multiple(t, **patches) if patches else patch.object(t, 'TAIL', t.TAIL):
                return adapter.get_usage(session).value, len(parsed)

        def truth():
            """The total read from nothing saved."""
            with patch.object(t, '_usage', t.OrderedDict()):
                return measured_usage(native_id, (str(self.work),)).total_tokens

        def lines():
            return main.read_text().count('\n')  # the subagent's transcript, unchanged, proves and isn't parsed

        def facts(value):
            return value['total_tokens'], value['model'], value['effort'], value['context']['tokens']

        first, parsed = fresh_process()
        self.assertEqual((facts(first), parsed), ((10 * 2 + 20 * 2 + 3 + 4, 'claude-sonnet-5-5', 'low', 20), 4))
        saved = store.read_text()
        self.assertNotIn('SECRET-PROMPT-TEXT', saved)  # positions, digests and counts only
        self.assertEqual(sorted(json.loads(saved)['transcripts']), sorted([str(main), str(sub)]))
        with main.open('a') as handle:
            handle.write(self.turn('msg_3', '2026-11-13T10:02:00Z', 'claude-opus-5-5', 30, effort='max'))
        grown, parsed = fresh_process()
        self.assertEqual((facts(grown), parsed), ((first['total_tokens'] + 60, 'claude-opus-5-5', 'max', 30), 1))
        content = main.read_text()
        changed = {
            'an earlier row rewritten in place, the same size': content.replace('"input_tokens": 10', '"input_tokens": 90'),
            'a file moved over it, the same bytes': None,
            'a shorter file': ''.join(content.splitlines(keepends=True)[:-1]),
        }
        for case, text in changed.items():
            with self.subTest(case):
                fresh_process()  # the store is current for the file as it is now
                try:
                    if text is None:
                        copy = main.with_name('copy.jsonl')
                        copy.write_text(content)
                        os.replace(copy, main)
                    else:
                        main.write_text(text)
                    value, parsed = fresh_process()
                    self.assertEqual((value['total_tokens'], parsed), (truth(), lines()))  # every line again
                finally:
                    main.write_text(content)
        damaged = {
            'not JSON': '{', 'another schema': {'schema': 2, 'transcripts': {}},
            'a prefix that is not a digest': {'prefix': 'x' * 64}, 'a negative count': {'responses': {'msg_1': [-1, 0, 0, 0]}},
            'a count list of the wrong length': {'responses': {'msg_1': [1, 2]}}, 'a field missing': {'chain': None},
        }
        for case, damage in damaged.items():
            with self.subTest(case):
                fresh_process()
                data = json.loads(store.read_text())
                if isinstance(damage, dict) and 'schema' not in damage:
                    data['transcripts'][str(main)].update(damage)
                    store.write_text(json.dumps(data))
                else:
                    store.write_text(damage if isinstance(damage, str) else json.dumps(damage))
                value, parsed = fresh_process()
                whole = not (isinstance(damage, dict) and 'schema' not in damage)  # the subagent's entry goes too
                self.assertEqual((value['total_tokens'], parsed), (grown['total_tokens'], lines() + whole))
        with self.subTest('a store is read no further than its bound, on the opened file'):
            large = directory / 'large.json'
            large.write_text(json.dumps({'schema': 1, 'transcripts': {'x': 'y' * 512}}))
            opened, asked = t.open_regular, []

            def counting(path):
                handle = opened(path)
                read = handle.read
                handle.read = lambda size=-1: asked.append(size) or read(size)
                return handle
            with patch.object(t, 'STORE_BYTES', 128), patch.object(t, 'open_regular', side_effect=counting):
                self.assertEqual(t._load_store(large), {})
            self.assertEqual(asked, [129])  # one read, of the bound plus one byte: never the whole file
        with self.subTest('a write never goes through a link at a temporary name'):
            outside = self.work / 'unrelated.txt'
            outside.write_text('keep')
            for name in (f'{store.name}.{os.getpid()}.0.tmp', f'{store.name}.{os.getpid()}.tmp'):
                (directory / name).symlink_to(outside)
            with patch.object(t.threading, 'get_ident', return_value=0):
                self.assertTrue(t._save_store(store, {}))
            self.assertEqual((outside.read_text(), store.is_symlink()), ('keep', False))
        with self.subTest('a store past its bound is neither written nor read'):
            store.unlink()
            fresh_process(STORE_BYTES=64)
            self.assertFalse(store.exists())
            fresh_process()
            _, parsed = fresh_process(STORE_BYTES=64)
            self.assertEqual(parsed, lines() + 1)  # the subagent's too: nothing was read

    def reported(self):
        """An agent whose last response (00:00, 150k) the mod reported at 00:05."""
        agent, directory = self.agent()
        native_id = str(uuid4())
        project = self.transcript(native_id, self.stamped('msg_1', '2026-10-04T00:00:00Z', 10, 150000, 0))
        self.report(directory, {'tokens': 150010, 'window': 200000, 'percent': 75}, '2026-10-04T00:05:00+00:00')
        session = SessionHandle(agent, 'claude', str(self.work), launch_id='current', native_id=native_id,
                                session_file=str(directory / 'session.jsonl'))
        return harnesses.load(self.root, 'claude'), session, project, native_id

    def report(self, directory, context, at):
        facts = read(directory / 'runtime.json')
        write(directory / 'runtime.json', {**facts, 'usage': {'context': context, 'rate_limits': [], 'reported_at': at}})

    def test_the_newer_of_the_mods_report_and_the_transcript_is_the_reading(self):
        """An older report must never mask a newer response in the transcript, nor the transcript a newer report,
        nor a report from another launch: each way the dashboard shows a stale but plausible size."""
        adapter, session, project, native_id = self.reported()
        stale = SessionHandle(session.agent_id, 'claude', str(self.work), launch_id='other', native_id=native_id,
                              session_file=session.session_file)
        self.assertNotIn('window', adapter.get_usage(stale).value['context'])  # another launch's report is never used
        self.assertEqual(adapter.get_usage(session).value['context']['tokens'], 150010)
        with (project / f'{native_id}.jsonl').open('a') as handle:
            handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
        context = adapter.get_usage(session).value['context']
        self.assertEqual(context['tokens'], 32005)  # the report predates the new response
        self.assertNotIn('percent', context)
        self.report(self.root / 'state/agents' / session.agent_id,
                    {'tokens': 32005, 'window': 200000, 'percent': 16}, '2026-10-04T00:12:00+00:00')
        context = adapter.get_usage(session).value['context']
        self.assertEqual((context['tokens'], context['percent']), (32005, 16))
