"""Context, cost and rate limits reported by the Claude mod reach runtime.json and get_usage."""
import json
import os
from unittest.mock import patch
import unittest
from uuid import uuid4

from multiverse.harnesses.types import SessionHandle
from multiverse.store import read, write
from multiverse.tools import runtime_update
from multiverse import harnesses
from multiverse import display
from test_multiverse import BaseTest

REPORT = {'context': {'tokens': 82000, 'window': 200000, 'percent': 41}, 'cost_usd': 1.25,
          'rate_limits': [{'window': 'five_hour', 'percent_used': 34.5, 'resets_at': '2026-09-30T20:00:00Z'},
                          {'window': 'bad', 'percent_used': 'x'}]}


def compaction_here():
    """Whether this checkout has the compaction plugin (the plugins/compaction submodule), which makes the estimate."""
    from multiverse.plugins import entry
    from multiverse.plugins.loader import PluginError
    try:
        entry(None, 'compaction')  # raises only when no plugin by that name is found
    except PluginError:
        return False
    return True


NEEDS_COMPACTION = unittest.skipUnless(compaction_here(), 'needs the compaction plugin: mv2 plugins install compaction')


class ClaudeUsageTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.config = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))
        # The compaction plugin makes the estimate after a compaction; the base template leaves it off.
        from multiverse.plugins import config as plugins_config
        plugins_config.set_enabled(self.root, 'compaction', True)

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
        # The mod's report, plus when the transcript's latest response came (what compaction's estimate is checked against).
        self.assertEqual(value['context'], {**REPORT['context'], 'responded_at': '2026-09-30T00:00:00Z'})
        self.assertEqual(value['cost_usd'], 1.25)
        stale = SessionHandle(agent, 'claude', str(self.work), launch_id='previous', native_id=native_id,
                              session_file=str(directory / 'session.jsonl'))
        self.assertEqual(adapter.get_usage(stale).value['context'], {'tokens': 10, 'responded_at': '2026-09-30T00:00:00Z'})
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
        self.assertEqual(display.context_display({'state': 'ok', 'value': value}), '82k')

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

    def test_rows_with_zero_usage_are_never_the_latest_response(self):
        from multiverse.harnesses.claude import context_reading
        agent, directory = self.agent()
        native_id = str(uuid4())
        project = self.transcript(native_id, self.stamped('msg_1', '2026-10-04T00:00:00Z', 10, 150000, 0),
                                  self.built('2026-10-04T00:10:00.500Z'), self.built('2026-10-04T00:10:00.501Z'))
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}):
            self.assertEqual(context_reading(native_id), (150010, '2026-10-04T00:00:00Z'))
            with (project / f'{native_id}.jsonl').open('a') as handle:
                handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
            self.assertEqual(context_reading(native_id), (32005, '2026-10-04T00:11:00Z'))

    def compacted(self, installed_at='2026-10-04T00:10:00+00:00', recorded=True, estimate=22000, derived_at='2026-10-04T05:00:00+00:00'):
        agent, directory = self.agent()
        native_id = str(uuid4())
        project = self.transcript(native_id, self.stamped('msg_1', '2026-10-04T00:00:00Z', 10, 150000, 0))
        install = str(uuid4())
        write(directory / 'compaction' / 'installs' / f'{install}.json',
              {'stats': {'tokens_after': estimate}, 'install_id': install, 'created_at': installed_at})
        if recorded:
            (directory / 'compaction' / 'installed.jsonl').write_text(json.dumps({'install': install, 'rows': [], 'at': derived_at}) + '\n')
        self.report(directory, {'tokens': 150010, 'window': 200000, 'percent': 75}, '2026-10-04T00:05:00+00:00')
        session = SessionHandle(agent, 'claude', str(self.work), launch_id='current', native_id=native_id,
                                session_file=str(directory / 'session.jsonl'))
        return harnesses.load(self.root, 'claude'), session, project, native_id, install

    def report(self, directory, context, at):
        facts = read(directory / 'runtime.json')
        write(directory / 'runtime.json', {**facts, 'usage': {'context': context, 'rate_limits': [], 'reported_at': at}})

    def value(self, adapter, session):
        """The usage record as mv2 reports it: the adapter's, after plugins' adjustments (compaction's estimate)."""
        from multiverse.plugins import harness as plugin_harness
        return plugin_harness.adjust_usage(plugin_harness.session_info(session, 'claude'), adapter.get_usage(session).value)

    def context(self, adapter, session):
        return self.value(adapter, session)['context']

    @NEEDS_COMPACTION
    def test_after_our_compaction_the_estimate_stands_in_until_a_real_response_arrives(self):
        adapter, session, project, native_id, install = self.compacted()
        context = self.context(adapter, session)
        self.assertEqual((context['tokens'], context['window'], context['estimated'], context['estimated_from']),
                         (22000, 200000, True, install))
        self.assertNotIn('percent', context)
        value = self.value(adapter, session)
        self.assertEqual(display.context_display({'state': 'ok', 'value': value}), '~22k/200k')
        self.assertIn('11%, estimated after compaction: conversation only', display.context_line({'state': 'ok', 'value': value}))
        # The display says whatever the estimate's maker set; records from before it set one read as compaction's.
        other = {'state': 'ok', 'value': {'context': {'tokens': 5000, 'estimated': True, 'estimated_reason': 'by a plugin'}}}
        self.assertEqual(display.context_line(other).split(' · ')[0], 'Context: ~5.0k (estimated by a plugin)')
        other['value']['context']['estimated_reason'] = 'by\na plugin ' + 'x' * 200
        self.assertEqual(len(display.usage_context(other)['estimated_reason']), 80)
        self.assertNotIn('\n', display.context_line(other))
        with (project / f'{native_id}.jsonl').open('a') as handle:
            handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
        context = self.context(adapter, session)
        # The mod's report predates the new response, so the transcript has the real request size.
        self.assertEqual(context['tokens'], 32005)
        self.assertNotIn('estimated', context)
        self.assertEqual(display.context_display({'state': 'ok', 'value': self.value(adapter, session)}), '32k/200k')

    def test_the_mods_newer_report_wins_once_it_follows_the_response(self):
        adapter, session, project, native_id, install = self.compacted()
        with (project / f'{native_id}.jsonl').open('a') as handle:
            handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
        self.report(self.root / 'state/agents' / session.agent_id, {'tokens': 32005, 'window': 200000, 'percent': 16},
                    '2026-10-04T00:12:00+00:00')
        context = self.context(adapter, session)
        self.assertEqual((context['tokens'], context['percent']), (32005, 16))
        self.assertNotIn('estimated', context)

    def test_a_compaction_that_was_never_installed_or_has_no_estimate_changes_nothing(self):
        adapter, session, *_ = self.compacted(recorded=False)
        self.assertEqual(self.context(adapter, session)['tokens'], 150010)
        self.assertNotIn('estimated', self.context(adapter, session))
        adapter, session, *_ = self.compacted(installed_at='not a time')
        self.assertNotIn('estimated', self.context(adapter, session))

    @NEEDS_COMPACTION
    def test_the_rows_an_install_stores_do_not_end_the_estimate(self):
        adapter, session, project, native_id, install = self.compacted()
        # Claude stores the installed conversation as new rows half a second later, each with zero usage.
        with (project / f'{native_id}.jsonl').open('a') as handle:
            handle.write(self.built('2026-10-04T00:10:00.500Z') + self.built('2026-10-04T00:10:00.502Z'))
        context = self.context(adapter, session)
        self.assertEqual((context['tokens'], context['estimated'], context['estimated_from']), (22000, True, install))
        with (project / f'{native_id}.jsonl').open('a') as handle:
            handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
        context = self.context(adapter, session)
        self.assertEqual(context['tokens'], 32005)
        self.assertNotIn('estimated', context)

    @NEEDS_COMPACTION
    def test_without_the_mod_the_estimate_still_replaces_the_stale_transcript_reading(self):
        adapter, session, project, native_id, install = self.compacted()
        stale = SessionHandle(session.agent_id, 'claude', str(self.work), launch_id='other', native_id=native_id,
                              session_file=session.session_file)
        context = self.context(adapter, stale)
        self.assertEqual((context['tokens'], context['estimated']), (22000, True))
        self.assertNotIn('window', context)
