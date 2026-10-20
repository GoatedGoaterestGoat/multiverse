"""Exact-thread Codex context accounting with no model calls."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import sys
import unittest
from uuid import uuid4
from unittest.mock import patch

import test_codex_launch as launch_tests
from multiverse.runtime import lifecycle, rpc
from multiverse.universe import agents
from multiverse.ui import tree
from test_multiverse import TerminalBase
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.harnesses.codex.usage import last_measurement, notification
from multiverse.harnesses.types import SessionHandle
from multiverse.runtime.runner import UsagePublisher
from multiverse.store import read, write


def event(native_id='thread', tokens=17457, window=258400):
    return {'method': 'thread/tokenUsage/updated', 'params': {
        'threadId': native_id, 'turnId': 'turn', 'tokenUsage': {
            'last': {'inputTokens': 17288, 'cachedInputTokens': 7936,
                     'cacheWriteInputTokens': 0, 'outputTokens': 169,
                     'reasoningOutputTokens': 100, 'totalTokens': tokens},
            'total': {'totalTokens': 9999999}, 'modelContextWindow': window}}}


class CodexContextTest(unittest.TestCase):
    def test_context_usage_is_read_from_the_latest_request(self):
        """Context usage comes from thread/tokenUsage/updated's latest request, not the cumulative total, with the
        native rounding; a break goes unnoticed because a plausible wrong number shows in the Context column and in
        what compaction reads."""
        observed = notification(event(), 'thread')
        self.assertEqual(observed['context'], {'tokens': 17457, 'window': 258400, 'percent': 2})
        self.assertNotIn('total_tokens', observed)
        for tokens, window, percent in [(0, 258400, 0), (12000, 258400, 0), (135200, 258400, 50),
                                        (12001, 12200, 0), (258400, 258400, 100), (300000, 258400, 100),
                                        (0, 12000, 100)]:
            with self.subTest(tokens=tokens, window=window):
                self.assertEqual(notification(event(tokens=tokens, window=window), 'thread')['context']['percent'],
                                 percent)
        for window in (None, 0, -1, True, '258400', float('nan')):
            with self.subTest(window=window):
                self.assertEqual(notification(event(window=window), 'thread')['context'], {'tokens': 17457})
        for tokens in (None, -1, True, '17457', 1.5, float('inf'), 2**63):
            with self.subTest(tokens=tokens):
                self.assertIsNone(notification(event(tokens=tokens), 'thread'))
        for value in ({}, {'method': 'thread/tokenUsage/updated', 'params': None},
                      {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'tokenUsage': {}}},
                      event('other')):
            with self.subTest(value=value):
                self.assertIsNone(notification(value, 'thread'))
        self.assertIsNone(notification(event(), None))


class CodexUsageRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write(self.root / 'properties.json', {'native_session_id': 'thread'})
        self.adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        self.session = SessionHandle('agent', 'codex', '/workspace', native_id='thread', launch_id='launch',
                                     session_file=str(self.root / 'session.jsonl'),
                                     native_mode={'policy': 'required', 'mode': 'bridge', 'fallback_reason': None})

    def reset(self):
        write(self.root / 'runtime.json', {'launch_id': 'launch', 'state': 'running'})
        self.publish = UsagePublisher(self.root, 'launch')

    def test_last_reading_carries_forward_only_for_its_thread(self):
        """The last reading carries across stop and resume only for its own thread and source; a break goes unnoticed
        because another thread's or harness's number looks plausible."""
        with self.subTest('the latest measurement replaces the previous one, also after compaction'):
            self.reset()
            self.assertEqual(self.adapter.get_usage(self.session).state, 'unavailable')
            self.publish(notification(event(tokens=180000), 'thread'))
            first = read(self.root / 'runtime.json')['usage']
            self.publish(notification(event(tokens=180000), 'thread'))
            self.assertEqual(read(self.root / 'runtime.json')['usage'], first)
            self.publish(notification(event(tokens=20000), 'thread'))
            result = self.adapter.get_usage(self.session)
            self.assertEqual(result.state, 'ok')
            self.assertEqual(result.value['context']['tokens'], 20000)
            self.assertIsNotNone(result.value['context_reported_at'])
            self.assertNotIn('total_tokens', result.value)
            from multiverse.display import context_display
            self.assertEqual(context_display(result.to_dict()), '20k/258k')
            self.assertEqual(self.adapter.capabilities(self.session)['get_usage'].state, 'supported')
        with self.subTest('a reading needs the exact launch and thread, but plain shows the last known'):
            self.reset()
            self.publish(notification(event(), 'thread'))
            for session in (replace(self.session, launch_id='old'), replace(self.session, native_id='other'),
                            replace(self.session, native_id=None), replace(self.session, session_file=None)):
                self.assertEqual(self.adapter.get_usage(session).state, 'unavailable')
            plain = replace(self.session, native_mode={'policy': 'plain', 'mode': 'plain', 'fallback_reason': None})
            adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
            result = adapter.get_usage(plain)
            self.assertTrue(result.value['context']['last_known'])
            self.assertEqual(result.value['context']['tokens'], 17457)
            self.assertEqual(adapter.capabilities(plain)['get_usage'].state, 'supported')
            write(self.root / 'runtime.json', {'launch_id': 'launch', 'state': 'running'})
            self.assertEqual(adapter.get_usage(plain).state, 'unavailable')
        with self.subTest('the snapshot survives a stop with its measurement time'):
            self.reset()
            self.publish(notification(event(tokens=0), 'thread'))
            facts = read(self.root / 'runtime.json')
            write(self.root / 'runtime.json', {**facts, 'state': 'stopped'})
            result = self.adapter.get_usage(self.session)
            self.assertEqual(result.value['context']['tokens'], 0)
            self.assertEqual(result.value['context_reported_at'], facts['usage']['reported_at'])
            self.assertTrue(result.value['context']['last_known'])
        with self.subTest('a retained reading survives unavailability until a new sample replaces it'):
            self.reset()
            self.publish(notification(event(tokens=180000), 'thread'))
            saved = read(self.root / 'runtime.json')['usage']
            current = replace(self.session, launch_id='new')
            for state in ('starting', 'running', 'stopped', 'dormant', 'failed'):
                write(self.root / 'runtime.json', {'launch_id': 'new', 'state': state, 'last_usage': saved})
                result = self.adapter.get_usage(current)
                self.assertEqual(result.value['context']['tokens'], 180000)
                self.assertTrue(result.value['context']['last_known'])
                self.assertEqual(result.value['context_reported_at'], saved['reported_at'])
            write(self.root / 'runtime.json', {'launch_id': 'new', 'state': 'running', 'last_usage': saved})
            # A stale publisher cannot replace the retained sample for the resumed launch.
            self.publish(notification(event(tokens=99999), 'thread'))
            self.assertEqual(self.adapter.get_usage(current).value['context']['tokens'], 180000)
            UsagePublisher(self.root, 'new')(notification(event(tokens=20000), 'thread'))
            refreshed = self.adapter.get_usage(current)
            self.assertEqual(refreshed.value['context']['tokens'], 20000)
            self.assertNotIn('last_known', refreshed.value['context'])
            self.assertNotEqual(refreshed.value['context_reported_at'], saved['reported_at'])
            facts = read(self.root / 'runtime.json')
            write(self.root / 'runtime.json', {**facts, 'native_relay': {'state': 'lost'}})
            self.assertTrue(self.adapter.get_usage(current).value['context']['last_known'])
        valid = {**notification(event(tokens=0), 'thread'), 'reported_at': 'original'}
        for invalid in (None, [], {}, {**valid, 'native_session_id': 'other'},
                        {**valid, 'source': 'claude-mod'}, {**valid, 'context': {'tokens': -1}},
                        {**valid, 'context': {'tokens': True}}):
            with self.subTest(carried=invalid):
                self.assertIsNone(last_measurement({'usage': invalid}, 'thread'))
                self.assertIsNone(last_measurement({'last_usage': invalid}, 'thread'))
                self.assertEqual(last_measurement({'usage': invalid, 'last_usage': valid}, 'thread'), valid)
        self.assertIsNone(last_measurement({'usage': valid}, None))


class CodexUsageLifecycleTest(TerminalBase):
    def test_context_survives_repeated_resumes_until_a_fresh_reading(self):
        """Context must survive real launch-record replacement and refresh; a silent loss otherwise looks like an empty context to the dashboard and compaction."""
        worker = self.work / 'codex-stand-in'
        worker.write_text(f'#!{sys.executable}\nimport sys,time\n'
                          'if sys.argv[1:] == ["--version"]:\n'
                          '    print("codex-cli 0.162.1"); raise SystemExit(0)\n'
                          'print("READY", flush=True)\ntime.sleep(120)\n')
        worker.chmod(0o700)
        write(self.root / 'harnesses/custom-codex/harness.json', {'schema': 2, 'adapter': 'codex',
              'settings': {'executable': str(worker), 'inbox_bridge': False}})
        started = lifecycle.start(self.root, 'context', harness='custom-codex', socket_path=self.socket,
                                  wait=True, wait_timeout=5)
        self.assertTrue(started['ready'], started)
        directory = agents.directory(self.root, started['id'])
        self.addCleanup(lifecycle.kill, self.root, started['id'])
        native_id = str(uuid4())
        agents.bind_session(self.root, started['id'], native_id)
        launch = started['runtime']['launch_id']
        agents.update_runtime(directory, launch, native_mode={'policy': 'auto', 'mode': 'bridge'})
        UsagePublisher(directory, launch)(notification(event(native_id, tokens=180000), native_id))
        original = read(directory / 'runtime.json')['usage']
        def row():
            return tree.with_usage(lifecycle.listing(self.root)[0])
        for index in range(2):
            with self.subTest(resume=index):
                lifecycle.kill(self.root, started['id'])
                self.assertEqual(row()['context'], '180k/258k')
                resumed = lifecycle.start(self.root, resume=started['id'], socket_path=self.socket,
                                          wait=True, wait_timeout=5)
                self.assertTrue(resumed['ready'], resumed)
                self.assertEqual(row()['context'], '180k/258k')
                self.assertTrue(row()['usage']['value']['context']['last_known'])
                self.assertEqual(row()['usage']['value']['context_reported_at'], original['reported_at'])
                self.assertEqual(read(directory / 'runtime.json')['last_usage'], original)
        launch = resumed['runtime']['launch_id']
        agents.update_runtime(directory, launch, native_mode={'policy': 'auto', 'mode': 'bridge'})
        UsagePublisher(directory, launch)(notification(event(native_id, tokens=20000), native_id))
        self.assertEqual(row()['context'], '20k/258k')
        self.assertNotIn('last_known', row()['usage']['value']['context'])


class CodexUsageRelayTest(unittest.IsolatedAsyncioTestCase):
    # Reuse the scripted native server, not its test cases.
    asyncSetUp = launch_tests.LaunchRelayTest.asyncSetUp
    asyncTearDown = launch_tests.LaunchRelayTest.asyncTearDown
    receive = launch_tests.LaunchRelayTest.receive

    async def connect(self):
        from websockets.asyncio.client import unix_connect
        from multiverse.universe.agents import bind_session
        self.settings['native_id'] = self.native_id
        self.protocol = LaunchProtocol(self.settings)
        self.publisher = UsagePublisher(self.directory, 'launch')
        self.proxy = rpc.LaunchProxy(self.spec, self.protocol,
            lambda native_id: bind_session(self.root, 'agent', native_id, 'launch'),
            self.errors.append, self.publisher)
        await asyncio.to_thread(self.proxy.start)
        self.client = await unix_connect(self.spec['socket_path'])
        await asyncio.wait_for(self.connected.wait(), 2)

    async def observe(self, value):
        payload = json.dumps(value)
        await self.upstream.send(payload)
        self.assertEqual(await asyncio.wait_for(self.client.recv(), 2), payload)

    async def bind(self):
        await self.client.send(json.dumps({'id': 3, 'method': 'thread/resume',
                                          'params': {'threadId': self.native_id}}))
        await self.receive()
        await self.observe({'id': 3, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}})

    async def test_usage_observation_never_alters_frames_or_disconnects(self):
        """Usage observation in the relay never alters a frame, and a failing runtime write (read-only or full disk)
        never disconnects the TUI; a break takes the agent offline because of a display number."""
        await self.connect()
        await self.observe(event(self.native_id))
        self.assertNotIn('usage', read(self.directory / 'runtime.json'))
        await self.bind()
        for value in (event('other'), event(self.native_id, tokens=-1)):
            await self.observe(value)
        self.assertNotIn('usage', read(self.directory / 'runtime.json'))
        with patch('multiverse.runtime.runner.update_runtime', side_effect=OSError('read-only runtime')):
            await self.observe(event(self.native_id))
        await self.observe({'method': 'future/event', 'params': {}})
        self.assertNotIn('usage', read(self.directory / 'runtime.json'))
        self.assertEqual(self.errors, [])
        await self.observe(event(self.native_id))
        usage = read(self.directory / 'runtime.json')['usage']
        self.assertEqual(usage['context']['tokens'], 17457)
        self.assertEqual(usage['native_session_id'], self.native_id)
        self.assertTrue(self.received.empty(), 'Observations never send requests or approval replies')
        await self.upstream.close()
        await asyncio.wait_for(self.client.wait_closed(), 2)
        self.assertEqual(read(self.directory / 'runtime.json')['usage'], usage)


if __name__ == '__main__':
    unittest.main()
