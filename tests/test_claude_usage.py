"""Context, cost and rate limits reported by the Claude mod reach runtime.json and get_usage."""
import json
import os
from unittest.mock import patch
from uuid import uuid4

from multiverse.harnesses.types import SessionHandle
from multiverse.store import read, write
from multiverse.tools import runtime_update
from multiverse import harnesses
from multiverse import usage as usage_module
from test_multiverse import BaseTest

REPORT = {'context': {'tokens': 82000, 'window': 200000, 'percent': 41}, 'cost_usd': 1.25,
          'rate_limits': [{'window': 'five_hour', 'percent_used': 34.5, 'resets_at': '2026-09-30T20:00:00Z'},
                          {'window': 'bad', 'percent_used': 'x'}]}


class ClaudeUsageTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.config = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))

    def response(self, id, input_tokens, output_tokens):
        usage = {'input_tokens': input_tokens, 'output_tokens': output_tokens,
                 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
        return json.dumps({'type': 'assistant', 'timestamp': '2026-09-30T00:00:00Z',
                           'message': {'id': id, 'usage': usage, 'content': [{'type': 'text'}]}}) + '\n'

    def agent(self):
        agent = str(uuid4())
        directory = self.root / 'state/agents' / agent
        write(directory / 'properties.json', {'id': agent, 'name': 'worker', 'harness': 'claude'})
        write(directory / 'runtime.json', {'launch_id': 'current', 'state': 'running', 'status': 'idle'})
        return agent, directory

    def test_usage_is_stored_sanitized_beside_status_and_keeps_the_launch_check(self):
        agent, directory = self.agent()
        with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current'}):
            runtime_update.run(self.root, agent=agent, usage_json=json.dumps(REPORT))
            with self.assertRaisesRegex(ValueError, 'Specify'):
                runtime_update.run(self.root, agent=agent)
            with self.assertRaisesRegex(ValueError, 'not JSON'):
                runtime_update.run(self.root, agent=agent, usage_json='{')
        facts = read(directory / 'runtime.json')
        self.assertEqual(facts['status'], 'idle')
        self.assertEqual(facts['usage']['context'], REPORT['context'])
        self.assertEqual(facts['usage']['cost_usd'], 1.25)
        self.assertEqual([limit['window'] for limit in facts['usage']['rate_limits']], ['five_hour'])
        self.assertIn('reported_at', facts['usage'])
        with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current'}):
            runtime_update.run(self.root, agent=agent, usage_json='{"context": {"window": 200000, "percent": 500}}')
        self.assertEqual(read(directory / 'runtime.json')['usage']['context'], {'window': 200000})

    def test_get_usage_merges_reported_figures_only_for_the_current_launch(self):
        agent, directory = self.agent()
        with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current'}):
            runtime_update.run(self.root, agent=agent, usage_json=json.dumps(REPORT))
        adapter = harnesses.load(self.root, 'claude')
        native_id = str(uuid4())
        project = self.config / 'projects' / '-work-project'
        project.mkdir(parents=True)
        (project / f'{native_id}.jsonl').write_text(self.response('msg_1', 10, 5))
        session = SessionHandle(agent, 'claude', str(self.work), launch_id='current', native_id=native_id,
                                session_file=str(directory / 'session.jsonl'))
        value = adapter.get_usage(session).value
        self.assertEqual(value['total_tokens'], 15)
        self.assertEqual(value['context'], REPORT['context'])
        self.assertEqual(value['cost_usd'], 1.25)
        stale = SessionHandle(agent, 'claude', str(self.work), launch_id='previous', native_id=native_id,
                              session_file=str(directory / 'session.jsonl'))
        self.assertEqual(adapter.get_usage(stale).value['context'], {'tokens': 10})
        self.assertNotIn('cost_usd', adapter.get_usage(stale).value)
        self.assertEqual(adapter.get_usage(stale).value['total_tokens'], 15)


    def transcript(self, native_id, *records):
        project = self.config / 'projects' / '-work-project'
        (project / native_id / 'subagents').mkdir(parents=True, exist_ok=True)
        (project / f'{native_id}.jsonl').write_text(''.join(records))
        return project

    def response_with(self, id, input_tokens, read, written):
        usage = {'input_tokens': input_tokens, 'output_tokens': 7, 'cache_read_input_tokens': read,
                 'cache_creation_input_tokens': written}
        return json.dumps({'type': 'assistant', 'message': {'id': id, 'usage': usage, 'content': [{'type': 'text'}]}}) + '\n'

    def test_context_is_the_last_main_thread_request_without_subagents_and_without_the_mod(self):
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
        self.assertEqual(usage_module.context_display({'state': 'ok', 'value': value}), '82k')

    def test_mod_window_is_kept_and_a_reported_request_size_wins(self):
        agent, directory = self.agent()
        native_id = str(uuid4())
        self.transcript(native_id, self.response_with('msg_1', 1, 2, 3))
        session = SessionHandle(agent, 'claude', str(self.work), launch_id='current', native_id=native_id,
                                session_file=str(directory / 'session.jsonl'))
        with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current'}):
            runtime_update.run(self.root, agent=agent, usage_json='{"context": {"window": 200000}}')
            self.assertEqual(harnesses.load(self.root, 'claude').get_usage(session).value['context'],
                             {'window': 200000, 'tokens': 6})
            runtime_update.run(self.root, agent=agent, usage_json=json.dumps(REPORT))
        self.assertEqual(harnesses.load(self.root, 'claude').get_usage(session).value['context'], REPORT['context'])
