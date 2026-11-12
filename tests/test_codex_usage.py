"""Exact-thread Codex context accounting with no model calls."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from uuid import uuid4
from unittest.mock import patch

import test_codex_launch as launch_tests
from isolation import isolate
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
        isolate(self)
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

    def test_observed_model_uses_only_the_latest_primary_turn_context(self):
        """Settings, side work or a stale cached turn must never look like the model the main turn used."""
        from multiverse.harnesses.codex import observation, transcripts
        native, side = str(uuid4()), str(uuid4())
        home = self.root / 'codex-home'
        folder = home / 'sessions/2026/11/12'
        folder.mkdir(parents=True)
        path = folder / f'rollout-2026-11-12T10-00-00-{native}.jsonl'
        other = folder / f'rollout-2026-11-12T10-00-00-{side}.jsonl'
        session = replace(self.session, native_id=native, home=str(home),
                          native_versions={'cli': '0.167.2', 'daemon': '0.167.2'})
        write(self.root / 'properties.json', {'native_session_id': native})
        self.reset()
        self.publish(notification(event(native), native))
        header = {'type': 'session_meta', 'payload': {'id': native, 'cli_version': '0.167.2', 'source': 'cli'}}
        def context(model='Provider/Model.raw', effort='xhigh', at='2026-11-12T10:01:00Z'):
            return {'type': 'turn_context', 'timestamp': at, 'payload': {'model': model, 'effort': effort}}
        def emit(rows, *, source='cli'):
            path.write_text(''.join(json.dumps(row) + '\n' for row in [
                {**header, 'payload': {**header['payload'], 'source': source}}, *rows]))
        def observe():
            result = self.adapter.get_usage(session)
            self.assertEqual(result.state, 'ok', result)
            value = result.value
            self.assertEqual(value['context']['tokens'], 17457)
            return {key: value.get(key) for key in ('model', 'effort', 'observed_at')}
        first = context('Earlier', 'low', '2026-11-12T10:00:00Z')
        expected = {'model': 'Provider/Model.raw', 'effort': 'xhigh', 'observed_at': '2026-11-12T10:01:00Z'}
        empty = dict.fromkeys(expected)
        for label, rows, wanted in (
                ('latest context', [first, context()], expected),
                ('missing effort', [first, {**context(), 'payload': {'model': 'Provider/Model.raw'}}],
                 {**expected, 'effort': None}),
                ('settings effort is not turn effort', [first, {**context(), 'payload': {
                    'model': 'Provider/Model.raw', 'reasoning_effort': 'high'}}], {**expected, 'effort': None}),
                ('settings are not a turn', [context(), {'type': 'event_msg', 'timestamp': '2026-11-12T10:02:00Z',
                    'payload': {'type': 'thread_settings_applied', 'thread_id': native,
                                'thread_settings': {'model': 'Not yet used', 'reasoning_effort': 'low'}}}], expected),
                ('review in progress', [context(), {'type': 'event_msg', 'payload': {'type': 'entered_review_mode'}},
                                        context('Review', 'high')], expected),
                ('review finished', [context(), {'type': 'event_msg', 'payload': {'type': 'entered_review_mode'}},
                                    context('Review', 'high'),
                                    {'type': 'event_msg', 'payload': {'type': 'exited_review_mode'}}], expected),
                ('review item lifecycle', [context(), {'type': 'event_msg', 'payload': {'type': 'item_started',
                    'item': {'type': 'EnteredReviewMode'}}}, context('Review', 'high'),
                    {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'ExitedReviewMode'}}}], expected),
                ('new primary after review', [{'type': 'event_msg', 'payload': {'type': 'entered_review_mode'}},
                    context('Review', 'high'), {'type': 'event_msg', 'payload': {'type': 'exited_review_mode'}}, context()], expected),
                ('no timestamp', [first, context(at=None)], empty),
                ('naive timestamp', [first, context(at='2026-11-12T10:01:00')], empty),
                ('bad timestamp', [first, context(at='not a time')], empty),
                ('oversized timestamp', [first, context(at='t' * 100000)], empty),
                ('broken Z timestamp', [first, context(at='2026-11-12T10:01:00Z:00')], empty),
                ('bad model', [first, context(model=None)], empty),
                ('blank model', [first, context(model='  ')], empty),
                ('unprintable model', [first, context(model='model\nother')], empty),
                ('unencodable model', [first, context(model='\ud800')], empty),
                ('oversized model', [first, context(model='m' * 4097)], empty),
                ('bad effort', [first, context(effort=123)], empty),
                ('blank effort', [first, context(effort='  ')], empty)):
            with self.subTest(label=label):
                emit(rows)
                self.assertEqual(observe(), wanted)
        for source in ({'subagent': 'review'}, {'subagent': {'spawn': {'parent_thread_id': native}}}, {'internal': 'review'}):
            with self.subTest(source=source):
                emit([context()], source=source)
                self.assertEqual(observe(), empty)
        emit([context()])
        other.write_text(json.dumps({**header, 'payload': {**header['payload'], 'id': side}}) + '\n'
                         + json.dumps(context('Side', 'high', '2026-11-12T10:05:00Z')) + '\n')
        self.assertEqual(observe(), expected)
        self.assertEqual(self.adapter.get_usage(session).value.get('native_session_id'), native)
        for version in ('0.166.0', '0.167.0', '0.167.1', '0.167.2'):
            with self.subTest(checked_version=version):
                value = self.adapter.get_usage(replace(session,
                    native_versions={'cli': version, 'daemon': version})).value
                self.assertEqual({key: value.get(key) for key in expected}, expected)
        plain = replace(session, native_mode={'mode': 'plain'}, native_versions={'cli': '0.167.2'})
        value = self.adapter.get_usage(plain).value
        self.assertEqual({key: value.get(key) for key in expected}, expected)
        self.assertTrue(value['context']['last_known'])
        for versions in ({}, {'cli': ['0.167.2'], 'daemon': '0.167.2'},
                         {'cli': '0.167.3', 'daemon': '0.167.3'},
                         {'cli': '0.167.2', 'daemon': '0.167.1'}):
            with self.subTest(versions=versions):
                value = self.adapter.get_usage(replace(session, native_versions=versions)).value
                self.assertEqual({key: value.get(key) for key in expected}, empty)
        with patch.object(observation.State, 'row', side_effect=AssertionError('Cached metadata was parsed again')):
            self.assertEqual(observe(), expected)
        emit([context()])
        with path.open('a') as stream:
            stream.write('{"type":"turn_context","payload":{"model":"one","model":"two"}}\n')
        self.assertEqual(observe(), empty, 'Ambiguous latest metadata must not revive an older model')
        with patch.object(observation, 'paths', side_effect=OSError('unreadable model metadata')):
            self.assertEqual(observe(), empty)
        # Exercise the real core filter with the real Codex reading, not a
        # prebuilt usage dictionary that could overlook a missing native ID.
        from multiverse.runtime import native as native_runtime
        emit([context()])
        scope = self.root / 'core-view'
        directory = scope / 'state' / 'agents' / session.agent_id
        properties = {'id': session.agent_id, 'harness': 'codex', 'workspace': session.workspace,
                      'native_session_id': native, 'native_session_home': str(home)}
        for started, wanted in (('2026-11-12T09:00:00Z', expected), ('2026-11-12T11:00:00Z', empty)):
            with self.subTest(core_launch_started=started):
                facts = {**read(self.root / 'runtime.json'), 'started_at': started,
                         'native_versions': dict(session.native_versions), 'native_mode': dict(session.native_mode)}
                write(directory / 'runtime.json', facts)
                with patch.object(native_runtime.harnesses, 'load', return_value=self.adapter):
                    filtered = native_runtime.usage_snapshot(scope, properties, facts, reconciled=True)
                self.assertEqual(filtered['state'], 'ok', filtered)
                self.assertEqual({key: filtered['value'].get(key) for key in expected}, wanted)
                self.assertEqual(filtered['value']['context']['tokens'], 17457)
        self.reset()
        with patch.object(observation, 'read', side_effect=AssertionError('Unavailable usage scanned a rollout')):
            result = self.adapter.get_usage(session)
        self.assertEqual(result.state, 'unavailable')
        self.assertEqual(result.reason, 'Codex has no saved context usage for this thread yet')

    def test_observation_cache_parses_only_changes_without_retaining_rollout_rows(self):
        """Refresh must retain bounded observation state, not repeatedly parse or index every stored tool result."""
        from multiverse.harnesses.codex import observation, transcripts, usage
        observation._states.clear()
        observation._copies.clear()
        transcripts._indexes.clear()
        self.addCleanup(observation._states.clear)
        self.addCleanup(observation._copies.clear)
        native = str(uuid4())
        home = self.root / 'observations'
        folder = home / 'sessions'
        folder.mkdir(parents=True)
        path = folder / f'rollout-2026-11-12T10-00-00-{native}.jsonl'
        session = replace(self.session, native_id=native, home=str(home),
                          native_versions={'cli': '0.169.0', 'daemon': '0.169.0'})
        def encode(row):
            return (json.dumps(row) + '\n').encode()
        header = encode({'type': 'session_meta', 'payload': {'id': native, 'source': 'cli'}})
        def context(model):
            return encode({'type': 'turn_context', 'timestamp': '2026-11-12T10:01:00Z',
                           'payload': {'model': model, 'effort': 'high'}})
        def append(data):
            with path.open('ab') as stream:
                stream.write(data)
        tool = encode({'type': 'response_item', 'payload': {'type': 'function_call_output',
                                                         'output': 'private tool body' * 70000}})
        path.write_bytes(header + tool + context('initial'))
        opened, reads, parsed = [], [], []
        original_row = observation.State.row
        def parse(state, line):
            parsed.append(len(line))
            return original_row(state, line)
        original = observation.open_rollout
        class Tracked:
            def __init__(self, handle):
                self.handle = handle
            def __getattr__(self, key):
                return getattr(self.handle, key)
            def __enter__(self):
                self.handle.__enter__()
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def read(self, size):
                reads.append((self.handle.tell(), size))
                return self.handle.read(size)
            def __iter__(self):
                return self
            def __next__(self):
                start = self.handle.tell()
                line = self.handle.readline()
                reads.append((start, len(line)))
                if not line:
                    raise StopIteration
                return line
        def tracked(candidate):
            opened.append(candidate)
            return Tracked(original(candidate))
        def observe():
            return usage.observation(session, bridge=True)
        with patch.object(observation, 'open_rollout', side_effect=tracked), patch.object(
                observation.State, 'row', new=parse), patch.object(
                transcripts, 'read_copy', side_effect=AssertionError('Usage filled the transcript index')):
            self.assertEqual(observe()['model'], 'initial')
            self.assertEqual(len(opened), 1)
            for _ in range(10):
                self.assertEqual(observe()['model'], 'initial')
            self.assertEqual(len(opened), 1, 'Unchanged refresh must not even open a rollout')
            old_end = path.stat().st_size
            reads.clear()
            parsed.clear()
            append(context('appended'))
            self.assertEqual(observe()['model'], 'appended')
            self.assertTrue(reads)
            self.assertEqual(len(parsed), 1, 'Only appended rows may be parsed after a full byte proof')
            proof = [(offset, size) for offset, size in reads if offset < old_end - observation.TAIL_BYTES]
            self.assertTrue(proof, 'Growth must reprove the complete prior prefix')
            self.assertEqual(proof[0][0], 0)
            self.assertTrue(all(size <= 65536 for _, size in proof))
            self.assertGreaterEqual(sum(size for _, size in reads), old_end)
            partial = context('completed')
            append(partial[:-1])
            self.assertEqual(observe()['model'], 'appended')
            append(b'\n')
            self.assertEqual(observe()['model'], 'completed')
            enter = encode({'type': 'event_msg', 'payload': {'type': 'entered_review_mode'}})
            leave = encode({'type': 'event_msg', 'payload': {'type': 'exited_review_mode'}})
            append(enter + context('review') + b'not-json\n')
            self.assertEqual(observe()['model'], 'completed')
            append(leave + b'not-json\n')
            self.assertEqual(observe(), {}, 'Damage outside review clears the latest context')
            append(context('recovered'))
            self.assertEqual(observe()['model'], 'recovered')
            append(encode({'type': 'turn_context', 'timestamp': 't' * 100000,
                           'payload': {'model': 'bounded'}}))
            self.assertEqual(observe(), {})
            self.assertIsNone(observation._states[path].latest[2])
            # A grown file whose old end was changed is a rewrite, not an append.
            path.write_bytes(header + tool + context('rewritten') + b' ' * (path.stat().st_size + 16000) + b'\n' + context('new tip'))
            reads.clear()
            self.assertEqual(observe()['model'], 'new tip')
            self.assertTrue(any(offset == 0 for offset, _ in reads))
            path.write_bytes(header + context('shorter'))
            self.assertEqual(observe()['model'], 'shorter')
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(header + context('replaced'))
            replacement.replace(path)
            self.assertEqual(observe()['model'], 'replaced')
        # A growing writer can rewrite any earlier metadata while preserving
        # the old end. Every cached decision therefore needs the full byte proof.
        enter = encode({'type': 'event_msg', 'payload': {'type': 'entered_review_mode'}})
        ignored = encode({'type': 'event_msg', 'payload': {'type': 'ignored_review_mode'}})
        prefix_cases = (
            ('foreign identity', header + context('owned'),
             header.replace(native.encode(), str(uuid4()).encode()) + context('owned'), context('foreign'), {}),
            ('nonprimary source', header + context('owned'),
             header.replace(b'"cli"', b'"bad"') + context('owned'), context('foreign'), {}),
            ('review began earlier', header + context('owned') + ignored,
             header + context('owned') + enter, context('review'), {'model': 'owned'}),
            ('review marker removed', header + context('owned') + enter,
             header + context('owned') + ignored, context('after'), {'model': 'after'}),
            ('latest context rewritten', header + context('owned'),
             header + context('other'), encode({'type': 'event_msg', 'payload': {'type': 'noop'}}), {'model': 'other'}))
        for label, before, after, extra, wanted in prefix_cases:
            with self.subTest(growing_rewrite=label):
                self.assertEqual(len(before), len(after))
                original_bytes = before + tool
                path.write_bytes(original_bytes)
                self.assertEqual(observe()['model'], 'owned')
                changed = after + tool + extra
                self.assertEqual(original_bytes[-observation.TAIL_BYTES:],
                                 changed[len(original_bytes)-observation.TAIL_BYTES:len(original_bytes)])
                inode = path.stat().st_ino
                path.write_bytes(changed)
                self.assertEqual(path.stat().st_ino, inode)
                value = observe()
                self.assertEqual({key: value.get(key) for key in wanted} if wanted else value, wanted)
        path.write_bytes(header + context('bounded'))
        self.assertEqual(observe()['model'], 'bounded')
        self.assertEqual(transcripts._indexes, {})
        state = observation._states[path]
        self.assertFalse(any(isinstance(value, (list, dict)) for value in vars(state).values()))
        self.assertFalse(any('private tool body' in repr(value) for value in vars(state).values()))
        self.assertNotIn('tail', vars(state), 'Only a fixed digest of old-end bytes may be retained')
        for index in range(observation.CACHE_LIMIT + 2):
            other = str(uuid4())
            candidate = folder / f'rollout-2026-11-12T10-00-00-{other}.jsonl'
            candidate.write_bytes(encode({'type': 'session_meta', 'payload': {'id': other}}) + context('small'))
            self.assertEqual(usage.observation(replace(session, native_id=other), bridge=True)['model'], 'small')
        self.assertEqual(len(observation._states), observation.CACHE_LIMIT)
        self.assertEqual(len(observation._copies), observation.CACHE_LIMIT)
        self.assertEqual(transcripts._indexes, {})

    def test_observation_copy_identity_and_compressed_changes_are_reproved(self):
        """Cached observations must keep exact-copy identity and reject changed or inconsistent stored evidence."""
        try:
            import zstandard
        except ImportError:
            self.skipTest('zstandard is not installed; compressed observation fixtures require it')
        from multiverse.harnesses.codex import observation, usage
        observation._states.clear()
        observation._copies.clear()
        self.addCleanup(observation._states.clear)
        self.addCleanup(observation._copies.clear)
        native = str(uuid4())
        folder = self.root / 'observation-copies' / 'sessions'
        folder.mkdir(parents=True)
        path = folder / f'rollout-2026-11-12T10-00-00-{native}.jsonl.zst'
        session = replace(self.session, native_id=native, home=str(folder.parent),
                          native_versions={'cli': '0.167.2', 'daemon': '0.167.2'})
        def encode(rows):
            return ''.join(json.dumps(row) + '\n' for row in rows).encode()
        header = {'type': 'session_meta', 'payload': {'id': native, 'source': 'cli'}}
        def context(model):
            return {'type': 'turn_context', 'timestamp': '2026-11-12T10:01:00Z', 'payload': {'model': model}}
        def compressed(rows):
            path.write_bytes(zstandard.ZstdCompressor().compress(encode(rows)))
        def observe():
            return usage.observation(session, bridge=True)
        compressed([header, context('first')])
        with patch.object(observation, 'open_rollout', wraps=observation.open_rollout) as opened:
            self.assertEqual(observe()['model'], 'first')
            self.assertEqual(observe()['model'], 'first')
            self.assertEqual(opened.call_count, 1)
            compressed([header, context('second')])
            self.assertEqual(observe()['model'], 'second')
            self.assertEqual(opened.call_count, 2)
        # Empty/invalid values and stable decoder failures are also cacheable.
        # Otherwise a broken cold rollout is decompressed on every refresh.
        for label, data in (
                ('bad timestamp', zstandard.ZstdCompressor().compress(encode([
                    header, {**context('invalid'), 'timestamp': 'not a timestamp'}]))),
                ('empty rollout', zstandard.ZstdCompressor().compress(b'')),
                ('corrupt compressed frame', b'not a zstd frame')):
            with self.subTest(stable_failure=label):
                observation._copies.clear()
                path.write_bytes(data)
                with patch.object(observation, 'open_rollout', wraps=observation.open_rollout) as opened:
                    self.assertEqual(observe(), {})
                    self.assertEqual(observe(), {})
                    self.assertEqual(opened.call_count, 1, 'Stable failed evidence must not be reopened')
                    self.assertIn((str(folder.parent), native), observation._copies)
                with patch.object(observation, 'open_rollout', side_effect=AssertionError('Failed stamp read again')):
                    self.assertEqual(observe(), {})
        original = observation.open_rollout
        class ExitFailure:
            def __init__(self, handle):
                self.handle = handle
            def __getattr__(self, key):
                return getattr(self.handle, key)
            def __enter__(self):
                self.handle.__enter__()
                return self.handle
            def __exit__(self, *args):
                self.handle.__exit__(*args)
                raise OSError('synthetic decoder failure after stream exit')
        compressed([header, context('must not survive failed decoder exit')])
        with patch.object(observation, 'open_rollout', side_effect=lambda candidate: ExitFailure(original(candidate))) as opened:
            self.assertEqual(observe(), {})
            self.assertEqual(observe(), {})
            self.assertEqual(opened.call_count, 1)
        observation._copies.clear()
        with patch.object(observation, 'scan', side_effect=observation.Unstable('changed during scan')) as scanned:
            self.assertEqual(observe(), {})
            self.assertEqual(observe(), {})
            self.assertEqual(scanned.call_count, 2, 'An unstable scan cannot be cached as an empty observation')
        self.assertEqual(observation._copies, {})
        with patch.object(observation, 'paths', side_effect=OSError('no discovery stamps')) as discovered:
            self.assertEqual(observe(), {})
            self.assertEqual(observe(), {})
            self.assertEqual(discovered.call_count, 2)
        self.assertEqual(observation._copies, {})
        original_paths = observation.paths
        discovery_calls = []
        def changed_paths(home, native_id):
            discovery_calls.append(None)
            return original_paths(home, native_id) if len(discovery_calls) == 1 else []
        with patch.object(observation, 'paths', side_effect=changed_paths):
            self.assertEqual(observe(), {}, 'A changed copy set cannot publish even a completed scan')
        self.assertEqual(len(discovery_calls), 2)
        self.assertEqual(observation._copies, {})
        plain = path.with_suffix('')
        plain.write_bytes(encode([header, context('plain wins')]))
        self.assertEqual(observe()['model'], 'plain wins')
        archive = folder.parent / 'archived_sessions'
        archive.mkdir()
        older = archive / plain.name
        shared = encode([header, {'type': 'response_item', 'payload': {
            'type': 'function_call_output', 'output': 'x' * 20000}}, context('plain wins')])
        plain.write_bytes(shared)
        older.write_bytes(shared)
        os.utime(older, ns=(1, 1))
        self.assertEqual(observe()['model'], 'plain wins')
        changed = shared.replace(b'x' * 20000, b'y' + b'x' * 19999, 1)
        self.assertEqual(shared[-observation.TAIL_BYTES:], changed[-observation.TAIL_BYTES:])
        suffix = encode([context('new tip')])
        plain.write_bytes(shared + suffix)
        older.write_bytes(changed + suffix)
        os.utime(older, ns=(1, 1))
        self.assertEqual(observe(), {}, 'Growing copies must still agree on every earlier byte')
        plain.write_bytes(encode([header, context('plain wins')]))
        older.write_bytes(encode([header]))
        os.utime(older, ns=(1, 1))
        self.assertEqual(observe()['model'], 'plain wins')
        with patch.object(observation, 'open_rollout', side_effect=AssertionError('Unchanged copies read again')):
            self.assertEqual(observe()['model'], 'plain wins')
        invalid_rows = (
                ('divergent prefix', [header, context('different')]),
                ('wrong identity', [{**header, 'payload': {'id': str(uuid4())}}]),
                ('later wrong identity', [header, {'type': 'session_meta', 'payload': {'id': str(uuid4())}}]),
                ('side source', [{**header, 'payload': {'id': native, 'source': {'subagent': 'review'}}}]),
                ('history base', [{**header, 'payload': {'id': native, 'history_base': 'base'}}]),
                ('ambiguous header', []))
        for label, rows in invalid_rows:
            with self.subTest(label=label):
                older.write_bytes(encode(rows))
                os.utime(older, ns=(1, 1))
                self.assertEqual(observe(), {})
        older.unlink()
        for label, rows in invalid_rows[1:]:
            with self.subTest(single_copy=label):
                plain.write_bytes(encode(rows + [context('must not be used')]))
                self.assertEqual(observe(), {})
        plain.write_bytes(b'{"type":"session_meta","type":"turn_context","payload":{}}\n'
                          + encode([context('must not be used')]))
        self.assertEqual(observe(), {})
        original = observation.open_rollout
        class Changed:
            def __init__(self, handle):
                self.handle = handle
            def __getattr__(self, key):
                return getattr(self.handle, key)
            def __enter__(self):
                self.handle.__enter__()
                return self.handle
            def __exit__(self, *args):
                self.handle.__exit__(*args)
                plain.write_bytes(encode([header, context('concurrent rewrite')]))
        observation._copies.clear()
        plain.write_bytes(encode([header, context('before rewrite')]))
        with patch.object(observation, 'open_rollout', side_effect=lambda candidate: Changed(original(candidate))):
            self.assertEqual(observe(), {}, 'A changed path cannot publish a mixed scan')
            self.assertNotIn((str(folder.parent), native), observation._copies)
        self.assertEqual(observe()['model'], 'concurrent rewrite')

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
        with patch('multiverse.runtime.runner.mutate_runtime', side_effect=OSError('read-only runtime')):
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
