"""Shared argv rendering and default operations; provider behavior stays in provider packages."""
from dataclasses import replace
import json
from pathlib import Path
from string import Formatter

from .types import CAPABILITIES, Capability, CommandSpec, LaunchContext, NativeResult, SessionHandle, Message, Executor
from .types import normalize_session_id

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
        result._configure_launch()
        return result

    def _configure_launch(self):
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

    def _launch_command(self, context: LaunchContext):
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

    def spawn(self, context: LaunchContext) -> CommandSpec:
        return self._launch_command(replace(context, mode='spawn'))

    def resume(self, session: SessionHandle, context: LaunchContext) -> CommandSpec:
        if 'resume' not in self.spec:
            raise ValueError('Harness does not support resume')
        if session.harness != self.name:
            raise ValueError('Resume session belongs to a different harness')
        if self.session_mode != 'file' and not session.native_id:
            raise ValueError('No native session ID recorded. Bind the exact native session ID with '
                             '`mv2 session-bind --agent AGENT --session-id UUID` before resuming; no fallback is used.')
        native_id = normalize_session_id(session.native_id) if session.native_id is not None else ''
        if any('{session_file}' in arg for arg in self.spec['resume']):
            if not session.session_file or not Path(session.session_file).is_file():
                raise ValueError('Native session file is missing; refusing to create a fresh session on resume')
        values = {**context.values, 'agent_id': session.agent_id,
                  'session_id': native_id, 'session_file': session.session_file or '',
                  'workspace': session.workspace}
        return self._launch_command(replace(context, mode='resume', values=values))

    def stop(self, session: SessionHandle, executor: Executor) -> NativeResult:
        return NativeResult('ok', 'Core stops the owned terminal process')

    def capabilities(self, session: SessionHandle, executor: Executor | None = None) -> dict[str, Capability]:
        result = {name: Capability('unavailable', 'No native implementation',
                    required=name in ('spawn', 'resume', 'send_steer', 'send_follow_up')) for name in CAPABILITIES}
        result['session_name'] = Capability('unavailable', 'Native session naming is not verified for this manifest')
        result['stop'] = Capability('supported', mechanism='terminal')
        result['spawn'] = Capability('supported', mechanism='terminal', required=True)
        if 'resume' in self.spec:
            result['resume'] = Capability('supported', mechanism=self.session_mode, required=True)
        return result

    def send(self, session: SessionHandle, message: Message, executor: Executor) -> NativeResult:
        return NativeResult('unavailable', 'No verified native message delivery for this session')

    def get_delivery(self, session, message_id, executor):
        return NativeResult('unavailable', 'Native receipt reconciliation is not implemented')

    def ask(self, session: SessionHandle, question: str, executor: Executor) -> NativeResult:
        return NativeResult('unavailable', 'No isolated native side-question implementation; main conversation unchanged')

    def get_usage(self, session: SessionHandle, executor: Executor | None = None) -> NativeResult:
        return NativeResult('unavailable', 'No measured native session usage collector')
