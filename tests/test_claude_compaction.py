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
from multiverse.compaction import __main__ as entry, config, pipeline
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
    messages = [row['message'] for row in rows if 'message' in row]
    log.write(json.dumps({{'argv': argv, 'rows': len(rows), 'thinking': thinking, 'instruction': instruction,
                          'first': messages[0].get('content'), 'kinds': [row.get('type') for row in rows],
                          'assistant': [m['content'][0].get('text', m['content'][0].get('type'))
                                        for m in messages if m.get('role') == 'assistant'],
                          'mv2': sorted(k for k in os.environ if k.startswith('MV2_')),
                          'retries': os.environ.get('CLAUDE_CODE_MAX_RETRIES')}}) + '\n')
mode = os.environ.get('FAKE_MODE', 'merge')
if mode == 'refuse':
    print(json.dumps({{'type': 'result', 'is_error': True, 'stop_reason': 'refusal', 'api_error_status': None,
                      'result': "API Error: the model's safeguards flagged this message"}}))
    sys.exit(1)
if mode == 'merge':
    print(json.dumps({{'type': 'result', 'is_error': False, 'stop_reason': 'end_turn', 'api_error_status': None,
                      'result': json.dumps({{'start': 0, 'end': 2, 'summary': 'I handled two steps.'}})}}))
elif mode == 'ratelimit':
    print(json.dumps({{'type': 'result', 'is_error': True, 'api_error_status': 429, 'result': 'API Error: 429 rate_limit_error'}}))
    sys.exit(1)
'''


def claude_builder(**changes):
    """Claude's builder settings as an empty compaction.json gives them, with `changes`."""
    from dataclasses import replace
    return replace(config.Config().for_harness('claude', ClaudeCompaction.builder_defaults).builder, **changes)


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
        every = source.turns()
        turns = [t for t in every if not t.dropped]
        self.assertEqual([t.prompt.content['text'] for t in turns], ['First task', 'Second', 'Third', 'Fourth', 'Fifth'])
        self.assertEqual([t.dropped for t in every if t.dropped], ['the display-only command /compact'] * 2)
        first = turns[0]
        # A call and its result are one leaf, so no merge can separate them (v1's auto_merge_tool_pairs);
        # thinking is no leaf at all.
        self.assertEqual([leaf.kind for leaf in first.leaves], ['asst_text', 'tool_exchange', 'asst_text'])
        self.assertEqual(first.leaves[1].refs, (ids['use']['uuid'], ids['result']['uuid']))
        self.assertEqual(first.leaves[1].preview, 'Bash(ls) -> a.txt b.txt')
        # Its size is kept for the size before compaction: each response's output less its visible text,
        # shared by signature length (298, then 124 and 374).
        self.assertEqual(first.omitted, 298 + 124 + 374)
        recorded = Store(self.agent_dir).installed()
        self.assertEqual(len(recorded), 1)
        self.assertEqual([row['uuid'] for row in recorded[0]['rows']], [ids['built']['uuid'], ids['copy']['uuid']])
        self.assertEqual(len(source.turns()), 7)
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

    def test_display_command_turns_are_marked_dropped(self):
        r = Rows()
        r.user('Real work', 'p1')
        r.assistant('m1', {'type': 'text', 'text': 'done'}, 5)
        r.user('/compact', 'p2')
        r.user('/compact keep the plan', 'p3')
        r.command('/context', 'p4')
        r.user('/rename better', 'p5')
        r.user('/compactly is not a command', 'p6')
        r.add('system', subtype='local_command', content='<command-name>/model</command-name>')
        r.user('<local-command-stdout>Set model</local-command-stdout>', None, isMeta=True)
        r.assistant('m2', {'type': 'text', 'text': 'after'}, 5)
        turns = transcript.turns(r.rows, set())
        self.assertEqual([(t.prompt.content['text'][:20], bool(t.dropped)) for t in turns],
                         [('Real work', False), ('/compact', True), ('/compact keep the pl', True),
                          ('<local-command-cavea', True), ('/rename better', False),
                          ('/compactly is not a ', False)])
        self.assertEqual([leaf.content['text'] for leaf in turns[-1].leaves], ['after'])  # older command rows go

    def test_multiverse_inbox_and_send_runs_are_turns_and_its_other_forms_are_left_out(self):
        r = Rows()
        r.user('Reply with just: ok', 'p1')
        r.assistant('m1', {'type': 'text', 'text': 'ok'}, 5)

        def run(args, output):  # as Claude Code 2.1.288 stores a mod command's run
            r.add('system', subtype='local_command', content='<command-name>/multiverse</command-name>\n'
                  f'            <command-message>multiverse</command-message>\n            <command-args>{args}</command-args>')
            r.add('system', subtype='local_command', content=f'<local-command-stdout>multiverse: {output}</local-command-stdout>')
        run('inbox', 'No unread inbox-only messages.')
        run('send worker hello there', 'Sent to worker as you (accepted).')
        run('list', 'commands/help: the catalog')
        run('', 'commands/help: the catalog')
        run('help', 'Running help.')
        r.user('Explain this universe.', 'p2')  # the prompt /multiverse help submitted
        r.assistant('m2', {'type': 'text', 'text': 'It has a help command.'}, 5)
        turns = transcript.turns(r.rows, set())
        self.assertEqual(len(turns), 4)
        self.assertEqual([t.dropped for t in turns], [None] * 4)
        inbox, send = turns[1], turns[2]
        self.assertIn('<command-args>inbox</command-args>', inbox.prompt.content['text'])
        self.assertEqual([leaf.content['text'] for leaf in inbox.leaves],
                         ['<local-command-stdout>multiverse: No unread inbox-only messages.</local-command-stdout>'])
        self.assertIn('<command-args>send worker hello there</command-args>', send.prompt.content['text'])
        self.assertIn('Sent to worker as you', send.leaves[0].content['text'])
        self.assertEqual(turns[3].prompt.content['text'], 'Explain this universe.')
        everything = ' '.join(leaf.content.get('text', '') for t in turns for leaf in [t.prompt, *t.leaves])
        self.assertNotIn('the catalog', everything)
        self.assertNotIn('Running help.', everything)
        # Delivered as recorded: the command, then its output, both from the person.
        messages = render.render([inbox], {inbox.id: list(inbox.leaves)})['messages']
        self.assertEqual([(m['role'], m['text']) for m in messages],
                         [('user', inbox.prompt.content['text']), ('user', inbox.leaves[0].content['text'])])
        # Claude 2.1.287 stored commands as user rows under a promptId: the same rule marks the turn.
        old = Rows()
        old.user('<command-name>/multiverse</command-name>\n<command-args>list</command-args>', 'q1')
        old.user('<local-command-stdout>the catalog</local-command-stdout>', 'q1')
        old.user('<command-name>/multiverse</command-name>\n<command-args>inbox</command-args>', 'q2')
        old.user('<local-command-stdout>No unread inbox-only messages.</local-command-stdout>', 'q2')
        old.user('/multiverse help', 'q3')
        old.user('/compact', 'q4')
        self.assertEqual([t.dropped for t in transcript.turns(old.rows, set())],
                         ['the display-only command /multiverse list', None,
                          'the display-only command /multiverse help', 'the display-only command /compact'])

    def test_parallel_tool_calls_are_read_although_claude_links_them_as_branches(self):
        r = Rows()
        r.user('Run three', 'p1')
        calls = [r.assistant('m1', {'type': 'tool_use', 'id': f'tu{n}', 'name': 'Bash', 'input': {'command': f'c{n}'}}, 9)
                 for n in range(3)]
        for n in (2, 0, 1):  # each result hangs off its own call; the last one continues the chain
            r.parent = calls[n]['uuid']
            r.result(f'tu{n}', f'out{n}', 'p1')
        r.assistant('m2', {'type': 'text', 'text': 'done'}, 3)
        turns = transcript.turns(r.rows, set())
        self.assertEqual([leaf.preview for leaf in turns[0].leaves],
                         ['Bash(c0) -> out0', 'Bash(c1) -> out1', 'Bash(c2) -> out2', 'done'])

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


def describe(message):
    """A built message in a word: its text, its tool, or its result."""
    if message.get('toolResults'):
        return 'result:' + message['toolResults'][0]['text']
    return message['toolUses'][0]['tool'] if message['toolUses'] else message['text']


class RenderTest(unittest.TestCase):
    def test_every_message_is_built_one_block_each_and_thinking_is_left_out(self):
        from multiverse.compaction.model import Turn
        prompt = Leaf('user_text', ('p',), 2, content={'text': 'Do it'})
        thinking = Leaf('thinking', ('t',), 50, content={'text': ''})
        use = Leaf('tool_use', ('use',), 10, content={'tool_use_id': 'tu1', 'name': 'Bash', 'input': {'command': 'ls'}})
        result = Leaf('tool_result', ('res',), 20, content={'tool_use_id': 'tu1', 'text': 'a b', 'is_error': False})
        text = Leaf('asst_text', ('x',), 3, content={'text': 'done'})
        summary = Node(0, [thinking, use, result], 'I listed the files.', 5)
        turns = [Turn('one', prompt, [thinking, use, result, text]), Turn('two', None, [thinking, use, result])]
        payload = render.render(turns, {'one': [summary, text], 'two': [thinking, use, result]})
        self.assertEqual(payload['messages'], [
            {'role': 'user', 'text': 'Do it', 'toolUses': []},
            {'role': 'assistant', 'text': 'I listed the files.', 'toolUses': []},
            {'role': 'assistant', 'text': 'done', 'toolUses': []},
            {'role': 'assistant', 'text': '', 'toolUses': [{'tool_use_id': 'tu1', 'tool': 'Bash', 'input': {'command': 'ls'}}]},
            {'role': 'user', 'text': '', 'toolUses': [], 'toolResults': [{'tool_use_id': 'tu1', 'text': 'a b', 'isError': False}]}])
        self.assertNotIn('keep', json.dumps(payload))
        self.assertEqual(payload['sources'], [{'stands_for': None}] * 5)
        self.assertEqual((payload['count'], payload['built'], payload['dropped']), (5, 5, 1))

    def test_a_tool_exchange_is_delivered_as_its_call_then_its_result(self):
        from multiverse.compaction.model import Turn
        exchanges = [Leaf('tool_exchange', (f'u{n}', f'r{n}'), 9, content={
            'tool_use_id': n, 'name': 'Bash', 'input': {'command': n}, 'result_text': f'out {n}', 'is_error': n == 'b'})
            for n in 'ab']
        messages = render.render([Turn('t', None, exchanges)], {'t': exchanges})['messages']
        self.assertEqual(messages, [
            {'role': 'assistant', 'text': '', 'toolUses': [{'tool_use_id': 'a', 'tool': 'Bash', 'input': {'command': 'a'}}]},
            {'role': 'user', 'text': '', 'toolUses': [], 'toolResults': [{'tool_use_id': 'a', 'text': 'out a', 'isError': False}]},
            {'role': 'assistant', 'text': '', 'toolUses': [{'tool_use_id': 'b', 'tool': 'Bash', 'input': {'command': 'b'}}]},
            {'role': 'user', 'text': '', 'toolUses': [], 'toolResults': [{'tool_use_id': 'b', 'text': 'out b', 'isError': True}]}])

    def test_an_unpaired_call_or_result_gets_v1s_placeholder(self):
        from multiverse.compaction.model import Turn
        use = Leaf('tool_use', ('use',), 10, content={'tool_use_id': 'tu1', 'name': 'mcp:odd name', 'input': {}})
        result = Leaf('tool_result', ('res',), 20, content={'tool_use_id': 'tu1', 'text': 'a b', 'is_error': False})
        later = Leaf('asst_text', ('x',), 3, content={'text': 'done'})
        turn = Turn('t', None, [use, result, later])
        calls = render.render([turn], {'t': [use, Node(0, [result, later], 'I saw two files.', 4)]})['messages']
        self.assertEqual([describe(m) for m in calls], ['mcp_odd_name', 'result:(result summarized)', 'I saw two files.'])
        results = render.render([turn], {'t': [Node(0, [use], 'I listed.', 2), result, later]})['messages']
        self.assertEqual([describe(m) for m in results], ['I listed.', 'summarized_tool', 'result:a b', 'done'])
        self.assertEqual(results[1]['toolUses'][0]['tool_use_id'], 'tu1')

    def test_unpaired_calls_of_a_batch_are_answered_before_anything_follows(self):
        from multiverse.compaction.model import Turn
        uses = [Leaf('tool_use', (f'u{n}',), 5, content={'tool_use_id': n, 'name': 'Bash', 'input': {}}) for n in 'abc']
        results = [Leaf('tool_result', (f'r{n}',), 5, content={'tool_use_id': n, 'text': n, 'is_error': False}) for n in 'cab']
        a, b, c = uses
        rc, ra, rb = results
        end = Leaf('asst_text', ('end',), 5, content={'text': 'end'})
        turn = Turn('t', None, [*uses, *results, end])

        def shape(frontier):
            return [describe(m) for m in render.render([turn], {'t': frontier})['messages']]
        # A summary inside the batch waits for the raw results of its calls.
        self.assertEqual(shape([a, b, Node(0, [c, rc], 'S', 1), ra, rb, end]),
                         ['Bash', 'Bash', 'result:a', 'result:b', 'S', 'end'])
        # A raw result whose call was summarized waits too, then gets its placeholder call.
        self.assertEqual(shape([a, Node(0, [b, c], 'S', 1), rc, ra, rb, end]),
                         ['Bash', 'result:a', 'S', 'summarized_tool', 'result:c', 'summarized_tool', 'result:b', 'end'])
        # Calls whose results were summarized are answered once the raw ones have followed.
        self.assertEqual(shape([a, b, c, Node(0, [rc, ra], 'S', 1), rb, end]),
                         ['Bash', 'Bash', 'Bash', 'result:b', 'result:(result summarized)', 'result:(result summarized)',
                          'S', 'end'])

    def test_images_are_noted_and_an_empty_prompt_gets_v1s_placeholder(self):
        from multiverse.compaction.model import Turn
        prompt = Leaf('user_text', ('p',), 3000, content={'text': 'Look', 'images': 2}, images=2990)
        shown = Leaf('user_text', ('s',), 1600, content={'text': '', 'images': 1}, images=1600)
        payload = render.render([Turn('t', prompt, [shown]), Turn('u', Leaf('user_text', ('q',), 0, content={'text': ''}), [])],
                                {'t': [shown], 'u': []})
        self.assertEqual([m['text'] for m in payload['messages']], [
            'Look\n\n_[2 image(s) omitted to fit compaction budget]_', '_[1 image(s) omitted to fit compaction budget]_',
            '_(no user prompt for this turn)_'])


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
        builder = config.Builder(model='sonnet', effort='low', options={'claude_max_retries': 2})
        with patch.dict(os.environ, {'MV2_AGENT_ID': 'leak', 'CLAUDECODE': '1'}):
            result = source.merge(turn, list(turn.leaves), builder)
        self.assertEqual(result, merge.Merge(0, 2, 'I handled two steps.'))
        call = self.calls()[-1]
        argv = call['argv']
        self.assertEqual(argv[argv.index('--model') + 1], 'sonnet')
        self.assertEqual(argv[argv.index('--effort') + 1], 'low')
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertEqual(argv[argv.index('--system-prompt') + 1], merge.SYSTEM_PROMPT)
        self.assertIn('--strict-mcp-config', argv)
        self.assertIn('--no-session-persistence', argv)
        self.assertEqual(call['mv2'], [])
        self.assertEqual(call['retries'], '2')
        # The first step: v1's title row, the turn's own rows verbatim but its thinking, then the context switch.
        self.assertEqual(call['first'], 'First task')
        self.assertEqual(call['kinds'], ['custom-title', 'user', 'assistant', 'assistant', 'user', 'assistant',
                                         'user', 'assistant'])
        self.assertFalse(call['thinking'])
        self.assertIn('Current frontier (3 entries):\n\n  [0] asst_text | "I will look"\n'
                      '  [1] tool_exchange | "Bash(ls) -> a.txt b.txt"\n  [2] asst_text | "done"', call['instruction'])
        self.assertNotIn('thinking', call['instruction'])
        work = Store(self.agent_dir).directory / 'work'
        self.assertEqual(list((self.config / 'projects' / merge.project_slug(work.resolve())).glob('*.jsonl')), [])
        later = [Node(0, turn.leaves[:2], 'I looked.', 3), *turn.leaves[2:]]
        source.merge(turn, later, builder)
        call = self.calls()[-1]
        # Later steps: the prompt, one assistant row per frontier entry (thinking verbatim), the context switch.
        self.assertEqual(call['kinds'], ['custom-title', 'user', 'assistant', 'assistant', 'user', 'assistant'])
        self.assertEqual(call['assistant'][:2], ['**Compacted summary (2 entries):**\n\nI looked.', 'done'])
        self.assertFalse(call['thinking'])
        self.assertIn('[0] summary node (covers 2 leaves) | "I looked."\n  [1] asst_text | "done"', call['instruction'])

    def test_a_refused_merge_is_retried_unchanged_then_by_the_fallback_model(self):
        r, _ = conversation()
        self.write_transcript(r.rows[:12])
        source = self.source()
        turn = source.turns()[0]
        builder = claude_builder(turn_retries=1, rate_limit=config.RateLimit(retries=0))
        from multiverse.compaction.build import Build, Reporter
        events = []
        with patch.dict(os.environ, {'FAKE_MODE': 'refuse'}):
            failures = Build(source, Store(self.agent_dir), builder, Reporter(Store(self.agent_dir), events.append)).turns([turn])
        self.assertIn('refused', failures[turn.id])
        calls = self.calls()
        # Retried unchanged, then continued with the fallback model, retried unchanged again.
        self.assertEqual([call['argv'][call['argv'].index('--model') + 1] for call in calls],
                         ['sonnet', 'sonnet', 'claude-sonnet-4-6', 'claude-sonnet-4-6'])
        self.assertTrue(all(not call['thinking'] and call['rows'] == 8 for call in calls))
        self.assertEqual([event['retrying'] for event in events if event['event'] == 'error'], [True, False, True, False])

    def test_a_rate_limited_merge_reports_its_kind(self):
        r, _ = conversation()
        self.write_transcript(r.rows[:12])
        source = self.source()
        turn = source.turns()[0]
        with patch.dict(os.environ, {'FAKE_MODE': 'ratelimit'}), self.assertRaises(MergeError) as caught:
            source.merge(turn, list(turn.leaves), claude_builder())
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

    def test_an_unusable_compaction_json_never_stops_a_launch(self):
        from multiverse.tools import doctor
        session = SessionHandle(self.agent_dir.name, 'claude', str(self.work), native_id=self.session_id,
                                session_file=str(self.agent_dir / 'session.jsonl'))
        cases = [({'builder': {'model': 'sonnet'}}, 'belong to one harness'),
                 ({'harnesses': {'claude': {'enabled': True, 'builder': {'effort': 'extreme'}}}},
                  'harnesses.claude.builder: effort')]
        for value, expected in cases:
            with self.subTest(expected=expected):
                write(self.root / 'compaction.json', value)
                command = self.adapter.spawn(LaunchContext('spawn', self.values()))
                self.assertNotIn('hooks', self.settings(command))
                self.assertNotIn('MV2_COMPACTION', command.environment)
                self.assertEqual(command.native_mode['mode'], 'bridge without compaction')
                self.assertIn(expected, command.native_mode['fallback_reason'])
                capability = self.adapter.capabilities(session)['compaction']
                self.assertEqual(capability.state, 'error')
                self.assertIn(expected, capability.reason)
                self.assertTrue(any(expected in problem for problem in doctor.run(self.root)['problems']))

    def test_the_compaction_capability_says_whether_this_session_compacts(self):
        from dataclasses import replace
        session = SessionHandle(self.agent_dir.name, 'claude', str(self.work), native_id=self.session_id,
                                session_file=str(self.agent_dir / 'session.jsonl'))
        write(self.root / 'compaction.json', {'harnesses': {'claude': {'enabled': True}}})
        self.assertEqual(self.adapter.capabilities(session)['compaction'].state, 'supported')
        fell_back = replace(session, state='running', native_mode={'fallback_reason': 'Launched without compaction: x'})
        capability = self.adapter.capabilities(fell_back)['compaction']
        self.assertEqual((capability.state, 'restart' in capability.reason), ('error', True))
        write(self.root / 'compaction.json', {'harnesses': {'claude': {'enabled': False}}})
        self.assertEqual(self.adapter.capabilities(session)['compaction'].state, 'unavailable')
        write(self.root / 'harnesses/claude/harness.json', {'schema': 2, 'adapter': 'claude', 'settings': {'inbox_bridge': False}})
        capability = harnesses.load(self.root, 'claude').capabilities(session)['compaction']
        self.assertEqual(capability.state, 'unavailable')
        self.assertIn('inbox_bridge', capability.reason)

    def test_claude_supplies_its_builder_defaults_and_checks_its_own_settings(self):
        builder = claude_builder()
        self.assertEqual((builder.model, builder.fallback_model, builder.effort, builder.options),
                         ('sonnet', 'claude-sonnet-4-6', 'medium', {'claude_max_retries': 3}))
        source = self.source()
        source.check_builder(builder)
        cases = [(claude_builder(effort='extreme'), 'effort'), (claude_builder(model=None), 'model'),
                 (claude_builder(options={'claude_max_retries': 16}), 'claude_max_retries'),
                 (claude_builder(options={'claude_max_retries': 3, 'colour': 'red'}), 'colour')]
        for value, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                source.check_builder(value)
        write(self.root / 'compaction.json', {'harnesses': {'claude': {'builder': {'effort': 'extreme'}}}})
        from multiverse.tools.compaction import find_agent
        with self.assertRaisesRegex(ValueError, 'harnesses.claude.builder: effort'):
            find_agent(self.root, self.agent_dir.name)

    def test_compaction_needs_the_bridge(self):
        self.assertIsNone(self.source().unavailable())
        write(self.root / 'harnesses/claude/harness.json', {'schema': 2, 'adapter': 'claude', 'settings': {'inbox_bridge': False}})
        self.adapter = harnesses.load(self.root, 'claude')
        self.assertIn('inbox_bridge', self.source().unavailable())
        session = SessionHandle(self.agent_dir.name, 'claude', str(self.work), native_id=self.session_id)
        self.assertEqual(self.adapter.capabilities(session)['compaction'].state, 'unavailable')

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


class DashboardBackendTest(Fixture):
    def test_the_dashboard_reads_and_saves_compaction_through_the_tool(self):
        from multiverse.ui.backend import LocalBackend
        backend, agent = LocalBackend(), self.agent_dir.name
        r = Rows()
        for index in range(4):
            r.user(f'Task {index}', f'p{index}')
            r.assistant(f'm{index}', {'type': 'text', 'text': f'first answer {index}'}, 5)
            r.assistant(f'n{index}', {'type': 'text', 'text': f'second answer {index}'}, 5)
        self.write_transcript(r.rows)
        write(self.root / 'compaction.json', {'harnesses': {'claude': {'enabled': True}}})
        view = backend.compaction_overview(self.root)
        claude = next(row for row in view['harnesses'] if row['name'] == 'claude')
        self.assertEqual((claude['enabled'], claude['options'], claude['builder']['model'], claude['problem']),
                         (True, ['claude_max_retries'], 'sonnet', None))
        self.assertEqual(view['file'], {'harnesses': {'claude': {'enabled': True}}})
        row = next(row for row in view['agents'] if row['id'] == agent)
        self.assertEqual((row['scheduler_from'], row['trees_cached']), ('universe', 0))
        # The whole file and Claude's own checks run before anything is written.
        with self.assertRaisesRegex(ValueError, 'effort'):
            backend.save_compaction(self.root, {'harnesses': {'claude': {'enabled': True, 'builder': {'effort': 'extreme'}}}})
        self.assertEqual(json.loads((self.root / 'compaction.json').read_text()), {'harnesses': {'claude': {'enabled': True}}})
        saved = backend.save_compaction(self.root, {'default': {'scheduler': 'uniform', 'budget': 0},
                                                    'harnesses': {'claude': {'enabled': True}}})
        self.assertEqual(saved['default'], {'scheduler': 'uniform', 'budget': 0})
        # Previews never save; the default preview follows the universe.
        current = backend.compaction_agent(self.root, agent)
        self.assertEqual((current['scheduler_from'], current['summary']['turns'], current['summary']['summarized']),
                         ('universe', 4, 4))
        kept = backend.compaction_agent(self.root, agent, scheduler={'scheduler': 'last_k_raw', 'k': 4, 'budget': 0})
        self.assertEqual((kept['summary']['summarized'], kept['summary']['after_partial']), (0, False))
        self.assertEqual(kept['summary']['tokens_after'], kept['summary']['tokens_before'])
        self.assertTrue(backend.compaction_agent(self.root, agent, scheduler=None)['summary']['after_partial'])
        self.assertFalse(config.agent_file(self.agent_dir).exists())
        self.assertEqual(current['failures'], [])
        # A turn whose build is still failed says why, numbered as the plan numbers it.
        from multiverse.compaction.store import Store
        from multiverse.tools.compaction import find_agent
        turns = pipeline.conversation(find_agent(self.root, agent).source.turns())
        store = Store(self.agent_dir)
        store.set_failure(turns[2].id, 'the model refused this merge')
        store.set_failure('0123456789abcdef', 'gone')
        failing = backend.compaction_agent(self.root, agent)
        self.assertEqual(failing['failures'], ['turn 3: the model refused this merge', 'turn 01234567: gone'])
        self.assertEqual(backend.compaction_agent(self.root, agent, preview=False)['failures'], failing['failures'])
        store.set_failure(turns[2].id, None)
        store.set_failure('0123456789abcdef', None)
        with self.assertRaisesRegex(ValueError, 'scheduler'):
            backend.compaction_agent(self.root, agent, scheduler={'scheduler': 'nope'})
        own = backend.set_compaction_scheduler(self.root, agent, {'scheduler': 'last_k_raw', 'k': 1, 'budget': 0})
        self.assertEqual((own['scheduler_from'], own['scheduler']['k']), ('agent', 1))
        self.assertEqual(next(row for row in backend.compaction_overview(self.root)['agents']
                              if row['id'] == agent)['scheduler_from'], 'agent')
        self.assertEqual(backend.set_compaction_scheduler(self.root, agent, None)['scheduler_from'], 'universe')
        self.assertFalse(config.agent_file(self.agent_dir).exists())


class InstallTest(Fixture):
    def test_install_streams_the_compacted_conversation_for_the_hook(self):
        write(self.root / 'compaction.json', {'harnesses': {'claude': {
            'enabled': True, 'builder': {'rate_limit': {'wait_seconds': {'first': 0.01, 'max': 0.01}}}}}})
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
        # Turns 0 and 1 become their prompt and one summary; turns 2 and 3 stay whole, all of it built.
        self.assertFalse(any('keep' in message for message in messages))
        self.assertEqual(len(messages), 2 * 2 + 2 * 4)
        summaries = [message['text'] for message in messages if message.get('role') == 'assistant']
        self.assertEqual(summaries, ['I handled two steps.'] * 2 + [f'step {step} of task {task}' for task in (2, 3)
                                                                     for step in range(3)])
        self.assertEqual(len(self.calls()), 4)  # two merges for each three-leaf turn
        self.assertEqual(Store(self.agent_dir).tree_count(), 2)
        events = []
        entry.install(str(self.agent_dir), {'trigger': 'manual', 'offered': offered}, events.append)
        self.assertEqual(len(self.calls()), 4)


if __name__ == '__main__':
    unittest.main()
