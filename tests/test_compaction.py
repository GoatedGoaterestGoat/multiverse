"""Harness-neutral tree compaction: settings, schedulers, trees, builds, one compaction and its entry points.

A fake CompactionSource stands in for a harness, so these tests exercise exactly
the protocol the generic code relies on, with no model or harness process.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse.compaction import __main__ as entry, config, pipeline, schedule
from multiverse.compaction.build import Build, BuildFailed, Cancelled, Reporter
from multiverse.compaction.model import Leaf, Node, Turn, tree_from_record, tree_record
from multiverse.compaction.protocol import Merge, MergeError
from multiverse.compaction.store import Store
from multiverse.store import write
from test_multiverse import BaseTest

FAST = config.Builder(rate_limit=config.RateLimit(retries=3, first=0.01, multiplier=2, maximum=0.05))


def leaf(name, tokens=100, kind='text'):
    return Leaf(kind, (name,), tokens, preview=name, content={'text': name})


def turn(name, count=4, tokens=100):
    return Turn(name, leaf(f'{name}-prompt', 5, 'user_text'), [leaf(f'{name}-{i}', tokens) for i in range(count)])


class FakeSource:
    """A harness whose model always merges the first two entries, unless told to fail first."""

    harness = 'fake'
    keeps_images = True
    builder_defaults = {'model': 'fake-model'}

    def __init__(self, turns=(), failures=()):
        self._turns = list(turns)
        self.failures = list(failures)  # MergeErrors to raise, in order, before succeeding
        self.calls = 0
        self.rendered = None
        self.cancelled = False

    def unavailable(self):
        return None

    def check_builder(self, builder):
        if builder.options:
            raise ValueError(f'unknown fake settings: {sorted(builder.options)}')

    def turns(self):
        return [Turn(t.id, t.prompt, list(t.leaves), dropped=t.dropped, omitted=t.omitted) for t in self._turns]

    def completed(self, turns, marker):
        return turns if marker == 'all' else turns[:-1]

    def merge(self, turn, frontier, builder):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return Merge(0, 2, f'summary of {len(frontier)}')

    def render(self, turns, frontiers, offered, images):
        self.rendered = {t.id: frontiers[t.id] for t in turns}
        self.images = images
        messages = [{'text': getattr(entry, 'summary', None) or entry.preview} for t in turns for entry in frontiers[t.id]]
        return {'messages': messages, 'count': len(messages)}

    def cancel(self):
        self.cancelled = True


class Events:
    def __init__(self):
        self.items = []

    def __call__(self, event):
        self.items.append(event)

    def of(self, name):
        return [event for event in self.items if event['event'] == name]


class SettingsTest(BaseTest):
    def test_missing_file_means_compaction_is_off(self):
        (self.root / 'compaction.json').unlink(missing_ok=True)
        settings = config.load(self.root).for_harness('claude')
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.default, config.Schedule('last_k_raw', 0, 2))
        self.assertEqual((settings.builder.model, settings.builder.effort, settings.builder.options), (None, None, {}))
        built = config.load(self.root).for_harness('x', {'model': 'm', 'fallback_model': 'f', 'effort': 'e', 'own': 1}).builder
        self.assertEqual((built.model, built.fallback_model, built.effort, built.options), ('m', 'f', 'e', {'own': 1}))

    def test_the_templates_give_claude_its_builder_and_nothing_shared(self):
        from multiverse.harnesses.claude.compaction import ClaudeCompaction
        templates = Path(config.__file__).resolve().parents[1] / 'templates'
        for name, enabled in (('base', False), ('development', True)):
            with self.subTest(template=name):
                value = json.loads((templates / name / config.FILE).read_text())
                self.assertNotIn('builder', value)
                loaded = config.parse(value).for_harness('claude', ClaudeCompaction.builder_defaults)
                self.assertEqual(loaded.enabled, enabled)
                self.assertEqual(value['harnesses']['claude']['builder']['model'], ClaudeCompaction.builder_defaults['model'])

    def test_each_harness_has_its_own_builder_and_may_replace_the_shared_settings(self):
        write(self.root / 'compaction.json', {
            'schema': 1, 'enabled': False, 'default': {'scheduler': 'proportional', 'budget': 900},
            'harnesses': {'claude': {'enabled': True, 'builder': {
                              'effort': 'high', 'claude_max_retries': 5,
                              'rate_limit': {'retries': 4, 'wait_seconds': {'first': 5, 'max': 60}}}},
                          'codex': {'default': {'scheduler': 'uniform', 'budget': 10}, 'builder': {'model': 'gpt-x'}}}})
        loaded = config.load(self.root)
        claude = loaded.for_harness('claude', {'model': 'sonnet', 'effort': 'medium', 'claude_max_retries': 3})
        codex = loaded.for_harness('codex', {'model': 'codex-default', 'effort': 'medium'})
        pi = loaded.for_harness('pi')
        self.assertEqual([claude.enabled, codex.enabled, pi.enabled], [True, False, False])
        self.assertEqual((claude.builder.model, claude.builder.effort, claude.builder.options),
                         ('sonnet', 'high', {'claude_max_retries': 5}))
        self.assertEqual((claude.builder.rate_limit.retries, claude.builder.rate_limit.first,
                          claude.builder.rate_limit.maximum), (4, 5.0, 60.0))
        self.assertEqual(claude.default.scheduler, 'proportional')
        self.assertEqual((codex.builder.model, codex.builder.effort, codex.builder.options), ('gpt-x', 'medium', {}))
        self.assertEqual(codex.builder.rate_limit, config.RateLimit())
        self.assertEqual(codex.default, config.Schedule('uniform', 10))
        self.assertEqual(config.describe(claude)['builder']['claude_max_retries'], 5)

    def test_mistakes_are_reported_with_their_place(self):
        cases = [({'enabled': 'yes'}, 'enabled'), ({'default': {'scheduler': 'nope'}}, 'Unknown scheduler'),
                 ({'default': {'scheduler': 'uniform', 'k': 2}}, 'does not take'),
                 ({'builder': {'model': 'sonnet'}}, 'belong to one harness'),
                 ({'harnesses': {'claude': {'builder': {'concurrency': 0}}}}, 'concurrency'),
                 ({'harnesses': {'claude': {'builder': {'rate_limit': {'claude_max_retries': 3}}}}}, 'rate_limit'),
                 ({'harnesses': {'claude': {'builder': {'rate_limit': {'wait_seconds': {'first': 10, 'max': 5}}}}}}, 'max'),
                 ({'harnesses': {'claude': {'builder': {'model': ''}}}}, 'harnesses.claude'),
                 ({'harnesses': {'claude': {'builder': {'fallback_model': 5}}}}, 'fallback_model'),
                 ({'colour': 'red'}, 'Unknown')]
        for value, expected in cases:
            with self.subTest(value=value):
                write(self.root / 'compaction.json', value)
                with self.assertRaisesRegex(ValueError, expected):
                    config.load(self.root)

    def test_an_agent_follows_the_default_until_given_its_own_scheduler(self):
        agent = self.root / 'state/agents/a'
        settings = config.Settings(True, config.Schedule('last_k_raw', 0, 2))
        self.assertEqual(config.schedule_for(agent, settings), (settings.default, 'universe'))
        config.save_schedule(agent, config.Schedule('recency_boosted', 500, recency_ratio=2.0))
        mine, origin = config.schedule_for(agent, settings)
        self.assertEqual((mine.scheduler, mine.budget, mine.recency_ratio, origin), ('recency_boosted', 500, 2.0, 'agent'))
        self.assertEqual(json.loads(config.agent_file(agent).read_text()),
                         {'scheduler': 'recency_boosted', 'budget': 500, 'recency_ratio': 2.0, 'recency_half_life': 10.0})
        config.save_schedule(agent, None)
        self.assertEqual(config.schedule_for(agent, settings)[1], 'universe')

    def test_waits_grow_to_their_cap_and_honor_retry_after(self):
        limits = config.RateLimit(first=20, multiplier=2, maximum=300)
        self.assertEqual([limits.wait(attempt) for attempt in range(1, 7)], [20, 40, 80, 160, 300, 300])
        self.assertEqual(limits.wait(1, retry_after=90), 90)
        self.assertEqual(limits.wait(1, retry_after=9000), 300)


class ScheduleTest(unittest.TestCase):
    def test_last_k_raw_keeps_the_tail_whole_even_at_budget_zero(self):
        turns = [turn(f't{i}') for i in range(5)]
        shares, _ = schedule.allocate(turns, config.Schedule('last_k_raw', 0, 2))
        self.assertEqual([shares[t.id] for t in turns], [0, 0, 0, 400, 400])
        self.assertEqual([schedule.needs_tree(t, shares[t.id]) for t in turns], [True, True, True, False, False])
        shares, _ = schedule.allocate(turns, config.Schedule('last_k_raw', 300, 0))
        self.assertEqual(sum(shares.values()), 300 - 5 * 5)  # the prompts' text comes off the top

    def test_the_budget_covers_prompts_and_the_raw_tail_as_in_v1(self):
        turns = [turn(f't{i}') for i in range(4)]
        shares, _ = schedule.allocate(turns, config.Schedule('last_k_raw', 1000, 2))
        self.assertEqual([shares[t.id] for t in turns], [90, 90, 400, 400])  # (1000 - 4 * 5 - 800) / 2

    def test_prompt_images_are_kept_newest_first_while_they_fit(self):
        turns = [turn(f't{i}') for i in range(4)]
        for t in turns[:3]:
            t.prompt = Leaf('user_text', (f'{t.id}-prompt',), 305, images=300)
        cases = [(config.Schedule('proportional', 1300), {'t1', 't2'}),  # half of 1300 - 20 fits two
                 (config.Schedule('proportional', 1300, image_mode='keep_all'), {'t0', 't1', 't2'}),
                 (config.Schedule('proportional', 1300, image_mode='drop_all'), set()),
                 (config.Schedule('last_k_raw', 0, 2), {'t2'})]  # the raw tail keeps its own
        for value, kept in cases:
            with self.subTest(schedule=value):
                shares, images = schedule.allocate(turns, value)
                self.assertEqual(images, kept)
        shares, _ = schedule.allocate(turns, config.Schedule('proportional', 1300))
        self.assertLessEqual(sum(shares.values()), 1300 - 20 - 600)

    def test_every_scheduler_stays_within_its_budget(self):
        turns = [turn('a', 2), turn('b', 8), turn('c', 4, tokens=500)]
        for name in ('proportional', 'uniform', 'recency_boosted'):
            with self.subTest(name=name):
                shares, _ = schedule.allocate(turns, config.Schedule(name, 1000))
                self.assertLessEqual(sum(shares.values()), 1000)
                self.assertEqual(set(shares), {'a', 'b', 'c'})
        boosted, _ = schedule.allocate([turn('old'), turn('new')], config.Schedule('recency_boosted', 1000))
        self.assertGreater(boosted['new'], boosted['old'])

    def test_expansion_undoes_the_newest_merges_that_fit(self):
        leaves = [leaf(f'l{i}', 100) for i in range(4)]
        first = Node(0, leaves[:2], 'a', 10)
        second = Node(1, leaves[2:], 'b', 10)
        root = Node(2, [first, second], 'root', 5)
        self.assertEqual(schedule.expand(root, 0), [root])
        self.assertEqual(schedule.expand(root, 20), [first, second])
        self.assertEqual(schedule.expand(root, 210), [first, *leaves[2:]])
        self.assertEqual(schedule.expand(root, 400), leaves)
        whole = Turn('t', None, leaves, root)
        self.assertEqual(schedule.frontier_for(whole, 400), leaves)
        with self.assertRaisesRegex(ValueError, 'needs a summary tree'):
            schedule.frontier_for(Turn('t', None, leaves), 10)


class TreeRecordTest(unittest.TestCase):
    def test_trees_store_leaves_by_reference_and_go_stale_with_their_turn(self):
        t = turn('t', 3)
        node = Node(0, t.leaves[:2], 'first two', 3)
        root = Node(1, [node, t.leaves[2]], 'all', 2)
        record = tree_record(t, root)
        self.assertNotIn('content', json.dumps(record))
        self.assertEqual(record['leaves'][0], {'kind': 'text', 'refs': ['t-0'], 'tokens': 100})
        rebuilt = tree_from_record(turn('t', 3), record)
        self.assertEqual([child.summary if isinstance(child, Node) else child.refs for child in rebuilt.children],
                         ['first two', ('t-2',)])
        self.assertIsNone(tree_from_record(turn('t', 4), record))
        changed = turn('t', 3)
        changed.leaves[1] = leaf('other')
        self.assertIsNone(tree_from_record(changed, record))


class StoreTest(BaseTest):
    def test_locks_are_exclusive_and_a_dead_holders_lock_is_cleared(self):
        store = Store(self.root / 'state/agents/a')
        held = store.turn_lock('turn', 'test')
        self.assertIsNotNone(held)
        self.assertIsNone(store.turn_lock('turn', 'test'))
        self.assertEqual(store.turn_holder('turn')['purpose'], 'test')
        held.release()
        self.assertIsNone(store.turn_holder('turn'))
        finished = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
        lock = store.trees / 'turn.lock'
        lock.write_text(json.dumps({'pid': int(finished.stdout), 'host': os.uname().nodename, 'token': 'old'}))
        self.assertIsNotNone(store.turn_lock('turn', 'test'))

    def test_status_keeps_recent_errors_and_failures(self):
        store = Store(self.root / 'state/agents/a')
        for index in range(25):
            store.add_error({'message': f'e{index}'})
        self.assertEqual([error['message'] for error in store.status()['errors']][-1], 'e24')
        self.assertEqual(len(store.status()['errors']), 20)
        store.set_failure('t', 'refused')
        store.set_failure('t', 'refused again')
        self.assertEqual(store.status()['failures']['t']['count'], 2)
        self.assertIsNotNone(store.recent_failure('t', 60))
        store.set_failure('t', None)
        self.assertIsNone(store.recent_failure('t', 60))


class BuildTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / 'state/agents/a')
        self.events = Events()
        self.reporter = Reporter(self.store, self.events)

    def test_a_tree_is_built_merge_by_merge_and_cached(self):
        source = FakeSource([turn('t', 4)])
        t = source.turns()[0]
        self.assertEqual(Build(source, self.store, FAST, self.reporter).turns([t]), {})
        self.assertEqual(source.calls, 3)
        self.assertEqual(t.root.id, 2)
        self.assertEqual(t.root.children[-1].refs, ('t-3',))  # always merging the first two entries
        self.assertEqual([child.id for child in t.root.children[:1]], [1])
        again = source.turns()[0]
        self.assertTrue(self.store.load_tree(again))
        self.assertEqual(again.root.summary, t.root.summary)
        self.assertEqual(len(self.events.of('progress')), 4)

    def test_rate_limits_wait_and_retry_the_same_step(self):
        source = FakeSource([turn('t', 3)], [MergeError('rate_limit', 'HTTP 429'), MergeError('rate_limit', 'overloaded', 0.02)])
        t = source.turns()[0]
        start = time.monotonic()
        self.assertEqual(Build(source, self.store, FAST, self.reporter).turns([t]), {})
        self.assertGreaterEqual(time.monotonic() - start, 0.03)
        errors = self.events.of('error')
        self.assertEqual([(e['kind'], e['retrying'], e['attempt'], e['wait']) for e in errors],
                         [('rate_limit', True, 1, 0.01), ('rate_limit', True, 2, 0.02)])
        self.assertIn('rate_limit', self.store.status()['errors'][0]['kind'])
        self.assertIn('retry 1/3', (self.store.directory / 'build.log').read_text())

    def test_running_out_of_rate_limit_retries_fails_the_turn_without_turn_retries(self):
        source = FakeSource([turn('t', 3)], [MergeError('rate_limit', 'HTTP 429')] * 10)
        failures = Build(source, self.store, FAST, self.reporter).turns(source.turns())
        self.assertIn('still rate limited after 3 retries', failures['t'])
        self.assertEqual(source.calls, 4)
        self.assertIn('t', self.store.status()['failures'])

    def test_other_failures_retry_at_once_from_the_frontier_reached(self):
        source = FakeSource([turn('t', 4)])
        original = source.merge
        sequence = iter([None, MergeError('refused', 'no'), MergeError('invalid', 'not json'), None, None])

        def merge(t, frontier, builder):
            failure = next(sequence)
            if failure:
                raise failure
            return original(t, frontier, builder)
        source.merge = merge
        t = source.turns()[0]
        self.assertEqual(Build(source, self.store, FAST, self.reporter).turns([t]), {})
        self.assertEqual(t.root.id, 2)
        self.assertEqual([(e['kind'], e['retrying'], e['attempt']) for e in self.events.of('error')],
                         [('refused', True, 1), ('invalid', True, 2)])

    def test_a_turn_fails_after_its_retries_and_an_invalid_span_counts(self):
        source = FakeSource([turn('t', 3)])
        source.merge = lambda t, frontier, builder: Merge(0, 1, 'too small')
        failures = Build(source, self.store, FAST, self.reporter).turns(source.turns())
        self.assertIn('invalid', failures['t'])
        self.assertEqual([e['retrying'] for e in self.events.of('error')], [True, True, True, False])

    def test_a_turn_still_refused_after_its_retries_continues_with_the_fallback_model(self):
        source = FakeSource([turn('t', 4)])
        original, models = source.merge, []
        refusals = iter([None, 'refused', 'refused', None, None])

        def merge(t, frontier, builder):
            models.append(builder.model)
            if next(refusals):
                raise MergeError('refused', 'no')
            return original(t, frontier, builder)
        source.merge = merge
        builder = config.Builder(model='strong', fallback_model='lenient', turn_retries=1, rate_limit=FAST.rate_limit)
        t = source.turns()[0]
        self.assertEqual(Build(source, self.store, builder, self.reporter).turns([t]), {})
        self.assertEqual(models, ['strong', 'strong', 'strong', 'lenient', 'lenient'])
        self.assertIn('continuing this turn with lenient', (self.store.directory / 'build.log').read_text())
        stored = json.loads(self.store.tree_path('t').read_text())
        self.assertEqual([merge['model'] for merge in stored['merges']], ['strong', 'lenient', 'lenient'])
        source = FakeSource([turn('u', 3)], [MergeError('refused', 'no')] * 4)
        failures = Build(source, self.store, config.Builder(fallback_model=None, turn_retries=1, rate_limit=FAST.rate_limit),
                         self.reporter).turns(source.turns())
        self.assertIn('refused', failures['u'])
        self.assertEqual(source.calls, 2)

    def test_usage_limits_are_not_retried(self):
        source = FakeSource([turn('t', 3)], [MergeError('usage_limit', 'limit reached')])
        failures = Build(source, self.store, FAST, self.reporter).turns(source.turns())
        self.assertIn('usage_limit', failures['t'])
        self.assertEqual(source.calls, 1)

    def test_a_turn_another_builder_holds_is_waited_for_or_left(self):
        source = FakeSource([turn('t', 3)])
        t = source.turns()[0]
        held = self.store.turn_lock('t', 'background')
        self.assertEqual(Build(source, self.store, FAST, self.reporter).turns([t], wait=False), {})
        self.assertIsNone(t.root)

        def finish():
            time.sleep(0.3)
            other = source.turns()[0]
            node = Node(0, other.leaves, 'built elsewhere', 3)
            self.store.save_tree(other, node)
            held.release()
        threading.Thread(target=finish).start()
        self.assertEqual(Build(source, self.store, FAST, self.reporter).turns([t]), {})
        self.assertEqual(t.root.summary, 'built elsewhere')
        self.assertEqual(source.calls, 0)
        self.assertIn('waiting', [event['phase'] for event in self.events.of('progress')])

    def test_cancel_stops_a_wait(self):
        source = FakeSource([turn('t', 3)], [MergeError('rate_limit', '429')])
        cancel = threading.Event()
        slow = config.Builder(rate_limit=config.RateLimit(retries=3, first=30, maximum=30))
        threading.Timer(0.2, cancel.set).start()
        with self.assertRaises(Cancelled):
            Build(source, self.store, slow, self.reporter, cancel).turns(source.turns())

    def test_turns_build_in_parallel_up_to_the_concurrency(self):
        active, peak, lock = [0], [0], threading.Lock()
        source = FakeSource([turn(f't{i}', 3) for i in range(4)])
        original = source.merge

        def merge(t, frontier, builder):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return original(t, frontier, builder)
        source.merge = merge
        Build(source, self.store, config.Builder(concurrency=2), self.reporter).turns(source.turns())
        self.assertEqual(peak[0], 2)
        self.assertEqual(self.store.tree_count(), 4)


class PipelineTest(BaseTest):
    def agent(self, source, schedule_=None):
        settings = config.Settings(True, schedule_ or config.Schedule('last_k_raw', 0, 2), FAST)
        return pipeline.Agent(self.root, self.root / 'state/agents/a', 'fake', settings, source)

    def test_compaction_builds_only_missing_trees_and_keeps_the_last_turns_raw(self):
        source = FakeSource([turn(f't{i}', 3) for i in range(4)])
        payload, stats = pipeline.compact(self.agent(source), [], Reporter(Store(self.root / 'state/agents/a')))
        self.assertEqual(source.calls, 4)  # two summarized turns of three leaves each
        self.assertEqual([len(source.rendered[f't{i}']) for i in range(4)], [1, 1, 3, 3])
        self.assertIsInstance(source.rendered['t0'][0], Node)
        self.assertEqual(payload['count'], 8)
        self.assertEqual((stats['turns'], stats['summarized']), (4, 2))
        self.assertLess(stats['tokens_after'], stats['tokens_before'])
        source.calls = 0
        pipeline.compact(self.agent(source), [], Reporter(Store(self.root / 'state/agents/a')))
        self.assertEqual(source.calls, 0)

    def test_turns_the_harness_drops_are_neither_built_nor_rendered(self):
        turns = [turn(f't{i}', 3) for i in range(4)]
        turns[1].dropped = 'the display-only command /context'
        source = FakeSource(turns)
        payload, stats = pipeline.compact(self.agent(source), [], Reporter(Store(self.root / 'state/agents/a')))
        self.assertEqual(sorted(source.rendered), ['t0', 't2', 't3'])
        self.assertEqual(source.calls, 2)  # only t0 is summarized; t2 and t3 stay raw
        self.assertEqual((stats['turns'], stats['dropped_turns']), (3, 1))

    def test_omitted_content_counts_before_compaction_and_never_after(self):
        turns = [turn(f't{i}', 3) for i in range(3)]
        for t in turns:
            t.omitted = 1000
        payload, stats = pipeline.compact(self.agent(FakeSource(turns)), [], Reporter(Store(self.root / 'state/agents/a')))
        self.assertEqual(stats['tokens_before'], 3 * (5 + 300 + 1000))
        self.assertLess(stats['tokens_after'], 3 * (5 + 300))

    def test_a_turn_without_a_tree_fails_the_whole_compaction(self):
        source = FakeSource([turn(f't{i}', 3) for i in range(3)], [MergeError('usage_limit', 'limit reached')])
        with self.assertRaisesRegex(pipeline.CompactionError, 'Could not build 1 summary tree'):
            pipeline.compact(self.agent(source), [], Reporter(Store(self.root / 'state/agents/a')))
        self.assertIsNone(source.rendered)

    def test_nothing_to_compact_is_an_error(self):
        with self.assertRaisesRegex(pipeline.CompactionError, 'no conversation'):
            pipeline.compact(self.agent(FakeSource([])), [], Reporter(Store(self.root / 'state/agents/a')))

    def test_plan_reports_each_turns_share(self):
        rows = pipeline.plan([turn('a', 3), turn('b', 3)], config.Schedule('last_k_raw', 0, 1))
        self.assertEqual([(row['turn'], row['raw'], row['needs_tree']) for row in rows], [('a', False, True), ('b', True, False)])


class EntryPointTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents/a'
        self.directory.mkdir(parents=True)

    def patched(self, source, enabled=True, background=True):
        settings = config.Settings(enabled, config.Schedule('last_k_raw', 0, 1),
                                   config.Builder(background=background, rate_limit=FAST.rate_limit))
        agent = pipeline.Agent(self.root, self.directory, 'fake', settings, source)
        return patch.object(entry, '_agent', return_value=agent)

    def test_install_streams_progress_then_messages_then_the_result(self):
        events = Events()
        with self.patched(FakeSource([turn('t0', 3), turn('t1', 2)])):
            self.assertEqual(entry.install(str(self.directory), {'trigger': 'manual'}, events), 0)
        kinds = [event['event'] for event in events.items]
        self.assertEqual(kinds[-1], 'result')
        self.assertEqual(kinds.count('message'), events.items[-1]['count'])
        self.assertLess(kinds.index('progress'), kinds.index('message'))
        payload = json.loads(Path(events.items[-1]['path']).read_text())
        self.assertEqual(payload['install_id'], events.items[-1]['install_id'])
        self.assertIsNone(Store(self.directory).status()['activity'])

    def test_install_fails_with_a_message_instead_of_a_partial_result(self):
        cases = [(FakeSource([turn('t0', 3), turn('t1', 2)], [MergeError('usage_limit', 'limit reached')]), True,
                  'Could not build'),
                 (FakeSource([turn('t0', 3)]), False, 'no longer enabled')]
        for source, enabled, expected in cases:
            with self.subTest(expected=expected):
                events = Events()
                with self.patched(source, enabled):
                    self.assertEqual(entry.install(str(self.directory), {}, events), 1)
                self.assertEqual(events.items[-1]['event'], 'failed')
                self.assertIn(expected, events.items[-1]['message'])
                self.assertFalse(events.of('message'))

    def test_install_reports_a_crash_and_refuses_a_second_compaction(self):
        source = FakeSource([turn('t0', 3), turn('t1', 2)])
        source.render = lambda *args: 1 / 0
        events = Events()
        with self.patched(source):
            self.assertEqual(entry.install(str(self.directory), {}, events), 1)
        self.assertIn('unexpected error', events.items[-1]['message'])
        self.assertIn('ZeroDivisionError', (self.directory / 'compaction/build.log').read_text())
        held = Store(self.directory).install_lock()
        events = Events()
        with self.patched(FakeSource([turn('t0', 3)])):
            self.assertEqual(entry.install(str(self.directory), {}, events), 1)
        self.assertIn('already running', events.items[-1]['message'])
        held.release()

    def test_background_stores_the_turn_end_and_starts_one_builder(self):
        with self.patched(FakeSource([turn('t0', 3)])), patch.object(entry.subprocess, 'Popen') as popen:
            self.assertEqual(entry.background(str(self.directory), '1234'), 0)
            self.assertEqual(Store(self.directory).turn_end(), '1234')
            self.assertEqual(popen.call_count, 1)
            self.assertIn('worker', popen.call_args.args[0])
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            held = Store(self.directory).worker_lock()
            self.assertEqual(entry.background(str(self.directory), '5678'), 0)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(Store(self.directory).turn_end(), '5678')
            held.release()
        with self.patched(FakeSource([turn('t0', 3)]), background=False), patch.object(entry.subprocess, 'Popen') as popen:
            entry.background(str(self.directory), '9')
            popen.assert_not_called()

    def test_the_worker_builds_every_turn_the_marker_says_has_ended(self):
        source = FakeSource([turn('t0', 3), turn('t1', 3)])
        with self.patched(source):
            Store(self.directory).mark_turn_end('all')
            self.assertEqual(entry.worker(str(self.directory)), 0)
        self.assertEqual(Store(self.directory).tree_count(), 2)
        source = FakeSource([turn('u0', 3), turn('u1', 3)])
        with self.patched(source):
            Store(self.directory).mark_turn_end('newest-still-running')
            entry.worker(str(self.directory))
        self.assertTrue(Store(self.directory).tree_path('u0').exists())
        self.assertFalse(Store(self.directory).tree_path('u1').exists())
        dropped = turn('v0', 3)
        dropped.dropped = 'a command'
        with self.patched(FakeSource([dropped, turn('v1', 3)])):
            Store(self.directory).mark_turn_end('all')
            entry.worker(str(self.directory))
        self.assertFalse(Store(self.directory).tree_path('v0').exists())
        self.assertTrue(Store(self.directory).tree_path('v1').exists())

    def test_guard_allows_subagents_and_refuses_the_session(self):
        with patch.dict(os.environ, {'MV2_AGENT_DIR': str(self.directory)}), patch('sys.stderr', new=io.StringIO()) as err:
            self.assertEqual(entry.guard(json.dumps({'trigger': 'auto', 'agent_id': 'sub'})), 0)
            self.assertEqual(entry.guard(json.dumps({'trigger': 'manual'})), 2)
            self.assertEqual(entry.guard('not json'), 2)
        self.assertIn('build.log', err.getvalue())

    def test_the_module_runs_from_the_command_line(self):
        result = subprocess.run([sys.executable, '-m', 'multiverse.compaction', 'guard'], input='{}', text=True,
                                capture_output=True, env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        self.assertEqual(result.returncode, 2)
        self.assertIn('blocked', result.stderr)


if __name__ == '__main__':
    unittest.main()
