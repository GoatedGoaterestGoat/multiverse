"""Real adapter metadata reaches the core API without borrowing observations across launches."""
from uuid import uuid4

from multiverse.plugins.testing import ClaudeRows, PluginTestCase
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from multiverse.tools import get_usage


class JoinedUsageTest(PluginTestCase):
    def test_claude_turn_fields_survive_usage_serialization_and_a_resume_clears_them(self):
        """A valid adapter observation can disappear during serialization or survive a resume before the new launch has a turn."""
        directory = self.agent('worker', model='opus', effort='high',
                               runtime={'started_at': '2026-01-01T00:00:00Z'})
        rows = ClaudeRows()
        rows.prompt('first')
        first = rows.response('first reply', model='claude-sonnet-5-5', effort='high', per_turn_effort='low')
        transcript = self.transcript(directory, rows.rows)

        def observation():
            result = get_usage.run(self.root, agent='worker')
            self.assertEqual(result['state'], 'ok', result)
            value = result['value']
            self.assertEqual(value['native_session_id'], transcript.native_id)
            return tuple(value[key] for key in ('model', 'effort', 'observed_at'))

        self.assertEqual(observation(), ('claude-sonnet-5-5', 'low', first['timestamp']))
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['observed_model_name'], 'Sonnet 5.5')
        self.assertEqual((row['model'], row['effort']), ('opus', 'high'))
        facts = read(directory / 'runtime.json')
        write(directory / 'runtime.json', {**facts, 'launch_id': str(uuid4()),
                                          'started_at': '2026-01-01T00:00:03Z'})
        self.assertEqual(observation(), (None, None, None))
        rows.prompt('after resume')
        second = rows.response('second reply', model='claude-haiku-5-5', effort='high', per_turn_effort=None)
        transcript.append(*rows.rows[-2:])
        self.assertEqual(observation(), ('claude-haiku-5-5', None, second['timestamp']))
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['observed_model_name'], 'Haiku 5.5')
        self.assertEqual((row['model'], row['effort']), ('opus', 'high'))

    def test_codex_turn_fields_reach_core_only_with_saved_context_for_this_thread(self):
        """Codex's turn metadata needs its measured thread ID to survive core filtering and must clear on a new launch."""
        import json
        identity = str(uuid4())
        home = self.work / 'codex-home'
        folder = home / 'sessions/2026/11/12'
        folder.mkdir(parents=True)
        path = folder / f'rollout-2026-11-12T10-00-00-{identity}.jsonl'
        header = {'type': 'session_meta', 'payload': {'id': identity, 'cli_version': '0.167.2', 'source': 'cli'}}
        def context(model, effort, at):
            return {'type': 'turn_context', 'timestamp': at, 'payload': {'model': model, 'effort': effort}}
        first = context('gpt-6.1-sol', 'high', '2026-11-12T10:01:00Z')
        path.write_text('\n'.join(json.dumps(item) for item in (header, first)) + '\n')
        directory = self.agent('worker', harness='codex', native_id=identity, native_session_home=str(home),
                               model='gpt-6-astra', effort='low', runtime={
                                   'started_at': '2026-11-12T10:00:00Z', 'native_mode': {'mode': 'plain'},
                                   'native_versions': {'cli': '0.167.2'},
                                   'usage': {'native_session_id': identity, 'source': 'codex-app-server',
                                             'context': {'tokens': 1234}, 'reported_at': '2026-11-12T10:01:00Z'}})
        def observation():
            result = get_usage.run(self.root, agent='worker')
            self.assertEqual(result['state'], 'ok', result)
            value = result['value']
            self.assertEqual(value.get('native_session_id'), identity)
            self.assertEqual(value['context']['tokens'], 1234)
            return tuple(value[key] for key in ('model', 'effort', 'observed_at'))
        self.assertEqual(observation(), ('gpt-6.1-sol', 'high', first['timestamp']))
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['observed_model_name'], 'GPT 6.1 Sol')
        self.assertEqual((row['model'], row['effort']), ('gpt-6-astra', 'low'))
        facts = read(directory / 'runtime.json')
        write(directory / 'runtime.json', {**facts, 'launch_id': str(uuid4()),
                                          'started_at': '2026-11-12T10:02:00Z'})
        self.assertEqual(observation(), (None, None, None))
        with path.open('a') as stream:
            stream.write(json.dumps(context('Provider/raw', None, '2026-11-12T10:03:00Z')) + '\n')
        self.assertEqual(observation(), ('Provider/raw', None, '2026-11-12T10:03:00Z'))
        self.assertIsNone(lifecycle.listing(self.root)[0]['observed_model_name'])
