"""A model observation belongs to one launch and conversation, separately from requested choices."""
from copy import deepcopy
from dataclasses import asdict
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.types import NativeResult, Usage
from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write
from multiverse.tools import get_usage, list_agents


class ObservedModelTest(PluginTestCase):
    def test_observations_require_this_launch_and_session_in_every_usage_entry_point(self):
        """A resumed or switched agent can otherwise silently show the previous conversation's plausible model."""
        identity = str(uuid4())
        started = '2026-11-12T10:00:00Z'
        directory = self.agent('worker', native_id=identity, model='opus', effort='high',
                               runtime={'started_at': started})
        properties, facts = read(directory / 'properties.json'), read(directory / 'runtime.json')
        row = {**properties, 'universe': str(self.root), 'runtime': facts}
        baseline = asdict(Usage(identity, 10, 5, 15, 'fixture', '2026-11-12T11:00:00Z',
                                model='claude-sonnet-5-5', effort='medium', observed_at=started))
        empty = {'model': None, 'effort': None, 'observed_at': None}
        cases = [
            ('equal start', {}, {}, {'model': 'claude-sonnet-5-5', 'effort': 'medium', 'observed_at': started}),
            ('offset after start', {'observed_at': '2026-11-12T02:01:00-08:00'}, {},
             {'model': 'claude-sonnet-5-5', 'effort': 'medium', 'observed_at': '2026-11-12T02:01:00-08:00'}),
            ('old turn', {'observed_at': '2026-11-12T09:59:59Z'}, {}, empty),
            ('missing turn time', {'observed_at': None}, {}, empty),
            ('naive', {'observed_at': '2026-11-12T11:00:00'}, {}, empty),
            ('date only', {'observed_at': '2026-11-12'}, {}, empty),
            ('damaged time', {'observed_at': '2026-11-12T10:00:00Z:00'}, {}, empty),
            ('non-string time', {'observed_at': 100}, {}, empty),
            ('missing launch time', {}, {'started_at': None}, empty),
            ('naive launch time', {}, {'started_at': '2026-11-12T10:00:00'}, empty),
            ('wrong session', {'native_session_id': str(uuid4())}, {}, empty),
            ('unbound reading', {'native_session_id': None}, {}, empty),
            ('model only', {'effort': None}, {},
             {'model': 'claude-sonnet-5-5', 'effort': None, 'observed_at': started}),
            ('effort only', {'model': None}, {}, {'model': None, 'effort': 'medium', 'observed_at': started}),
            ('damaged spelling', {'model': ['opus'], 'effort': 'high\nextra'}, {},
             {'model': None, 'effort': None, 'observed_at': started}),
        ]
        for label, changes, launch_changes, expected in cases:
            with self.subTest(label=label):
                value = {**baseline, **changes}
                snapshot = deepcopy(value)
                current = {**facts, **launch_changes}
                write(directory / 'runtime.json', current)
                with patch.object(harnesses.ClaudeAdapter, 'get_usage', return_value=NativeResult('ok', value=value)):
                    results = [native.usage_snapshot(self.root, properties, current, reconciled=True),
                               get_usage.run(self.root, agent='worker'),
                               lifecycle.row_usage({**row, 'runtime': current})['usage']]
                for result in results:
                    self.assertEqual(result['state'], 'ok', result)
                    self.assertEqual({key: result['value'].get(key) for key in empty}, expected)
                    self.assertEqual(result['value']['total_tokens'], 15)
                self.assertEqual(value, snapshot)
                self.assertEqual(read(directory / 'properties.json')['model'], 'opus')
                self.assertEqual(read(directory / 'properties.json')['effort'], 'high')

    def test_plugins_cannot_replace_turn_observations_or_fill_an_absent_one(self):
        """An adjuster can invent or erase plausible model settings unless core keeps the original turn identity and evidence."""
        self.write_plugin('estimate', {'module': 'plugin', 'extends': ['usage']}, {'plugin.py':
            "def adjust(info, usage):\n"
            "    mode = usage['fixture_adjustment']\n"
            "    adjusted = {**usage, 'model': 'guessed-model', 'effort': 'guessed-effort', "
            "'observed_at': '2099-01-01T00:00:00Z', 'note': 'kept'}\n"
            "    if mode == 'omit': adjusted.pop('native_session_id', None)\n"
            "    if mode == 'replace': adjusted['native_session_id'] = 'invented-id'\n"
            "    if mode == 'counters': return {'total_tokens': 20, 'note': 'kept'}\n"
            "    return adjusted\n"
            "def register(mv): mv.usage.adjust(adjust)\n"})
        self.enable('estimate')
        identity = str(uuid4())
        directory = self.agent('worker', native_id=identity, runtime={'started_at': '2026-11-12T10:00:00Z'})
        properties, facts = read(directory / 'properties.json'), read(directory / 'runtime.json')
        for turn in (None, '2026-11-12T10:10:00Z'):
            for mode in ('preserve', 'omit', 'replace', 'counters'):
                with self.subTest(turn=turn, mode=mode):
                    measured = {'native_session_id': identity, 'model': 'raw-model', 'effort': 'raw-effort',
                                'observed_at': turn, 'total_tokens': 10, 'fixture_adjustment': mode}
                    with patch.object(harnesses.ClaudeAdapter, 'get_usage', return_value=NativeResult('ok', value=measured)):
                        value = native.usage_snapshot(self.root, properties, facts, reconciled=True)['value']
                    self.assertTrue({'model', 'effort', 'observed_at'}.issubset(value), value)
                    self.assertEqual((value['model'], value['effort'], value['observed_at']),
                                     ('raw-model', 'raw-effort', turn) if turn else (None, None, None))
                    self.assertEqual(value.get('native_session_id'), identity)
                    self.assertEqual(value['total_tokens'], 20 if mode == 'counters' else 10)
                    self.assertEqual(value['note'], 'kept')
        with self.subTest('an adjuster cannot supply a missing measured identity'):
            measured = {'model': 'raw-model', 'effort': 'raw-effort', 'observed_at': '2026-11-12T10:10:00Z',
                        'fixture_adjustment': 'replace'}
            with patch.object(harnesses.ClaudeAdapter, 'get_usage', return_value=NativeResult('ok', value=measured)):
                value = native.usage_snapshot(self.root, properties, facts, reconciled=True)['value']
            self.assertEqual([value[key] for key in ('model', 'effort', 'observed_at')], [None, None, None])
            self.assertNotIn('native_session_id', value)

    def test_listing_keeps_default_and_observed_labels_separate_from_recorded_choices(self):
        """A missing model or an asynchronous usage update can otherwise inherit another row's label or default effort."""
        configuration = self.root / 'harnesses/claude/harness.json'
        write(configuration, {**read(configuration), 'settings': {
            'model': 'haiku', 'model_names': {'private-observed': 'Observed locally', 'haiku': 'Default locally'}}})
        identity = str(uuid4())
        directory = self.agent('recorded', native_id=identity, model='opus', effort='high',
                               runtime={'started_at': '2026-11-12T10:00:00Z'})
        self.agent('unset')
        observed = {'native_session_id': identity, 'model': 'private-observed', 'effort': 'medium',
                    'observed_at': '2026-11-12T10:01:00Z'}
        with patch.object(harnesses.ClaudeAdapter, 'get_usage', return_value=NativeResult('ok', value=observed)):
            rows = {row['name']: row for row in list_agents.cli_run(self.root, json=True)}
            row = rows['recorded']
            self.assertEqual((row['model'], row['effort'], row['model_name'], row.get('observed_model_name')),
                             ('opus', 'high', 'Opus 5.5', 'Observed locally'))
            self.assertIsNone(rows['unset']['usage']['value']['model'])
            self.assertIsNone(rows['unset']['observed_model_name'])
            later = lifecycle.row_usage(row)
            self.assertEqual(later['observed_model_name'], 'Observed locally')
        with patch.object(harnesses.ClaudeAdapter, 'get_usage', side_effect=AssertionError('usage must stay off-loop')):
            rows = lifecycle.listing(self.root, usage=False)
        for row in rows:
            self.assertEqual((row['default_model'], row['default_model_name']), ('haiku', 'Default locally'))
            self.assertNotIn('default_effort', row)
            self.assertNotIn('usage', row)
        write(configuration, {**read(configuration), 'settings': {'model': 'unknown-default'}})
        rows = lifecycle.listing(self.root, usage=False)
        self.assertTrue(all(row['default_model'] == 'unknown-default' and row['default_model_name'] is None for row in rows))
