"""Claude's compaction protocol: transcript reading across compactions, merges through a fake `claude`,
rendering for the mod's hook, launch settings and an install end to end. No model is called."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import harnesses
from multiverse.compaction import __main__ as entry, config
from multiverse.compaction.model import Leaf, Node
from multiverse.compaction.protocol import MergeError
from multiverse.compaction.store import Store
from multiverse.harnesses.claude import Transcript
from multiverse.harnesses.claude.compaction import ClaudeCompaction, TranscriptError, merge, render, transcript
from multiverse.harnesses.types import LaunchContext, SessionHandle
from multiverse.store import write
from test_multiverse import BaseTest

START = datetime(2026, 9, 1, tzinfo=timezone.utc)

FAKE_CLAUDE = r'''#!{python}
import json, os, re, sys
argv = sys.argv[1:]
session = argv[argv.index('--resume') + 1]
slug = re.sub(r'[^a-zA-Z0-9]', '-', os.getcwd())
path = os.path.join(os.environ['CLAUDE_CONFIG_DIR'], 'projects', slug, session + '.jsonl')
rows = [json.loads(line) for line in open(path)]
thinking = any(isinstance(block, dict) and block.get('type') == 'thinking'
               for row in rows for block in (row.get('message', {{}}).get('content') or []) if isinstance(row.get('message', {{}}).get('content'), list))
instruction = argv[argv.index('-p') + 1]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({{'argv': argv, 'rows': len(rows), 'thinking': thinking, 'instruction': instruction,
                          'first': rows[0].get('message', {{}}).get('content'),
                          'mv2': sorted(k for k in os.environ if k.startswith('MV2_')),
                          'retries': os.environ.get('CLAUDE_CODE_MAX_RETRIES')}}) + '\n')
mode = os.environ.get('FAKE_MODE', 'merge')
if mode == 'refuse-thinking' and (thinking or 'thinking' in instruction):
    print(json.dumps({{'type': 'result', 'is_error': True, 'stop_reason': 'refusal', 'api_error_status': None,
                      'result': "API Error: the model's safeguards flagged this message"}}))
    sys.exit(1)
if mode in ('merge', 'refuse-thinking'):
    print(json.dumps({{'type': 'result', 'is_error': False, 'stop_reason': 'end_turn', 'api_error_status': None,
                      'result': json.dumps({{'start': 0, 'end': 2, 'summary': 'I handled two steps.'}})}}))
elif mode == 'ratelimit':
    print(json.dumps({{'type': 'result', 'is_error': True, 'api_error_status': 429, 'result': 'API Error: 429 rate_limit_error'}}))
    sys.exit(1)
'''


class Rows:
    """A Claude transcript under construction: rows chained by parentUuid with rising timestamps."""

    def __init__(self):
        self.rows, self.parent, self.clock = [], None, START

    def add(self, kind, prompt=None, chained=True, **fields):
        self.clock += timedelta(seconds=10)
        row = {'type': kind, 'uuid': fields.pop('uuid', None) or str(uuid4()), 'timestamp': self.clock.isoformat(),
               'parentUuid': self.parent if chained else None, 'sessionId': 's', **fields}
        if prompt:
            row['promptId'] = prompt
        self.rows.append(row)
        self.parent = row['uuid']
        return row

    def user(self, text, prompt, **fields):
        return self.add('user', prompt, message={'role': 'user', 'content': text}, **fields)

    def assistant(self, message_id, block, output=0, **fields):
        return self.add('assistant', message={'id': message_id, 'role': 'assistant', 'model': 'claude-test',
                                              'content': [block], 'usage': {'output_tokens': output}}, **fields)

    def result(self, tool_id, text, prompt):
        return self.add('user', prompt, message={'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': tool_id, 'content': text}]})

    def command(self, name, prompt):
        self.user('<local-command-caveat>Caveat</local-command-caveat>', prompt, isMeta=True)
        self.user(f'<command-name>{name}</command-name>', prompt)
        self.user('<local-command-stdout>Compacted</local-command-stdout>', prompt)

    def boundary(self):
        logical = self.parent
        return self.add('system', subtype='compact_boundary', chained=False, logicalParentUuid=logical,
                        compactMetadata={'trigger': 'manual'})


def conversation():
    """Five real turns, two /compact command turns, Claude's own compaction and one of ours."""
    r = Rows()
    ids = {}
    ids['p1'] = r.user('First task', 'p1')
    ids['think1'] = r.assistant('m1', {'type': 'thinking', 'thinking': '', 'signature': 'a' * 400}, 300)
    r.assistant('m1', {'type': 'text', 'text': 'I will look'}, 300)
    ids['use'] = r.assistant('m2', {'type': 'tool_use', 'id': 'tu1', 'name': 'Bash', 'input': {'command': 'ls'}}, 20)
    ids['result'] = r.result('tu1', 'a.txt b.txt', 'p1')
    ids['think2'] = r.assistant('m3', {'type': 'thinking', 'thinking': '', 'signature': 'b' * 100}, 500)
    ids['think3'] = r.assistant('m3', {'type': 'thinking', 'thinking': '', 'signature': 'c' * 300}, 500)
    r.assistant('m3', {'type': 'text', 'text': 'done'}, 500)
    r.add('attachment', attachment={'type': 'environment', 'snapshot': {}})
    r.user('Second', 'p2')
    r.assistant('m4', {'type': 'text', 'text': 'ok'}, 5)
    r.command('/compact', 'p3')
    r.user('Third', 'p4')
    r.assistant('m5', {'type': 'text', 'text': 'three'}, 5)
    r.boundary()
    r.add('user', 'px', message={'role': 'user', 'content': 'This session is being continued...'}, isCompactSummary=True)
    ids['p5'] = r.user('Fourth', 'p5')
    ids['answer5'] = r.assistant('m6', {'type': 'text', 'text': 'four'}, 5)
    r.clock += timedelta(seconds=5)
    ids['ours'] = r.boundary()
    ids['built'] = r.user('First task', 'p6')
    r.add('attachment', attachment={'type': 'deferred_tools_delta', 'addedNames': ['X']})
    ids['copy'] = r.assistant('m6', {'type': 'text', 'text': 'four'}, 0)
    r.command('/compact', 'p6')
    ids['p7'] = r.user('Fifth', 'p7')
    ids['answer7'] = r.assistant('m7', {'type': 'text', 'text': 'five'}, 5)
    return r, ids


class Fixture(BaseTest):
    def setUp(self):
        super().setUp()
        self.config = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config), 'FAKE_LOG': str(self.work / 'fake.log')}))
        self.session_id = str(uuid4())
        self.agent_dir = self.root / 'state/agents' / str(uuid4())
        self.agent_dir.mkdir(parents=True)
        write(self.agent_dir / 'properties.json', {'id': self.agent_dir.name, 'name': 'worker', 'harness': 'claude',
                                                   'workspace': str(self.work), 'native_session_id': self.session_id})
        fake = self.work / 'fake-claude'
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        harness = self.root / 'harnesses/claude/harness.json'
        value = json.loads(harness.read_text())
        value['settings'] = {'inbox_bridge': True, 'executable': str(fake)}
        write(harness, value)
        self.adapter = harnesses.load(self.root, 'claude')

    def write_transcript(self, rows):
        path = self.config / 'projects' / '-work' / f'{self.session_id}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def source(self):
        session = SessionHandle(self.agent_dir.name, 'claude', str(self.work), native_id=self.session_id,
                                session_file=str(self.agent_dir / 'session.jsonl'))
        return ClaudeCompaction(self.adapter, session)

    def save_payload(self, created, count, sources):
        store = Store(self.agent_dir)
        install_id, path = store.save_install({'messages': [], 'count': count, 'sources': sources})
        value = json.loads(path.read_text())
        value['created_at'] = created
        path.write_text(json.dumps(value))
        return install_id

    def calls(self):
        path = self.work / 'fake.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class TranscriptTest(Fixture):
    def test_history_crosses_both_compactions_without_their_rows(self):
        r, ids = conversation()
        self.write_transcript(r.rows)
        install = self.save_payload(ids['built']['timestamp'].replace('+00:00', '+00:00'), 2,
                                    [{'stands_for': None}, {'stands_for': ids['answer5']['uuid']}])
        # The payload must predate its boundary.
        value = json.loads((self.agent_dir / 'compaction/installs' / f'{install}.json').read_text())
        value['created_at'] = (datetime.fromisoformat(ids['ours']['timestamp']) - timedelta(seconds=1)).isoformat()
        (self.agent_dir / 'compaction/installs' / f'{install}.json').write_text(json.dumps(value))
        source = self.source()
        turns = source.turns()
        self.assertEqual([t.prompt.content['text'] for t in turns], ['First task', 'Second', 'Third', 'Fourth', 'Fifth'])
        first = turns[0]
        self.assertEqual([leaf.kind for leaf in first.leaves], ['thinking', 'text', 'tool', 'thinking', 'thinking', 'text'])
        self.assertEqual(first.leaves[2].refs, (ids['use']['uuid'], ids['result']['uuid']))
        # Each response's thinking estimate (output less visible text) is shared by signature length.
        self.assertEqual([first.leaves[i].tokens for i in (0, 3, 4)], [297, 124, 374])
        self.assertEqual(source.copies, {ids['answer5']['uuid']: [ids['copy']['uuid']]})
        recorded = Store(self.agent_dir).installed()
        self.assertEqual(len(recorded), 1)
        self.assertEqual([row['uuid'] for row in recorded[0]['rows']], [ids['built']['uuid'], ids['copy']['uuid']])
        self.assertEqual(len(source.turns()), 5)
        self.assertEqual(len(Store(self.agent_dir).installed()), 1)

    def test_an_install_with_no_payload_stops_the_read(self):
        r, _ = conversation()
        self.write_transcript(r.rows)
        with self.assertRaisesRegex(TranscriptError, 'no record of the rows it added'):
            self.source().turns()

    def test_a_turn_end_marker_completes_every_turn_started_before_it(self):
        r = Rows()
        for index in range(3):
            r.user(f'prompt {index}', f'p{index}')
            r.assistant(f'm{index}', {'type': 'text', 'text': 'answer'}, 5)
        self.write_transcript(r.rows)
        source = self.source()
        turns = source.turns()
        ended = datetime.fromisoformat(r.rows[3]['timestamp']) + timedelta(seconds=1)
        self.assertEqual(len(source.completed(turns, str(int(ended.timestamp() * 1000)))), 2)
        self.assertEqual(len(source.completed(turns, str(int(r.clock.timestamp() * 1000) + 1))), 3)
        self.assertEqual(len(source.completed(turns, None)), 2)

    def test_a_typed_display_command_is_not_a_turn(self):
        r = Rows()
        r.user('Real work', 'p1')
        r.assistant('m1', {'type': 'text', 'text': 'done'}, 5)
        r.user('/compact', 'p2')
        r.user('/compact keep the plan', 'p3')
        r.command('/compact', 'p4')
        r.user('/rename better', 'p5')
        r.user('/compactly is not a command', 'p6')
        texts = [t.prompt.content['text'] for t in transcript.turns(r.rows, set())]
        self.assertEqual(texts, ['Real work', '/rename better', '/compactly is not a command'])

    def test_rewound_branches_and_sidechains_are_left_out(self):
        r = Rows()
        r.user('keep', 'p1')
        fork = r.parent
        r.assistant('m1', {'type': 'text', 'text': 'abandoned'}, 5)
        r.parent = fork
        r.assistant('m2', {'type': 'text', 'text': 'kept'}, 5)
        r.add('user', 'p9', isSidechain=True, message={'role': 'user', 'content': 'side'})
        self.write_transcript(r.rows)
        turns = transcript.turns(r.rows, set())
        self.assertEqual([[leaf.content['text'] for leaf in t.leaves] for t in turns], [['kept']])


class RenderTest(unittest.TestCase):
    def test_offered_rows_are_kept_and_the_rest_is_built_one_block_each(self):
        prompt = Leaf('user_text', ('p',), 2, content={'text': 'Do it'})
        thinking = Leaf('thinking', ('t',), 50)
        tool = Leaf('tool', ('use', 'res'), 30, content={'id': 'tu1', 'name': 'Bash', 'input': {'command': 'ls'},
                                                         'result': 'a b', 'is_error': False})
        text = Leaf('text', ('x',), 3, content={'text': 'done'})
        old = Leaf('text', ('orig',), 3, content={'text': 'kept by its copy'})
        summary = Node(0, [thinking, tool], 'I listed the files.', 5)
        from multiverse.compaction.model import Turn
        turns = [Turn('one', prompt, [thinking, tool, text]), Turn('two', None, [old]), Turn('three', None, [thinking, tool])]
        frontiers = {'one': [summary, text], 'two': [old], 'three': [thinking, tool]}
        offered = [{'handle': handle, 'role': 'x'} for handle in ('x', 'copy2', 'use', 'res')]
        payload = render.render(turns, frontiers, offered, {'orig': ['copy1', 'copy2']})
        self.assertEqual(payload['messages'], [
            {'role': 'user', 'text': 'Do it', 'toolUses': []},
            {'role': 'assistant', 'text': 'I listed the files.', 'toolUses': []},
            {'keep': 'x'}, {'keep': 'copy2'}, {'keep': 'use'}, {'keep': 'res'}])
        self.assertEqual(payload['sources'][3], {'stands_for': 'orig'})
        self.assertEqual((payload['count'], payload['kept'], payload['built'], payload['dropped']), (6, 4, 2, 1))
        built = render.render(turns[2:], {'three': [tool]}, [], {})['messages']
        self.assertEqual(built[0]['toolUses'][0]['tool_use_id'], 'tu1')
        self.assertEqual(built[1]['toolResults'], [{'tool_use_id': 'tu1', 'text': 'a b', 'isError': False}])


class MergeTest(Fixture):
    def test_claude_results_are_read_as_merges_or_failures_of_the_right_kind(self):
        ok = json.dumps({'is_error': False, 'result': 'Sure.\n```json\n{"start": 1, "end": 3, "summary": "x"}\n```'})
        self.assertEqual(merge.interpret(0, ok, ''), merge.Merge(1, 3, 'x'))
        cases = [(1, {'is_error': True, 'api_error_status': 429, 'result': 'rate_limit_error'}, 'rate_limit'),
                 (1, {'is_error': True, 'api_error_status': 529, 'result': 'Overloaded'}, 'rate_limit'),
                 (1, {'is_error': True, 'api_error_status': 500, 'result': 'Internal'}, 'rate_limit'),
                 (1, {'is_error': True, 'api_error_status': 429, 'result': "You've hit your limit · resets 3am"}, 'usage_limit'),
                 (1, {'is_error': True, 'stop_reason': 'refusal', 'result': '[reasoning_extraction]'}, 'refused'),
                 (0, {'is_error': False, 'result': 'no json here'}, 'invalid'),
                 (0, {'is_error': False, 'result': '{"start": 0}'}, 'invalid'),
                 (1, {'is_error': True, 'api_error_status': 400, 'result': 'bad request'}, 'failed')]
        for code, envelope, kind in cases:
            with self.subTest(envelope=envelope):
                with self.assertRaises(MergeError) as caught:
                    merge.interpret(code, json.dumps(envelope), '')
                self.assertEqual(caught.exception.kind, kind)
        with self.assertRaises(MergeError) as caught:
            merge.interpret(1, '', 'Error: connect ECONNRESET')
        self.assertEqual(caught.exception.kind, 'rate_limit')

    def test_a_merge_runs_claude_over_a_synthetic_session_it_then_deletes(self):
        r, _ = conversation()
        rows = [row for row in r.rows[:12]]
        self.write_transcript(rows)
        source = self.source()
        turn = source.turns()[0]
        builder = config.Builder(model='sonnet', effort='low', rate_limit=config.RateLimit(claude_max_retries=2))
        with patch.dict(os.environ, {'MV2_AGENT_ID': 'leak', 'CLAUDECODE': '1'}):
            result = source.merge(turn, list(turn.leaves), builder)
        self.assertEqual(result, merge.Merge(0, 2, 'I handled two steps.'))
        call = self.calls()[-1]
        argv = call['argv']
        self.assertEqual(argv[argv.index('--model') + 1], 'sonnet')
        self.assertEqual(argv[argv.index('--effort') + 1], 'low')
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertIn('--strict-mcp-config', argv)
        self.assertIn('--no-session-persistence', argv)
        self.assertEqual(call['mv2'], [])
        self.assertEqual(call['retries'], '2')
        self.assertEqual(call['first'], 'First task')  # the turn's own rows, verbatim
        self.assertEqual(call['rows'], 8 + 2)  # the turn's eight rows, then the context-switch exchange
        self.assertTrue(call['thinking'])
        self.assertIn('[0] part 1: thinking', call['instruction'])
        work = Store(self.agent_dir).directory / 'work'
        self.assertEqual(list((self.config / 'projects' / merge.project_slug(work.resolve())).glob('*.jsonl')), [])
        later = [Node(0, turn.leaves[:2], 'I looked.', 3), *turn.leaves[2:]]
        source.merge(turn, later, builder)
        call = self.calls()[-1]
        self.assertEqual(call['rows'], 8 + 2)  # every step reads the same original rows
        self.assertIn('[0] summary of parts 1-2, ~3 tokens | "I looked."', call['instruction'])
        self.assertIn('[1] part 3: tool', call['instruction'])

    def test_a_refused_turn_continues_without_its_thinking_and_never_names_it(self):
        r, _ = conversation()
        self.write_transcript(r.rows[:12])
        source = self.source()
        turn = source.turns()[0]
        builder = config.Builder(turn_retries=1, rate_limit=config.RateLimit(retries=0))
        from multiverse.compaction.build import Build, Reporter
        events = []
        with patch.dict(os.environ, {'FAKE_MODE': 'refuse-thinking'}):
            failures = Build(source, Store(self.agent_dir), builder, Reporter(Store(self.agent_dir), events.append)).turns([turn])
        self.assertEqual(failures, {})
        calls = self.calls()
        self.assertTrue(calls[0]['thinking'])
        self.assertTrue(all(not call['thinking'] and 'thinking' not in call['instruction'] for call in calls[1:]))
        self.assertIn('part 1: not shown', calls[1]['instruction'])
        self.assertEqual(calls[1]['rows'], 5 + 2)
        refusal = [event for event in events if event['event'] == 'error']
        self.assertEqual(len(refusal), 1)
        self.assertIn('leave its thinking out', refusal[0]['message'])

    def test_a_rate_limited_merge_reports_its_kind(self):
        r, _ = conversation()
        self.write_transcript(r.rows[:12])
        source = self.source()
        turn = source.turns()[0]
        with patch.dict(os.environ, {'FAKE_MODE': 'ratelimit'}), self.assertRaises(MergeError) as caught:
            source.merge(turn, list(turn.leaves), config.Builder())
        self.assertEqual(caught.exception.kind, 'rate_limit')


class AdapterTest(Fixture):
    def values(self):
        return dict(name='worker', agent_id=self.agent_dir.name, session_id=self.session_id, bootstrap='Hi',
                    bootstrap_json='"Hi"', task='', agent_dir=str(self.agent_dir), harness_dir='', session_file='',
                    bootstrap_file='', task_file='', workspace=str(self.work), model='',
                    control_socket=str(self.work / 'inbox.sock'))

    def settings(self, command):
        return json.loads(command.argv[command.argv.index('--settings') + 1])

    def test_an_enabled_universe_tells_the_mod_and_guards_claudes_own_compaction(self):
        off = self.adapter.spawn(LaunchContext('spawn', self.values()))
        self.assertNotIn('hooks', self.settings(off))
        self.assertNotIn('MV2_COMPACTION', off.environment)
        write(self.root / 'compaction.json', {'harnesses': {'claude': {'enabled': True}}})
        on = self.adapter.spawn(LaunchContext('spawn', self.values()))
        hooks = self.settings(on)['hooks']['PreCompact'][0]['hooks'][0]
        self.assertIn('-m multiverse.compaction guard', hooks['command'])
        self.assertEqual(on.environment['MV2_COMPACTION'], '1')
        self.assertEqual(self.settings(on)['crossSessionInbound'], 'accept')
        write(self.root / 'compaction.json', {'enabled': True, 'harnesses': {'claude': {'enabled': False}}})
        self.assertNotIn('hooks', self.settings(self.adapter.spawn(LaunchContext('spawn', self.values()))))

    def test_compaction_needs_the_bridge_and_stays_out_of_core_capabilities(self):
        self.assertIsNone(self.source().unavailable())
        write(self.root / 'harnesses/claude/harness.json', {'schema': 2, 'adapter': 'claude', 'settings': {}})
        self.adapter = harnesses.load(self.root, 'claude')
        self.assertIn('inbox_bridge', self.source().unavailable())
        session = SessionHandle(self.agent_dir.name, 'claude', str(self.work), native_id=self.session_id)
        self.assertNotIn('compaction', self.adapter.capabilities(session))

    def test_kept_rows_stored_again_never_replace_their_responses_usage(self):
        r = Rows()
        r.user('hi', 'p1')
        r.assistant('m1', {'type': 'text', 'text': 'hello'}, 7)
        r.rows[-1]['message']['usage'] = {'input_tokens': 10, 'cache_read_input_tokens': 90, 'output_tokens': 7}
        r.assistant('m1', {'type': 'text', 'text': 'hello'})
        r.rows[-1]['message']['usage'] = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0,
                                          'cache_creation_input_tokens': 0}
        path = self.write_transcript(r.rows)
        self.assertEqual(Transcript(path).read().responses['m1']['cache_read_input_tokens'], 90)


class InstallTest(Fixture):
    def test_install_streams_the_compacted_conversation_for_the_hook(self):
        write(self.root / 'compaction.json', {'harnesses': {'claude': {'enabled': True}},
                                               'builder': {'rate_limit': {'wait_seconds': {'first': 0.01, 'max': 0.01}}}})
        r = Rows()
        for index in range(4):
            r.user(f'Task {index}', f'p{index}')
            for step in range(3):
                r.assistant(f'm{index}-{step}', {'type': 'text', 'text': f'step {step} of task {index}'}, 5)
        self.write_transcript(r.rows)
        offered = [{'handle': row['uuid'], 'role': row['type']} for row in r.rows]
        events = []
        code = entry.install(str(self.agent_dir), {'trigger': 'manual', 'offered': offered}, events.append)
        self.assertEqual(code, 0, events[-1])
        messages = [event['message'] for event in events if event['event'] == 'message']
        self.assertEqual(len(messages), events[-1]['count'])
        # Turns 0 and 1 become their prompt and one summary; turns 2 and 3 stay whole, by handle.
        self.assertEqual(sum(1 for message in messages if 'keep' in message), 4 + 4 + 2)
        summaries = [message['text'] for message in messages if message.get('role') == 'assistant']
        self.assertEqual(summaries, ['I handled two steps.', 'I handled two steps.'])
        self.assertEqual(len(self.calls()), 4)  # two merges for each three-leaf turn
        self.assertEqual(Store(self.agent_dir).tree_count(), 2)
        events = []
        entry.install(str(self.agent_dir), {'trigger': 'manual', 'offered': offered}, events.append)
        self.assertEqual(len(self.calls()), 4)


if __name__ == '__main__':
    unittest.main()
