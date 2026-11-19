"""Saved model choices are independent of the running native conversation."""
from concurrent.futures import ThreadPoolExecutor
import threading
import os
from unittest.mock import patch

from multiverse import harnesses
from multiverse.plugins import access
from multiverse.runtime import lifecycle, native
from multiverse.store import lock, read, uid, write
from multiverse.tools import inspect
from multiverse.universe import agents
from test_model_choices import LaunchAdapter
from test_multiverse import BaseTest


class SetModelTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.adapter = LaunchAdapter()
        self.adapter.settings = {}
        self.agent_id, self.launch = uid(), uid()
        self.directory = self.root / 'state' / 'agents' / self.agent_id
        self.properties = {'id': self.agent_id, 'universe_id': read(self.root / 'universe.json')['id'], 'name': 'worker',
                           'harness': 'fourth', 'workspace': str(self.work), 'model': 'fast', 'effort': 'high',
                           'native_session_id': uid(), 'phase': 'blocked', 'summary': 'Keep this summary'}
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'working'})
        self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))

    def setter(self, **options):
        self.assertTrue(callable(getattr(lifecycle, 'set_model', None)), 'Saving a model must not require a launch')
        return lifecycle.set_model(self.root, 'worker', **options)

    def test_saved_choices_never_launch_or_change_native_records_even_in_a_recovered_folder(self):
        """Changing a next-resume choice must leave running work, native identity and inbox records untouched."""
        for state in ('running', 'starting', 'stopped', 'dormant'):
            with self.subTest(state=state):
                write(self.directory / 'properties.json', self.properties)
                write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': state, 'status': 'working'})
                write(self.directory / 'inbox-state.json', {'kept': True})
                before = {p.name: p.read_bytes() for p in self.directory.glob('*.json')}
                with patch.object(lifecycle, 'start', side_effect=AssertionError('must never launch')), \
                        patch.object(native, 'context', side_effect=AssertionError('must never touch native state')), \
                        patch.object(lifecycle, 'Tmux', side_effect=AssertionError('must never touch tmux')):
                    result = self.setter(model='steady')
                self.assertEqual((result['model'], result['effort']), ('steady', 'high'))
                self.assertEqual(bool(result['notices']), state in ('running', 'starting'))
                saved = read(self.directory / 'properties.json')
                self.assertEqual({k: v for k, v in saved.items() if k not in ('model', 'effort', 'updated_at', 'model_before_edit')},
                                 {k: v for k, v in self.properties.items() if k not in ('model', 'effort', 'updated_at', 'model_before_edit')})
                if state in ('running', 'starting'):
                    self.assertEqual(saved['model_before_edit'], {'launch_id': self.launch, 'model': 'fast', 'effort': 'high'})
                    self.setter(model='fast', effort='low')
                    self.assertEqual(read(self.directory / 'properties.json')['model_before_edit'], saved['model_before_edit'])
                else:
                    self.assertNotIn('model_before_edit', saved)
                for name, content in before.items():
                    if name != 'properties.json':
                        self.assertEqual((self.directory / name).read_bytes(), content)
                self.assertEqual(self.adapter.calls, [])
        moved = self.directory.with_name('recovered-folder')
        self.directory.rename(moved)
        result = self.setter(model='plain')
        self.assertEqual((result['id'], read(moved / 'properties.json')['model']), (self.agent_id, 'plain'))
        self.assertFalse(self.directory.exists())

    def test_saved_effort_is_kept_dropped_or_refused_under_the_current_subset(self):
        """Saved effort must not silently select a forbidden model or level when a model choice changes."""
        rows = (
            ({}, {}, {'model': 'steady'}, ('steady', 'high')),
            ({}, {}, {'model': 'plain'}, ('plain', None)),
            ({}, {}, {'effort': ''}, ('fast', None)),
            ({}, {}, {'model': '', 'effort': ''}, (None, None)),
            ({}, {'effort': None}, {'model': 'private[1m]'}, ('private[1m]', None)),
            ({}, {'model': None, 'effort': None}, {'effort': 'high'}, None),
            ({'model': 'steady'}, {'model': None, 'effort': None}, {'effort': 'low'}, (None, 'low')),
            ({'models': {'quick': {}, 'steady': {}}}, {}, {'model': 'quick'}, ('fast', 'high')),
            ({'models': {'plain': {}}}, {}, {'model': 'plain'}, ('plain', None)),
            ({'models': {'steady': {'efforts': ['low']}}}, {}, {'model': 'steady'}, None),
            ({'models': {'steady': {'efforts': ['low']}}}, {}, {'model': 'steady', 'effort': 'low'}, ('steady', 'low')),
            ({'models': {'steady': {}}}, {}, {'model': 'outside'}, None),
            ({'models': {'steady': {}}}, {}, {'model': '', 'effort': ''}, None),
            ({'model': 'steady', 'models': {'steady': {}}}, {}, {'model': '', 'effort': ''}, (None, None)),
            ({}, {}, {'model': 'plain', 'effort': 'high'}, None),
        )
        for settings, recorded, options, expected in rows:
            with self.subTest(settings=settings, options=options):
                self.adapter.settings = settings
                self.adapter.default_model = settings.get('model')
                write(self.directory / 'properties.json', {**self.properties, **recorded})
                before = (self.directory / 'properties.json').read_bytes()
                if expected is None:
                    with self.assertRaises(ValueError):
                        self.setter(**options)
                    self.assertEqual((self.directory / 'properties.json').read_bytes(), before)
                else:
                    result = self.setter(**options)
                    self.assertEqual((result['model'], result['effort']), expected)
                    if options == {'model': 'plain'}:
                        self.assertTrue(any('Dropped recorded effort' in note for note in result['notices']))

    def test_permission_pending_commands_and_invalid_records_refuse_before_writing(self):
        """A saved-choice edit must keep resume's access guard and never race an unresolved settings command."""
        self.assertTrue(callable(getattr(lifecycle, 'set_model', None)))
        before = (self.directory / 'properties.json').read_bytes()
        for level in ('observe', 'spend'):
            with self.subTest(level=level), access.within(level, 'test plugin'), self.assertRaises(access.Denied):
                self.setter(model='steady')
        for options in ({}, {'model': 7}, {'model': 'bad\0id'}, {'effort': 'not a level'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.setter(**options)
        with patch.object(self.adapter, 'supports_launch_effort', False), self.assertRaisesRegex(ValueError, 'does not support'):
            self.setter(effort='low')
        pending = uid()
        write(self.directory / 'slash' / 'pending.json', {'schema': 1, 'ids': [pending]})
        write(self.directory / 'slash' / (pending + '.json'), {'command': 'model', 'session': {'launch_id': self.launch},
              'result': {'id': pending, 'command': 'model', 'args': '', 'state': 'queued'}})
        with self.assertRaisesRegex(ValueError, 'no confirmed model outcome') as refusal:
            self.setter(model='steady', effort='low')
        self.assertNotIn('mv2 resume', str(refusal.exception))
        self.assertIn('mv2 set-model', str(refusal.exception))
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'stopped'})
        with self.assertRaisesRegex(ValueError, 'For this stopped agent, mv2 resume'):
            self.setter(model='steady', effort='low')
        self.assertEqual((self.directory / 'properties.json').read_bytes(), before)

    def test_partial_identity_records_never_publish_a_choice_before_refusing(self):
        """A partial record must not save a choice and then fail while naming its result."""
        self.assertTrue(callable(getattr(lifecycle, 'set_model', None)))
        path = self.directory / 'properties.json'
        original = lifecycle.records.properties
        for key in ('id', 'name', 'harness'):
            for empty in (False, True):
                for late in (False, True):
                    with self.subTest(key=key, empty=empty, late=late):
                        broken = {k: v for k, v in self.properties.items() if k != key}
                        if empty:
                            broken[key] = ''
                        write(path, self.properties if late else broken)
                        calls = []
                        def changed(selected, **kwargs):
                            calls.append(selected)
                            if late and len(calls) == 2:
                                write(path, broken)  # damage appears after admission, before the fresh locked read
                            return original(selected, **kwargs)
                        error = None
                        with patch.object(lifecycle, 'directory', return_value=self.directory), \
                                patch.object(lifecycle.records, 'properties', side_effect=changed):
                            try:
                                lifecycle.set_model(self.root, self.agent_id, model='steady')
                            except Exception as caught:
                                error = caught
                        self.assertIsInstance(error, ValueError)
                        self.assertEqual(read(path), broken)

    def test_an_outdated_caller_cannot_edit_any_agents_saved_choice(self):
        """A previous launch must not edit saved choices when the same caller would be refused by resume."""
        self.assertTrue(callable(getattr(lifecycle, 'set_model', None)))
        facts = read(self.directory / 'runtime.json')
        write(self.directory / 'runtime.json', {**facts, 'tmux': {'socket': str(self.work / 'absent.sock'), 'pane': '%1'}})
        environment = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': self.agent_id,
                       'MV2_AGENT_DIR': str(self.directory), 'MV2_LAUNCH_ID': uid()}
        before = (self.directory / 'properties.json').read_bytes()
        with patch.dict(os.environ, environment), self.assertRaisesRegex(ValueError, 'Outdated launch'):
            self.setter(model='steady')
        self.assertEqual((self.directory / 'properties.json').read_bytes(), before)
        with patch.dict(os.environ, {**environment, 'MV2_LAUNCH_ID': self.launch}):
            self.assertEqual(self.setter(model='steady')['model'], 'steady')

    def test_waiting_for_launch_lock_preserves_a_newer_report(self):
        """Saving a model after another writer must preserve its report rather than overwrite a stale snapshot."""
        self.assertTrue(callable(getattr(lifecycle, 'set_model', None)))
        seen = threading.Event()
        original = lifecycle.records.properties
        def observed(path, **kwargs):
            value = original(path, **kwargs)
            seen.set()
            return value
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(lifecycle.records, 'properties', side_effect=observed):
            with lock(self.directory / '.launch.lock'):
                future = pool.submit(self.setter, model='steady')
                self.assertTrue(seen.wait(5))
                agents.report(self.root, self.agent_id, phase='working', summary='A newer report')
            self.assertEqual(future.result(timeout=5)['model'], 'steady')
        self.assertEqual(read(self.directory / 'properties.json')['summary'], 'A newer report')

    def test_inspect_and_listing_expose_saved_choices_without_changing_observed_usage(self):
        """Saved next-resume settings must remain visible while the session's observed model stays independent."""
        self.setter(model='steady', effort='low')
        write(self.directory / 'runtime.json', {})
        with patch.object(lifecycle, 'observe', return_value={'live': 'dormant', 'reason': ''}), \
                patch.object(native, 'session_snapshot', side_effect=lambda root, directory, properties, facts, **kw: (properties, facts)), \
                patch.object(lifecycle, 'row_usage', return_value={}), patch.object(inspect, 'run', return_value=''):
            row = lifecycle.listing(self.root)[0]
            shown = inspect.cli_run(self.root, agent='worker', json=True)
        self.assertEqual((row['model'], row['effort']), ('steady', 'low'))
        self.assertEqual((shown['model'], shown['effort']), ('steady', 'low'))
