"""Shared argv rendering and default operations; provider behavior stays in provider packages."""
from dataclasses import asdict, replace
import json
from string import Formatter
from uuid import UUID

from .types import NATIVE_CAPABILITIES, Capability, CommandSpec, LaunchContext, NativeContext, NativeResult, SessionHandle, Message, DeliveryMode, Executor

FIELDS = {'name', 'agent_id', 'session_id', 'bootstrap', 'bootstrap_json', 'task', 'agent_dir',
          'harness_dir', 'session_file', 'bootstrap_file', 'task_file', 'workspace', 'model', 'adapter_dir'}


def arguments(value, label, required=False):
    if not isinstance(value, list) or not all(isinstance(arg, str) and '\0' not in arg for arg in value):
        raise ValueError(f'{label} must be an array of strings without NUL characters')
    if required and (not value or not value[0]):
        raise ValueError(f'{label} must contain an executable')
    for arg in value:
        try:
            for _, field, fmt, conversion in Formatter().parse(arg):
                if field is not None and (field not in FIELDS or fmt or conversion):
                    raise ValueError(f'Unsupported placeholder in {label}: {field}')
        except ValueError as error:
            raise ValueError(f'Invalid {label}: {error}') from error
    return value


class ConfiguredAdapter:
    package = None
    supports_bridge = False

    @classmethod
    def configured(cls, name, settings):
        unknown = set(settings) - {'executable', 'model', 'args', 'spawn_args', 'resume_args', 'unset_env', 'inbox_bridge'}
        if unknown:
            raise ValueError(f'Unknown harness settings: {sorted(unknown)}')
        bridge = settings.get('inbox_bridge', False)
        if not isinstance(bridge, bool) or (bridge and not cls.supports_bridge):
            raise ValueError('inbox_bridge is not supported by this adapter')
        spec = json.loads((cls.package / 'defaults.json').read_text())
        for key in ('model', 'unset_env'):
            if key in settings:
                spec[key] = settings[key]
        executable = settings.get('executable', spec['spawn'][0])
        if not isinstance(executable, str) or not executable or '\0' in executable:
            raise ValueError('executable must be a nonempty string')
        for mode in ('spawn', 'resume'):
            argv = spec[mode]
            argv[0] = executable
            extra = [*arguments(settings.get('args', []), 'args'),
                     *arguments(settings.get(mode + '_args', []), mode + '_args')]
            position = argv.index('--') if '--' in argv else 1
            argv[position:position] = extra
        result = cls(name, spec)
        result.settings = settings
        result.configure_native()
        return result

    def configure_native(self):
        pass

    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        for mode in ('spawn', 'resume'):
            if mode in spec:
                arguments(spec[mode], mode, required=True)
        if 'spawn' not in spec:
            raise ValueError('Harness does not support spawn')
        arguments(spec.get('model_args', []), 'model_args')
        unset = spec.get('unset_env', [])
        if not isinstance(unset, list) or not all(isinstance(k, str) and k and '=' not in k and '\0' not in k for k in unset):
            raise ValueError('unset_env must contain environment variable names')
        self.session_mode = spec.get('session', 'file')
        if self.session_mode not in ('file', 'assigned', 'reported'):
            raise ValueError('Unsupported harness session mode')
        self.bootstrap = spec.get('bootstrap', '')
        self.default_model = spec.get('model')
        if not isinstance(self.bootstrap, str) or (self.default_model is not None and not isinstance(self.default_model, str)):
            raise ValueError('bootstrap and model must be strings')

    def session_id(self, value):
        return str(UUID(value))

    def validate_resume(self, properties, agent_dir):
        if 'resume' not in self.spec:
            raise ValueError('Harness does not support resume')
        if self.session_mode != 'file':
            if not properties.get('native_session_id'):
                raise ValueError('No native session ID recorded. Use `mv2 session-bind --agent AGENT --session-id UUID` '
                                 'with the ID from the harness, then resume. No latest-session fallback is used.')
            self.session_id(properties['native_session_id'])
        elif any('{session_file}' in arg for arg in self.spec['resume']) and not (agent_dir / 'session.jsonl').is_file():
            raise ValueError('Native session file is missing; refusing to create a fresh session on resume')

    def launch(self, context: LaunchContext):
        if context.mode not in self.spec:
            raise ValueError(f'Harness does not support {context.mode}')
        values = {**context.values, 'model': context.model or ''}
        recipe = self.spec[context.mode]
        if self.package and context.mode == 'spawn' and not values.get('task'):
            recipe = recipe[:-1]  # each built-in recipe ends with its initial prompt
            if recipe and recipe[-1] == '--':
                recipe = recipe[:-1]
        argv = [arg.format_map(values) for arg in recipe]
        if context.model:
            position = argv.index('--') if '--' in argv else len(argv)
            argv[position:position] = [arg.format_map(values) for arg in self.spec.get('model_args', [])]
        # Scrubbing parent identity is a core invariant, including legacy definitions.
        unset = tuple(dict.fromkeys(['CODEX_THREAD_ID', 'CLAUDECODE', *self.spec.get('unset_env', [])]))
        return CommandSpec(tuple(argv), unset)

    def native_capabilities(self, context: NativeContext):
        result = {name: Capability('unavailable', 'No native interactive implementation',
                                   required=name in ('launch', 'resume', 'steer', 'follow_up')) for name in NATIVE_CAPABILITIES}
        result['launch'] = Capability('supported', mechanism='terminal', required=True)
        if 'resume' in self.spec:
            result['resume'] = Capability('supported', mechanism=self.session_mode, required=True)
        return result

    def probes(self, context=None):
        return {}

    def control(self, operation, context, text):
        return NativeResult('unavailable', f'{operation} is unavailable for this native session')

    def spawn(self, context: LaunchContext) -> CommandSpec:
        return self.launch(replace(context, mode='spawn'))

    def resume(self, context: LaunchContext) -> CommandSpec:
        return self.launch(replace(context, mode='resume'))

    def capabilities(self, session: SessionHandle, executor: Executor | None = None) -> dict[str, Capability]:
        ctx = session.context()
        if executor:
            ctx = replace(ctx, probes=executor.probe(self, ctx))
        native = self.native_capabilities(ctx)
        mapping = {'spawn': 'launch', 'resume': 'resume', 'send_steer': 'steer',
                   'send_follow_up': 'follow_up', 'ask': 'ask', 'get_usage': 'usage'}
        return {**{key: native[value] for key, value in mapping.items()},
                'observe': Capability('supported', 'Core launch state; native activity may be unknown')}

    def send(self, session: SessionHandle, message: Message, mode: DeliveryMode, executor: Executor) -> NativeResult:
        return NativeResult('unavailable', 'No verified native message delivery for this session')

    def ask(self, session: SessionHandle, question: str, executor: Executor) -> NativeResult:
        return NativeResult('unavailable', 'No isolated native side-question implementation; main conversation unchanged')

    def get_usage(self, session: SessionHandle, executor: Executor | None = None) -> NativeResult:
        return NativeResult('unavailable', 'No measured native session usage collector')

    def observe(self, session: SessionHandle, executor: Executor | None = None) -> NativeResult:
        return NativeResult('ok', value={'session': asdict(session),
                                        'state': session.state, 'activity': session.activity})
