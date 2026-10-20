"""Shared argv rendering and default operations; provider behavior stays in provider packages."""
from dataclasses import replace
import json
from pathlib import Path
import re
from string import Formatter

from .types import CAPABILITIES, Capability, CommandSpec, LaunchContext, NativeResult, SessionHandle, Message, Executor
from .types import normalize_session_id

FIELDS = {'name', 'agent_id', 'session_id', 'bootstrap', 'bootstrap_json', 'task', 'agent_dir',
          'harness_dir', 'session_file', 'bootstrap_file', 'task_file', 'workspace', 'model', 'adapter_dir'}
# A bare TOML key as well, so Codex can forward the variable to its tools unquoted.
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
# mv2 sets PATH for every launch, always removes the parent markers, and resolves
# native storage from HOME; a harness cannot replace them (nor any MV2_* name).
RESERVED_ENV = frozenset({'PATH', 'HOME', 'CODEX_THREAD_ID', 'CLAUDECODE'})
# What a parent Claude Code session leaves in its subprocesses' environment, and so in a tmux server first started
# from one: its identity, which a Claude reads at startup (CLAUDECODE, the child-session marker that turns
# transcript saving off, the session, attended and bridge IDs, the entry point and worktree), its IDE port and inbox,
# and its tool shells' own values. Claude Code removes the same identity when it starts a fresh Claude itself
# (2.1.303). Generic names a user may set on purpose (TRACEPARENT, BUN_OPTIONS, AI_AGENT) are left alone.
PARENT_CLAUDE = ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_SESSION_ATTENDED',
                 'CLAUDE_CODE_BRIDGE_SESSION_ID', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_HOST_WORKTREE',
                 'CLAUDE_CODE_HOST_WORKTREE_FENCE', 'CLAUDE_CODE_EVAL_INTERVIEW_SESSION',
                 'CLAUDE_CODE_SSE_PORT', 'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
                 'CLAUDE_PID', 'CLAUDE_EFFORT', 'CLAUDE_CODE_TMPDIR', 'CLAUDE_CODE_EXECPATH',
                 'CLAUDE_CODE_INVOKED_SKILLS')


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
    # The built-in adapter a harness uses ('codex', 'claude', 'pi'); '' for a local definition.
    family = ''
    resume_prompt_args = None
    # Variables mv2 itself reads to find this harness's native sessions; they come from mv2's environment.
    storage_env = ()

    @classmethod
    def configured(cls, name, settings):
        unknown = set(settings) - {'executable', 'model', 'args', 'spawn_args', 'resume_args', 'unset_env', 'env',
                                   'inbox_bridge'}
        if unknown:
            raise ValueError(f'Unknown harness settings: {sorted(unknown)}')
        bridge = settings.get('inbox_bridge', False)
        if not isinstance(bridge, bool):
            raise ValueError('inbox_bridge must be true or false')
        if bridge and not cls.supports_bridge:
            raise ValueError('inbox_bridge is not supported by this adapter')
        spec = json.loads((cls.package / 'defaults.json').read_text())
        for key in ('model', 'unset_env', 'env'):
            if key in settings:
                spec[key] = settings[key]
        if isinstance(settings.get('env'), dict) and 'unset_env' not in settings:
            # A variable the harness sets is no longer removed by default (NO_COLOR, say).
            spec['unset_env'] = [key for key in spec.get('unset_env', []) if key not in settings['env']]
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
        self.environment = self._environment(spec.get('env', {}), unset)
        self.session_mode = spec.get('session', 'file')
        if self.session_mode not in ('file', 'assigned', 'reported'):
            raise ValueError('Unsupported harness session mode')
        self.bootstrap = spec.get('bootstrap', '')
        self.default_model = spec.get('model')
        if not isinstance(self.bootstrap, str) or (self.default_model is not None and not isinstance(self.default_model, str)):
            raise ValueError('bootstrap and model must be strings')

    def _environment(self, env, unset):
        if not isinstance(env, dict) or not all(
                isinstance(key, str) and ENV_NAME.fullmatch(key) and isinstance(value, str) and '\0' not in value
                for key, value in env.items()):
            raise ValueError('env must map environment variable names to strings without NUL characters')
        managed = sorted(key for key in env if key.startswith('MV2_') or key in RESERVED_ENV)
        if managed:
            raise ValueError(f'env cannot set {", ".join(managed)}: mv2 manages these for every launch')
        storage = sorted(key for key in env if key in self.storage_env)
        if storage:
            raise ValueError(f'env cannot set {", ".join(storage)}: mv2 reads these to find this harness\'s '
                             'sessions, so set them in the environment mv2 runs in')
        both = sorted(set(env) & set(unset))
        if both:
            raise ValueError(f'env and unset_env both name {", ".join(both)}')
        return dict(env)

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
        if context.mode == 'resume' and context.resume_prompt is not None:
            if self.resume_prompt_args is None:
                raise ValueError('This adapter has no verified interactive resume prompt; wake is unavailable')
            prompt_args = list(self.resume_prompt_args)
            if '--' in argv and prompt_args and prompt_args[0] == '--':
                prompt_args = prompt_args[1:]
            argv.extend(arg.format_map({**values, 'task': context.resume_prompt}) for arg in prompt_args)
        # Scrubbing parent identity is a core invariant, including legacy definitions, and so is Claude's
        # child-session marker, which turns transcript saving off, and a parent Claude's IDE port and inbox path
        # and secret (an agent spawned from inside Claude inherits them): a harness replacing unset_env keeps them.
        unset = tuple(dict.fromkeys(['CODEX_THREAD_ID', *PARENT_CLAUDE, *self.spec.get('unset_env', [])]))
        return CommandSpec(tuple(argv), unset, environment=dict(self.environment))

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
        if session.state in ('stopped', 'dormant'):
            return NativeResult('ok', 'No native process was running')
        return NativeResult('ok', 'Core stops the owned terminal process')

    def connection_problem(self, session: SessionHandle) -> str | None:
        """Optional cheap, read-only diagnostic for this exact live launch."""
        return None

    def session_home(self, environment) -> str | None:
        """Optional native storage root, resolved from the launch's environment."""
        return None

    def session_storage_evidence(self, session, home) -> NativeResult:
        """Read-only exact conversation evidence in an explicit absolute storage root."""
        return NativeResult('unknown', 'This harness cannot verify a legacy conversation storage location')

    def capabilities(self, session: SessionHandle, executor: Executor | None = None) -> dict[str, Capability]:
        result = {name: Capability('unavailable', 'No native implementation',
                    required=name in ('spawn', 'resume', 'send_steer', 'send_follow_up')) for name in CAPABILITIES}
        result['session_name'] = Capability('unavailable', 'Native session naming is not verified for this manifest')
        for operation in ('send_steer', 'send_follow_up'):
            result[operation] = replace(result[operation], mechanism='launch-only',
                reason='This agent is launch-only: its harness has no way to deliver into a running session')
        result['stop'] = Capability('supported', mechanism='terminal')
        result['spawn'] = Capability('supported', mechanism='terminal', required=True)
        if 'resume' in self.spec:
            result['resume'] = Capability('supported', mechanism=self.session_mode, required=True)
            result['wake'] = (Capability('supported', mechanism='interactive-resume-prompt')
                if self.resume_prompt_args is not None else Capability(
                    'unavailable', 'No verified interactive resume prompt for this adapter'))
        return result

    def send(self, session: SessionHandle, message: Message, executor: Executor) -> NativeResult:
        return NativeResult('unavailable', 'No verified native message delivery for this session', {'launch_only': True})

    def get_delivery(self, session, message_id, executor):
        return NativeResult('unavailable', 'Native receipt reconciliation is not implemented')

    def ask(self, session: SessionHandle, question: str, executor: Executor) -> NativeResult:
        return NativeResult('unavailable', 'No isolated native side-question implementation; main conversation unchanged')

    def get_usage(self, session: SessionHandle, executor: Executor | None = None) -> NativeResult:
        return NativeResult('unavailable', 'No measured native session usage collector')

    def attachment_recovery(self, session):
        """Unsupported unless a provider supplies a read-only safety plan."""
        return None

    def attachment_ready(self, session):
        # Existing terminal integrations declare readiness by bridge registration.
        return (not session.endpoint or bool(session.native_id and
                self.capabilities(session)['send_steer'].state == 'supported'))

    def retained_usage(self, session, runtime):
        return None
