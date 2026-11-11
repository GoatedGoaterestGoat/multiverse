"""One model policy for catalogs, launch/resume and disposable model runs, without native sessions."""
from dataclasses import replace
import os
import sys
from unittest.mock import Mock, patch

from multiverse import harnesses
from multiverse.harnesses.types import (ModelCatalog, ModelCheck, ModelInfo, NativeEndpoint, RunFailure, RunReply,
                                       SharedServiceSpec)
from multiverse.runtime import lifecycle, model_choices, native, runs
from multiverse.store import read, write
from multiverse.tools import doctor, resume, send, spawn
from multiverse.universe import agents
from test_multiverse import BaseTest
from test_operations import FourthAdapter
from test_runs import Provider, REQUEST


class Models:
    def models(self, *, environment):
        values = (
            ModelInfo(id='fast', name='Fast', aliases=('quick',), efforts=('low', 'high'), default_effort='high', default=True),
            ModelInfo(id='steady', name='Steady', efforts=('low', 'high'), default_effort='low'),
            ModelInfo(id='plain', name='Plain'))
        suffix = environment.get('TEST_CATALOG_SUFFIX', '')
        return ModelCatalog(models=tuple(replace(item, id=item.id + suffix) for item in values),
                            free_form=True, source='fixture')

    def check_model(self, model, *, effort=None, environment):
        item = next((item for item in self.models(environment=environment).models
                     if model in (item.id, *item.aliases)), None)
        if item is None:
            return ModelCheck(ok=True, model=model, effort=effort, verified=False)
        return ModelCheck(ok=effort is None or effort in item.efforts, model=item.id, verified=True,
                          effort=effort if effort is not None else item.default_effort,
                          reason='Unsupported effort' if effort is not None and effort not in item.efforts else None)


class LaunchAdapter(Models, FourthAdapter):
    supports_launch_effort = True
    settings = {}


class RunAdapter(Models, Provider):
    pass


class ModelChoicesTest(BaseTest):
    def configure(self, harness, settings):
        path = self.root / 'harnesses' / harness / 'harness.json'
        value = read(path)
        value['settings'] = settings
        write(path, value)

    def test_real_catalogs_keep_defaults_until_a_complete_valid_subset_is_set(self):
        """Catalog and default eligibility must match the current subset or a picker can offer forbidden choices."""
        for name in ('claude', 'codex'):
            with self.subTest(name):
                self.configure(name, {})
                original = lifecycle.model_choices(self.root, name)
                self.assertTrue(original['free_form'])
                self.assertTrue(original['unset'])
                self.assertFalse(original['narrowed'])
                first, second = [item for item in original['models'] if item['efforts']][:2]
                self.configure(name, {'models': {second['id']: {}, first['id']: {'efforts': [first['efforts'][0]]}}})
                chosen = lifecycle.model_choices(self.root, name)
                expected_order = [item['id'] for item in original['models'] if item['id'] in (first['id'], second['id'])]
                self.assertEqual([item['id'] for item in chosen['models']], expected_order)
                self.assertFalse(chosen['free_form'])
                self.assertFalse(chosen['unset'])
                self.assertTrue(chosen['narrowed'])
                self.assertEqual(chosen['models'][0]['efforts'], (first['efforts'][0],))
                self.assertFalse(chosen['models'][0]['effort_unset'])
                self.assertTrue(chosen['models'][1]['effort_unset'])
                adapter = harnesses.load(self.root, name)
                for model, effort, ok in ((first['id'], first['efforts'][0], True), (first['id'], None, False),
                                          (first['id'], first['efforts'][-1], len(first['efforts']) == 1),
                                          ('unlisted-private-model', None, False), ('default', None, False),
                                          (second['id'], None, True), (None, None, False)):
                    with self.subTest(model=model, effort=effort):
                        result = model_choices.check(self.root, adapter, model, effort=effort, environment={})
                        self.assertEqual(result.ok, ok, result)
                self.configure(name, {'model': second['id'], 'models': {second['id']: {}}})
                self.assertTrue(lifecycle.model_choices(self.root, name)['unset'])
                self.configure(name, {'model': second['id'],
                                      'models': {second['id']: {'efforts': [second['efforts'][0]]}}})
                configured = lifecycle.model_choices(self.root, name)
                self.assertTrue(configured['unset'])  # The model can be omitted even though the effort cannot.
                self.assertEqual(configured.get('default_model'), second['id'])
                self.assertFalse(configured['models'][0]['effort_unset'])
                self.configure(name, {'model': 'unlisted-default', 'models': {second['id']: {}}})
                configured = lifecycle.model_choices(self.root, name)
                self.assertFalse(configured['unset'])
                self.assertIsNone(configured.get('default_model'))
                self.configure(name, {})
                self.assertIsNone(lifecycle.model_choices(self.root, name).get('default_model'))
                self.assertTrue(model_choices.check(self.root, harnesses.load(self.root, name),
                                                   'unlisted-private-model', environment={}).ok)

        for environment in ({}, {'CLAUDE_CODE_USE_BEDROCK': '1'}):
            with self.subTest(provider=environment):
                self.configure('claude', {'env': environment, 'models': {'sonnet': {}}})
                adapter = harnesses.load(self.root, 'claude')
                result = model_choices.check(self.root, adapter, 'sonnet', environment=environment)
                self.assertTrue(result.ok)
                self.assertNotEqual(result.model, 'sonnet')
                self.assertEqual(lifecycle.model_choices(self.root, 'claude')['models'][0]['id'], result.model)
                self.assertIsNone(result.effort)  # The table default is not the effective omitted effort.
        self.assertIsNone(lifecycle.model_choices(self.root, 'pi'))

        invalid = ({}, None, [], {'gone-model': {}}, {'sonnet': {'efforts': ['gone-level']}},
                   {'sonnet': {'efforts': []}}, {'sonnet': {'efforts': ['low', 'low']}},
                   {'sonnet': {'efforts': 'low'}}, {'sonnet': {'unknown': []}}, {'sonnet': None})
        for setting in invalid:
            with self.subTest(setting=setting):
                self.configure('claude', {'models': setting})
                with self.assertRaisesRegex(ValueError, r'harnesses/claude/harness.json.*settings.models'):
                    lifecycle.model_choices(self.root, 'claude')
                result = model_choices.check(self.root, harnesses.load(self.root, 'claude'), 'sonnet', environment={})
                self.assertFalse(result.ok)
                self.assertIn('harness.json', result.reason)
                problems = doctor.run(self.root)['problems']
                self.assertTrue(any('harness.json' in problem and 'settings.models' in problem for problem in problems),
                                problems)
        self.configure('claude', {'model': 'opus', 'models': {'sonnet': {'efforts': ['low']}}})
        problems = doctor.run(self.root)['problems']
        self.assertTrue(any('settings.model' in problem and 'outside this subset' in problem for problem in problems),
                        problems)
        self.configure('claude', {'model': 'sonnet', 'models': {'sonnet': {'efforts': ['low']}}})
        self.assertFalse(any('settings.model' in problem for problem in doctor.run(self.root)['problems']))
        self.configure('pi', {'models': {'something': {}}})
        with self.assertRaisesRegex(ValueError, 'no model catalog'):
            lifecycle.model_choices(self.root, 'pi')

    def test_launch_and_resume_recheck_choices_before_publishing_or_starting(self):
        """Excluding a recorded choice must refuse a resume without changing its records or quietly dropping effort."""
        def accepted(call, **options):
            try:
                return call(self.root, **options)
            except (ValueError, RuntimeError) as error:
                self.fail(f'An allowed selection was refused: {error}')

        adapter = LaunchAdapter()
        adapter.settings = {}
        terminal = Mock()
        terminal.create.return_value = {'socket': '/fake', 'session': '$1', 'pane': '%1'}
        self.enterContext(patch.object(harnesses, 'load', return_value=adapter))
        self.enterContext(patch.object(lifecycle, 'Tmux', return_value=terminal))
        self.enterContext(patch.object(lifecycle.shutil, 'which', return_value='/fake/binary'))
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}))
        with patch.object(adapter, 'check_model', return_value=ModelCheck(ok=False, reason='Unavailable model')):
            with self.assertRaisesRegex(ValueError, 'Unavailable model'):
                spawn.run(self.root, name='unavailable', harness='fourth', model='fast', effort='high')
        agent = spawn.run(self.root, name='restricted', harness='fourth', model='fast', effort='high')
        directory = agents.directory(self.root, agent['id'])
        adapter.settings = {'models': {'fast': {'efforts': ['low']}, 'steady': {'efforts': ['low']}, 'plain': {}}}
        before = {path.name: path.read_bytes() for path in directory.glob('*.json')}
        for options in ({}, {'model': 'steady'}, {'model': 'plain'}, {'effort': 'default'},
                        {'model': 'outside'}, {'effort': 'high'}):
            with self.subTest(options=options):
                terminal.reset_mock()
                with self.assertRaises(ValueError) as refusal:
                    resume.run(self.root, agent=agent['id'], **options)
                self.assertNotIn('--effort default', str(refusal.exception))
                if options == {}:
                    self.assertIn('choose an effort from low', str(refusal.exception))
                terminal.create.assert_not_called()
                self.assertEqual({path.name: path.read_bytes() for path in directory.glob('*.json')}, before)
        fixed = accepted(resume.run, agent=agent['id'], model='quick', effort='low')
        self.assertEqual((fixed['model'], fixed['effort']), ('fast', 'low'))
        self.assertEqual((adapter.calls[-1][-1].model, adapter.calls[-1][-1].effort), ('fast', 'low'))
        self.assertEqual(resume.run(self.root, agent=agent['id'])['effort'], 'low')
        adapter.settings = {'models': {'steady': {}}}
        with self.assertRaisesRegex(ValueError, 'not allowed'):
            resume.run(self.root, agent=agent['id'])
        self.assertEqual(resume.run(self.root, agent=agent['id'], model='steady', effort='default')['model'], 'steady')
        for options in ({}, {'model': 'outside'}, {'model': 'steady', 'effort': 'wrong'}):
            with self.subTest(spawn=options):
                with self.assertRaises(ValueError):
                    spawn.run(self.root, name='refused', harness='fourth', **options)
                with self.assertRaises(agents.AgentNotFound):
                    agents.directory(self.root, 'refused')
        adapter.default_model = 'steady'
        self.assertEqual(spawn.run(self.root, name='configured', harness='fourth')['model'], 'steady')
        adapter.settings = {'models': {'steady': {'efforts': ['low']}}}
        configured = accepted(spawn.run, name='configured-with-effort', harness='fourth', effort='low')
        self.assertEqual((configured['model'], configured['effort']), ('steady', 'low'))
        with self.assertRaisesRegex(ValueError, 'Choose an effort'):
            spawn.run(self.root, name='configured-needs-effort', harness='fourth')
        adapter.settings = {'models': {'quick': {}}}
        prepared = adapter.spawn
        with patch.object(adapter, 'spawn', side_effect=lambda context:
                          replace(prepared(context), environment={'TEST_CATALOG_SUFFIX': '-cloud'})):
            cloud = accepted(spawn.run, name='actual-environment', harness='fourth', model='quick')
        self.assertEqual(cloud['model'], 'fast-cloud')
        self.assertEqual(adapter.calls[-1][-1].model, 'fast-cloud')
        for late in (False, True):
            with self.subTest(final_preparation=late):
                terminal.reset_mock()

                def changed(context):
                    command = prepared(context)
                    if late:
                        return replace(command, shared_service=SharedServiceSpec(('stand-in',), 'socket', ('cli', 'server')),
                                       environment={'TEST_CATALOG_SUFFIX': '-cloud'} if context.executor else {})
                    return replace(command, fallback=replace(command, environment={'TEST_CATALOG_SUFFIX': '-cloud'}))

                with patch.object(adapter, 'spawn', side_effect=changed), \
                        patch.object(native, 'start_shared_service', return_value=NativeEndpoint('unix-websocket', '/fake')):
                    with self.assertRaisesRegex((RuntimeError, ValueError), 'not allowed'):
                        spawn.run(self.root, name='changed-final' if late else 'changed-fallback', harness='fourth', model='quick')
                if not late:
                    terminal.create.assert_not_called()
                else:
                    failed = agents.directory(self.root, 'changed-final')
                    self.assertEqual(agents.runtime(failed)['state'], 'failed')
                    self.assertEqual(list(failed.glob('launches/*/request.json')), [])
        adapter.settings = {'models': {'gone-model': {}}}
        with self.assertRaisesRegex(ValueError, 'harness.json'):
            resume.run(self.root, agent=agent['id'], model='steady')

    def test_unrestricted_model_without_effort_passes_through_spawn_resume_and_wake(self):
        """A model accepted before subsets existed must still resume and wake unless a subset or effort needs validation."""
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.work / 'claude-home')}))
        self.configure('claude', {'executable': sys.executable, 'inbox_bridge': False})
        adapter = harnesses.load(self.root, 'claude')
        model = 'sonnet[1m]'
        self.assertFalse(adapter.check_model(model, environment={}).ok)
        terminal = Mock()
        terminal.create.return_value = {'socket': '/fake', 'session': '$1', 'pane': '%1'}
        self.enterContext(patch.object(lifecycle, 'Tmux', return_value=terminal))
        self.enterContext(patch.object(lifecycle.shutil, 'which', return_value='/fake/binary'))
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'dormant'}))
        self.enterContext(patch.object(native, 'session_snapshot', side_effect=lambda root, d, p, f, **kw: (p, f)))
        self.enterContext(patch.object(lifecycle, '_wake_socket', return_value='/fake'))
        self.enterContext(patch.object(native, 'start_shared_service', side_effect=AssertionError('No native service')))
        for action in ('resume', 'wake', 'spawn'):
            with self.subTest(action=action):
                agent = spawn.run(self.root, name=action, harness='claude')
                directory = agents.directory(self.root, agent['id'])
                # A main-era record carries the raw model with no effort or model-subset metadata.
                properties = read(directory / 'properties.json')
                properties['model'] = model
                properties.pop('effort', None)
                write(directory / 'properties.json', properties)
                terminal.reset_mock()
                try:
                    if action == 'resume':
                        result = resume.run(self.root, agent=agent['id'])
                    elif action == 'wake':
                        result = send.run(self.root, agent=agent['id'], text='wake input', delivery='follow-up')
                        self.assertTrue(result['ok'], result)
                    else:
                        result = spawn.run(self.root, name='raw-model', harness='claude', model=model)
                        directory = agents.directory(self.root, result['id'])
                except (ValueError, RuntimeError) as error:
                    self.fail(f'Unrestricted {action} refused a model without effort: {error}')
                terminal.create.assert_called_once()
                facts = agents.runtime(directory)
                request = read(directory / 'launches' / facts['launch_id'] / 'request.json')
                argv = request['argv']
                self.assertEqual(argv[argv.index('--model') + 1], model)
                self.assertEqual(read(directory / 'properties.json')['model'], model)
                self.assertNotIn('--effort', argv)
                if action == 'wake':
                    self.assertIn('wake input', argv[-1])

        for settings, effort in (({'models': {'sonnet': {}}}, None), ({}, 'high')):
            with self.subTest(settings=settings, effort=effort):
                self.configure('claude', {'executable': sys.executable, 'inbox_bridge': False, **settings})
                directory = agents.directory(self.root, 'resume')
                properties = read(directory / 'properties.json')
                properties['effort'] = effort
                write(directory / 'properties.json', properties)
                before = {path.name: path.read_bytes() for path in directory.glob('*.json')}
                terminal.reset_mock()
                with self.assertRaisesRegex(ValueError, 'does not take'):
                    resume.run(self.root, agent='resume')
                terminal.create.assert_not_called()
                self.assertEqual({path.name: path.read_bytes() for path in directory.glob('*.json')}, before)

    def test_run_uses_the_same_subset_before_preparing_a_paid_child(self):
        """A run must obey the subset in its final environment or a routing change can silently bypass it."""
        adapter = RunAdapter()
        adapter.settings = {'models': {'quick': {'efforts': ['low']}}}
        environment = {'HOME': str(self.work / 'home'), 'STORE': str(self.work / 'store'), 'PATH': os.environ['PATH']}
        for model, effort in (('fast', None), ('fast', 'high'), ('steady', 'low'), ('outside', None)):
            with self.subTest(model=model, effort=effort):
                with self.assertRaises(RunFailure) as error:
                    runs.run_once(adapter, replace(REQUEST, model=model, effort=effort), root=self.root,
                                  plugin='fixture', environment=environment, timeout=5)
                self.assertEqual(error.exception.kind, 'invalid_request')
                self.assertEqual(adapter.plans, [])
        with patch.object(adapter, 'run_plan', wraps=adapter.run_plan) as plan:
            result = runs.run_once(adapter, replace(REQUEST, model='quick', effort='low'), root=self.root,
                                   plugin='fixture', environment=environment, timeout=5)
        self.assertEqual(plan.call_args.args[0].model, 'fast')
        self.assertEqual(result.text.strip(), 'answer')

        for change in ('add', 'remove', 'reroute', 'unrelated'):
            with self.subTest(final_environment=change):
                adapter = RunAdapter(files=False, overlay={'TEST_CATALOG_SUFFIX': '-cloud'}
                                     if change in ('add', 'reroute') else {'UNRELATED': 'retained'})
                adapter.settings = {'models': {'quick': {'efforts': ['low']}}}
                if change == 'reroute':
                    adapter.settings['models']['steady'] = {'efforts': ['low']}
                actual = {**environment, **({'TEST_CATALOG_SUFFIX': '-cloud'} if change == 'remove' else {})}
                original_plan, original_check = adapter.run_plan, adapter.check_model

                def prepare(*args, **kwargs):
                    value = original_plan(*args, **kwargs)
                    return replace(value, command=replace(value.command, unset_env=('TEST_CATALOG_SUFFIX',))) \
                        if change == 'remove' else value

                def resolve(model, *, effort, environment):
                    if change == 'reroute' and model in ('fast', 'steady') and environment.get('TEST_CATALOG_SUFFIX'):
                        model = 'steady-cloud'  # Allowed, but different from the prepared request's canonical model.
                    return original_check(model, effort=effort, environment=environment)

                before_start = Mock()
                with patch.object(adapter, 'run_plan', side_effect=prepare), \
                        patch.object(adapter, 'check_model', side_effect=resolve), \
                        patch.object(runs, '_execute', return_value=RunReply(text='fixture')) as execute:
                    if change == 'unrelated':
                        runs.run_once(adapter, replace(REQUEST, model='quick', effort='low'), root=self.root,
                                      plugin='fixture', environment=actual, timeout=5, before_start=before_start)
                        self.assertEqual(execute.call_count, 1)
                        self.assertEqual(execute.call_args.args[2]['environment']['UNRELATED'], 'retained')
                    else:
                        with self.assertRaises(RunFailure) as failure:
                            runs.run_once(adapter, replace(REQUEST, model='quick', effort='low'), root=self.root,
                                          plugin='fixture', environment=actual, timeout=5, before_start=before_start)
                        self.assertEqual(failure.exception.kind, 'invalid_request')
                        self.assertIn('changes the resolved model' if change == 'reroute' else 'not allowed',
                                      str(failure.exception))
                        execute.assert_not_called()
                        before_start.assert_not_called()
                    self.assertEqual(len(adapter.plans), 1)
                    self.assertFalse(adapter.plans[-1].path.exists())
