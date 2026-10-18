"""Claude usage from its transcripts, and the compaction estimate that stands in for it."""
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


def compaction_here():
    """Whether this checkout has the compaction plugin (the plugins/compaction submodule), which makes the estimate."""
    from multiverse.plugins import entry
    from multiverse.plugins.loader import PluginError
    try:
        entry(None, 'compaction')  # raises only when no plugin by that name is found
    except PluginError:
        return False
    return True


class ClaudeUsageTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.config = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))
        # The compaction plugin makes the estimate after a compaction; the base template leaves it off.
        from multiverse.plugins import config as plugins_config
        plugins_config.set_enabled(self.root, 'compaction', True)

    def needs_compaction(self):
        """Skip unless the compaction plugin is found: checked as the test runs, under its own HOME (BaseTest isolates
        it), never at import, where discovery would read and write the real one."""
        if not compaction_here():
            self.skipTest('needs the compaction plugin: mv2 plugins install compaction')

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
                self.assertEqual(context_reading(native_id), (150010, '2026-10-04T00:00:00Z'))
                with (project / f'{native_id}.jsonl').open('a') as handle:
                    handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
                self.assertEqual(context_reading(native_id), (32005, '2026-10-04T00:11:00Z'))

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

    def test_compaction_estimate_stands_in_until_a_real_response(self):
        """After a compaction the plugin's estimate stands in until a real response arrives, and the rows the install
        writes do not end it; a break goes unnoticed because the dashboard shows a stale but plausible pre-compaction
        context (45b493d, 1c750f3)."""
        with self.subTest('the estimate stands in until a real response'):
            self.needs_compaction()
            adapter, session, project, native_id, install = self.compacted()
            context = self.context(adapter, session)
            self.assertEqual((context['tokens'], context['window'], context['estimated'], context['estimated_from']),
                             (22000, 200000, True, install))
            self.assertNotIn('percent', context)
            with (project / f'{native_id}.jsonl').open('a') as handle:
                handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
            context = self.context(adapter, session)
            # The mod's report predates the new response, so the transcript has the real request size.
            self.assertEqual(context['tokens'], 32005)
            self.assertNotIn('estimated', context)
        with self.subTest("the mod's newer report wins once it follows the response"):
            adapter, session, project, native_id, install = self.compacted()
            with (project / f'{native_id}.jsonl').open('a') as handle:
                handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
            self.report(self.root / 'state/agents' / session.agent_id,
                        {'tokens': 32005, 'window': 200000, 'percent': 16}, '2026-10-04T00:12:00+00:00')
            context = self.context(adapter, session)
            self.assertEqual((context['tokens'], context['percent']), (32005, 16))
            self.assertNotIn('estimated', context)
        with self.subTest('a compaction never installed, or without an estimate, changes nothing'):
            adapter, session, *_ = self.compacted(recorded=False)
            self.assertEqual(self.context(adapter, session)['tokens'], 150010)
            self.assertNotIn('estimated', self.context(adapter, session))
            adapter, session, *_ = self.compacted(installed_at='not a time')
            self.assertNotIn('estimated', self.context(adapter, session))
        with self.subTest('the rows an install stores do not end the estimate'):
            self.needs_compaction()
            adapter, session, project, native_id, install = self.compacted()
            # Claude stores the installed conversation as new rows half a second later, each with zero usage.
            with (project / f'{native_id}.jsonl').open('a') as handle:
                handle.write(self.built('2026-10-04T00:10:00.500Z') + self.built('2026-10-04T00:10:00.502Z'))
            context = self.context(adapter, session)
            self.assertEqual((context['tokens'], context['estimated'], context['estimated_from']),
                             (22000, True, install))
            with (project / f'{native_id}.jsonl').open('a') as handle:
                handle.write(self.stamped('msg_2', '2026-10-04T00:11:00Z', 5, 30000, 2000))
            context = self.context(adapter, session)
            self.assertEqual(context['tokens'], 32005)
            self.assertNotIn('estimated', context)
        with self.subTest('without the mod the estimate still replaces the stale reading'):
            self.needs_compaction()
            adapter, session, project, native_id, install = self.compacted()
            stale = SessionHandle(session.agent_id, 'claude', str(self.work), launch_id='other', native_id=native_id,
                                  session_file=session.session_file)
            context = self.context(adapter, stale)
            self.assertEqual((context['tokens'], context['estimated']), (22000, True))
            self.assertNotIn('window', context)
