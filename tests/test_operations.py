"""Operation contract: an unknown provider works without edits to tools or UI."""
import sys
from unittest.mock import Mock, patch

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native
from multiverse.runtime import slash
from multiverse.ui.backend import LocalBackend
from multiverse.harnesses.types import CAPABILITIES, Capability, CommandSpec, ModelCheck, NativeResult, SessionHandle, SlashCommand
from multiverse.tools import spawn, resume
from multiverse.store import read, write
from test_multiverse import BaseTest


class FourthAdapter:
    """Implements only the public contract, without the bundled rendering base."""
    name = 'fourth'
    session_mode = 'assigned'
    default_model = None
    bootstrap = ''

    def __init__(self):
        self.calls = []
        self.failure = None

    def spawn(self, context):
        self.calls.append(('spawn', context))
        return CommandSpec((sys.executable, '-c', 'pass'))

    def resume(self, session, context):
        if not session.native_id:
            raise ValueError('Missing saved native session')
        self.calls.append(('resume', session, context))
        return CommandSpec((sys.executable, '-c', 'pass'))

    def stop(self, session, executor):
        self.calls.append(('stop', session))
        return NativeResult('ok')

    def capabilities(self, session, executor=None):
        return {key: Capability('supported') for key in CAPABILITIES}

    def send(self, session, message, executor):
        self.calls.append(('send', session, message, message.delivery))
        if self.failure:
            raise RuntimeError(self.failure)
        return NativeResult('native_queued', value={'message_id': message.id})

    def get_delivery(self, session, message_id, executor):
        self.calls.append(('get_delivery', session, message_id))
        return NativeResult('native_queued', value={'message_id': message_id, 'transcript_confirmed': True})

    def ask(self, session, question, executor):
        self.calls.append(('ask', session, question))
        return NativeResult('ok', value={'answer': question, 'native_session_id': session.native_id})

    def get_usage(self, session, executor=None):
        self.calls.append(('get_usage', session))
        return NativeResult('ok', value={'native_session_id': session.native_id, 'total_tokens': 0})


class OperationTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.adapter = FourthAdapter()
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.loader = self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}))
        backend = Mock()
        backend.create.return_value = {'socket': '/fake', 'session': '$1', 'pane': '%1'}
        self.enterContext(patch.object(lifecycle, 'Tmux', return_value=backend))
        self.enterContext(patch.object(lifecycle.shutil, 'which', return_value='/fake/binary'))
        self.first = spawn.run(self.root, name='fourth-worker', harness='fourth')
        self.agent = self.first['id']
        self.directory = agents.directory(self.root, self.agent)

    def test_a_third_party_adapter_spawns_and_resumes_through_tools(self):
        """An adapter implementing only the public protocol spawns without a task, resumes with the same native id and a
        new launch, and a rejected resume writes nothing; fragile because every bundled adapter inherits the rendering
        base, so a tool calling a base-only method passes every other test."""
        properties = (self.directory / 'properties.json').read_bytes()
        runtime = (self.directory / 'runtime.json').read_bytes()
        launches = list((self.directory / 'launches').iterdir())
        with patch.object(self.adapter, 'resume', side_effect=ValueError('Invalid saved session')), \
                patch.object(lifecycle, 'Tmux') as terminal, \
                patch.object(lifecycle.paths, 'prepare', wraps=lifecycle.paths.prepare) as prepare:
            with self.assertRaisesRegex(ValueError, 'Invalid saved session'):
                LocalBackend().resume(self.root, self.agent)
            terminal.assert_not_called()
            prepare.assert_not_called()
        self.assertEqual((self.directory / 'properties.json').read_bytes(), properties)
        self.assertEqual((self.directory / 'runtime.json').read_bytes(), runtime)
        self.assertEqual(list((self.directory / 'launches').iterdir()), launches)
        first = self.first['session']
        self.assertEqual(first['native_id'], self.agent)
        self.assertEqual(self.first['task'], '')
        self.assertEqual(self.first['summary'], '')
        resumed = LocalBackend().resume(self.root, self.agent)['session']
        for key in ('agent_id', 'native_id', 'workspace', 'session_file'):
            self.assertEqual(resumed[key], first[key])
        self.assertNotEqual(resumed['launch_id'], first['launch_id'])
        self.assertEqual([call[0] for call in self.adapter.calls], ['spawn', 'resume'])
        target = self.adapter.calls[-1][1]
        self.assertIsInstance(target, SessionHandle)
        self.assertEqual(target.native_id, first['native_id'])
        self.assertEqual(target.workspace, first['workspace'])
        self.assertEqual(target.session_file, first['session_file'])
        self.assertEqual(target.launch_id, first['launch_id'])

    def test_launch_effort_is_explicit_retained_and_never_silently_ignored(self):
        """Effort must reach spawn and every resume, or settings commands and explicit overrides are silently undone."""
        from multiverse.cli import parser
        before = (self.directory / 'properties.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'does not support --effort'):
            resume.run(self.root, agent=self.agent, effort='high')
        self.assertEqual((self.directory / 'properties.json').read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'does not support --effort'):
            spawn.run(self.root, name='unsupported-effort', harness='fourth', effort='high')
        with self.assertRaises(agents.AgentNotFound):
            agents.directory(self.root, 'unsupported-effort')
        # Choosing the harness default needs neither effort support nor a known model.
        default = spawn.run(self.root, name='default-effort', harness='fourth', effort='default')
        self.assertIsNone(default.get('effort'))
        self.assertIsNone(self.adapter.calls[-1][-1].effort)
        self.adapter.supports_launch_effort = True
        self.adapter.check_model = Mock(return_value=ModelCheck(ok=True, verified=False))
        self.adapter.calls.clear()
        with self.assertRaisesRegex(ValueError, 'Give --model with --effort'):
            resume.run(self.root, agent=self.agent, effort='high')
        self.assertEqual((self.directory / 'properties.json').read_bytes(), before)
        self.assertEqual(self.adapter.calls, [])
        self.adapter.check_model.assert_not_called()
        for argv, expected in ((['spawn', '--name', 'effort-worker', '--harness', 'fourth',
                                '--model', 'initial', '--effort', 'high'], 'high'),
                               (['resume', 'effort-worker'], 'high'),
                               (['resume', 'effort-worker', '--model', 'new', '--effort', 'low'], 'low'),
                               (['resume', 'effort-worker'], 'low')):
            with self.subTest(argv=argv):
                options = vars(parser().parse_args(argv))
                action = options.pop('action')
                options['root'] = self.root
                result = (spawn if action == 'spawn' else resume).run(**options)
                context = self.adapter.calls[-1][-1]
                self.assertEqual(context.effort, expected)
                self.assertEqual(result['effort'], expected)
                directory = agents.directory(self.root, result['id'])
                self.assertEqual(read(directory / 'properties.json')['effort'], expected)
        self.assertEqual(context.model, 'new')
        self.adapter.check_model.return_value = ModelCheck(ok=False, reason='This model does not take high effort')
        before = (directory / 'properties.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'does not take high effort'):
            resume.run(self.root, agent=result['id'], effort='high')
        self.assertEqual((directory / 'properties.json').read_bytes(), before)

        def check(model, *, effort=None, environment):
            levels = {'initial': ('low', 'high'), 'other': ('low', 'high'),
                      'limited': ('low',), 'no-effort': ()}.get(model)
            if levels is None:
                return ModelCheck(ok=False, reason='Model is unavailable')
            if effort is not None and effort not in levels:
                return ModelCheck(ok=False, reason='Model does not take this effort')
            return ModelCheck(ok=True, model=model, effort=effort, verified=True)

        self.adapter.check_model.side_effect = check
        cases = (
            ('model without effort', 'initial', {'model': 'no-effort'}, None, True, None),
            ('model without the saved level', 'initial', {'model': 'limited'}, None, True, None),
            ('model accepts the saved level', 'initial', {'model': 'other'}, 'high', False, None),
            ('explicit reset', 'initial', {'effort': 'default'}, None, True, None),
            ('reset without a recorded model', None, {'effort': 'default'}, None, True, None),
            ('reset while changing model', 'initial', {'model': 'no-effort', 'effort': 'default'}, None, True, None),
            ('plain resume retains effort', 'initial', {}, 'high', False, None),
            ('explicit incompatible effort', 'initial', {'model': 'no-effort', 'effort': 'high'}, None, False,
             'does not take this effort'),
            ('incompatible record without a model change', 'no-effort', {}, None, False,
             "effort.*agent's record"),
            ('invalid model is not repaired by dropping effort', 'initial', {'model': 'missing'}, None, False,
             'Model is unavailable'),
        )
        for label, recorded_model, options, expected, notice, problem in cases:
            with self.subTest(label):
                properties = read(directory / 'properties.json')
                properties.update(model=recorded_model, effort='high')
                write(directory / 'properties.json', properties)
                before = (directory / 'properties.json').read_bytes()
                if problem:
                    with self.assertRaisesRegex(ValueError, problem):
                        resume.run(self.root, agent=properties['id'], **options)
                    self.assertEqual((directory / 'properties.json').read_bytes(), before)
                    continue
                resumed = resume.run(self.root, agent=properties['id'], **options)
                self.assertEqual(self.adapter.calls[-1][-1].effort, expected)
                self.assertEqual(resumed.get('effort'), expected)
                self.assertEqual(read(directory / 'properties.json').get('effort'), expected)
                notices = ' '.join(resumed.get('notices', []))
                self.assertEqual('recorded effort' in notices, notice)

        properties = read(directory / 'properties.json')
        properties.update(model='initial', effort='high')
        write(directory / 'properties.json', properties)
        before = (directory / 'properties.json').read_bytes()
        prepare = self.adapter.resume

        def refuse_default(session, context):
            if context.effort is None:
                raise ValueError('Default launch refused')
            return prepare(session, context)

        with patch.object(self.adapter, 'resume', side_effect=refuse_default), \
                self.assertRaisesRegex(ValueError, 'Default launch refused'):
            resume.run(self.root, agent=properties['id'], model='no-effort')
        self.assertEqual((directory / 'properties.json').read_bytes(), before)

    def test_resume_settles_a_no_wait_command_before_applying_explicit_overrides(self):
        """A resume must not undo an unread command receipt, but its explicit model and effort still win."""
        self.adapter.supports_launch_effort = True
        self.adapter.check_model = Mock(return_value=ModelCheck(ok=True))
        self.adapter.slash_commands = lambda session, executor: NativeResult('ok', value=[
            SlashCommand(name='model', changes_settings=True)])
        self.adapter.run_slash = lambda session, request, executor: NativeResult('unknown')
        self.adapter.slash_result = lambda session, request, executor: NativeResult('unknown')
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.adapter.slash_result = lambda session, request, executor: NativeResult('unknown')
                queued = slash.run(self.root, self.agent, 'model', 'command-model', wait=False)
                self.adapter.slash_result = lambda session, request, executor: NativeResult('ok', value={
                    'state': 'ran', 'model': 'command-model', 'effort': 'high', 'session_id': self.agent})
                result = resume.run(self.root, agent=self.agent, **({'model': 'explicit-model', 'effort': 'low'}
                                                                   if explicit else {}))
                expected = ('explicit-model', 'low') if explicit else ('command-model', 'high')
                self.assertEqual((result['model'], result['effort']), expected)
                context = self.adapter.calls[-1][-1]
                self.assertEqual((context.model, context.effort), expected)
                self.assertEqual(slash.result(self.root, self.agent, queued.id).state, 'ran')
                self.assertFalse((self.directory / 'slash/pending.json').exists())
        self.adapter.slash_commands = lambda session, executor: NativeResult('ok', value=[
            SlashCommand(name=name, changes_settings=True) for name in ('model', 'effort')])
        self.adapter.slash_result = lambda session, request, executor: NativeResult('unknown', 'unreadable receipt')
        for setting in ('model', 'effort'):
            with self.subTest(unresolved=setting):
                queued = slash.run(self.root, self.agent, setting, 'high', wait=False)
                before = (self.directory / 'properties.json').read_bytes()
                other = 'effort' if setting == 'model' else 'model'
                for overrides in ({}, {other: 'explicit'}):
                    with self.assertRaisesRegex(ValueError, queued.id) as caught:
                        resume.run(self.root, agent=self.agent, **overrides)
                    self.assertIn('--result ' + queued.id, str(caught.exception))
                    self.assertIn('--' + setting + ' VALUE', str(caught.exception))
                    self.assertEqual((self.directory / 'properties.json').read_bytes(), before)
                # Observation remains usable, and explicitly choosing this one setting is enough.
                properties = read(self.directory / 'properties.json')
                self.assertEqual(native.session_snapshot(self.root, self.directory, properties,
                                 agents.runtime(self.directory), adapter=self.adapter)[0], properties)
                result = resume.run(self.root, agent=self.agent, **{setting: 'explicit'})
                self.assertEqual(result[setting], 'explicit')
        queued = slash.run(self.root, self.agent, 'model', 'lost', wait=False)
        self.adapter.slash_result = lambda session, request, executor: NativeResult('unknown', value={'absent': True})
        with patch.object(slash.delivery_evidence, 'launch_ended', return_value=True):
            resume.run(self.root, agent=self.agent)
        self.assertEqual(slash.result(self.root, self.agent, queued.id).state, 'lost')
