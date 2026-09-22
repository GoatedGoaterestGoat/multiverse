"""Codex interactive CLI lifecycle and exact-thread native queue specifications."""
from pathlib import Path
import json
import tomllib
from ..base import ConfiguredAdapter
from functools import partial
from dataclasses import replace

from ..types import SessionHandle, Message, Executor, Capability, CommandSpec, NativeResult, RpcSpec, SharedServiceSpec
from .protocol import check_connection, send_message, get_delivery, environment_exclusions, stop_session


class CodexAdapter(ConfiguredAdapter):
    package = Path(__file__).parent
    supports_bridge = True

    def _configure_launch(self):
        if self.settings.get('inbox_bridge'):
            for key in ('args', 'spawn_args', 'resume_args'):
                if any(arg == '--remote' or arg.startswith('--remote=') or arg == '--no-daemon'
                       for arg in self.settings.get(key, [])):
                    raise ValueError('inbox_bridge conflicts with a custom --remote or --no-daemon argument')

    def validate_resume(self, properties, agent_dir):
        super().validate_resume(properties, agent_dir)
        settings = getattr(self, 'settings', {})
        if settings.get('inbox_bridge'):
            permission_flags = {'--sandbox', '-s', '--ask-for-approval', '-a', '--add-dir', '--approve-for-me',
                                '--dangerously-bypass-approvals-and-sandbox', '--profile', '-p'}
            args = [*settings.get('args', []), *settings.get('resume_args', [])]
            if any(arg.split('=', 1)[0] in permission_flags for arg in args):
                raise ValueError('Codex native remote resume rejects explicit permission/profile overrides. '
                                 'Use spawn_args for initial grants; native resume retains saved permissions. '
                                 'Your configuration has not been changed.')

    def _launch_command(self, context):
        command = super()._launch_command(context)
        if not getattr(self, 'settings', {}).get('inbox_bridge'):
            return command
        argv = list(command.argv)
        if context.mode == 'resume':
            # Native resume retains the original writable grant.
            position = argv.index('--add-dir')
            if argv[position + 1] != context.values['agent_dir']:
                raise ValueError('Unexpected built-in Codex writable directory')
            del argv[position:position + 2]
        position = argv.index('--') if '--' in argv else len(argv)
        options = ['--remote', 'unix://' + context.endpoint.address if context.endpoint else 'unix://']
        if context.executor:
            observed = context.executor.rpc(RpcSpec(context.endpoint.address,
                partial(environment_exclusions, context.values['workspace'])))
            if observed.state != 'ok':
                raise ValueError(f'Cannot isolate shared native session environment: {observed.reason}')
            excludes = list(observed.value)
            # Preserve literal per-launch exclusions as well as native project/user config.
            for index, arg in enumerate(argv[:position]):
                value = argv[index + 1] if arg in ('-c', '--config') and index + 1 < len(argv) else (
                    arg[len('--config='):] if arg.startswith('--config=') else
                    arg[2:].removeprefix('=') if arg.startswith('-c') and not arg.startswith('--') else '')
                key, _, literal = value.partition('=')
                if key == 'shell_environment_policy.exclude':
                    extra = tomllib.loads('value=' + literal)['value']
                    if not isinstance(extra, list) or any(not isinstance(v, str) for v in extra):
                        raise ValueError('shell_environment_policy.exclude must be an array of strings')
                    excludes.extend(extra)
                elif key == 'shell_environment_policy':
                    raise ValueError('Shared daemon mode requires individual shell_environment_policy keys; configuration unchanged')
            excludes = list(dict.fromkeys([*excludes, 'MV2_*', *command.unset_env]))
            options += ['-c', 'shell_environment_policy.exclude=' + json.dumps(excludes)]
        # Tools run in the daemon, not the terminal process. Forward only this
        # launch's identity explicitly; never inherit another client's MV2_*.
        for key, value in context.environment.items():
            if key.startswith('MV2_') or key == 'PATH':
                options += ['-c', f'shell_environment_policy.set.{key}={json.dumps(value, ensure_ascii=False)}']
        argv[position:position] = options
        return CommandSpec(tuple(argv), command.unset_env,
                           shared_service=SharedServiceSpec(
                               (self.spec['spawn'][0], 'app-server', 'daemon', 'start'), 'socketPath', ('cliVersion', 'appServerVersion'),
                               {'CODEX_HOME': str(Path(context.environment.get('CODEX_HOME', '~/.codex')).expanduser().resolve())}))

    def spawn(self, context):
        return super().spawn(context)

    def stop(self, session, executor):
        if not session.endpoint:
            return super().stop(session, executor)
        if not session.native_id:
            return NativeResult('unavailable', 'Bind the exact native session ID before stopping shared-server work')
        return executor.rpc(RpcSpec(session.endpoint.address, partial(stop_session, session), max_requests=140))

    def capabilities(self, session: SessionHandle, executor: Executor | None = None) -> dict[str, Capability]:
        result = super().capabilities(session, executor)
        available = (session.state == 'running' and session.native_id and session.endpoint
                     and session.endpoint.transport == 'unix-websocket')
        support = Capability('unavailable', 'No running Codex app-server session', required=True)
        if available:
            support = Capability('unavailable', 'Native API has not been checked', required=True)
            if executor:
                try:
                    observed = executor.rpc(RpcSpec(session.endpoint.address, partial(check_connection, session)), timeout=5)
                    support = Capability('supported' if observed.state == 'ok' else 'error' if observed.state == 'unknown' else observed.state,
                                         observed.reason, 'codex-app-server', required=True)
                except Exception as error:
                    support = Capability('error', str(error), required=True)
        result['send_steer'] = result['send_follow_up'] = support
        result['get_delivery'] = replace(support, required=False)
        if session.endpoint:
            result['stop'] = replace(support, required=False)
        return result

    def get_delivery(self, session, message_id, executor):
        if not session.native_id or not session.endpoint or session.endpoint.transport != 'unix-websocket':
            return NativeResult('unavailable', 'No Codex app-server endpoint')
        return executor.rpc(RpcSpec(session.endpoint.address, partial(get_delivery, session, message_id)))

    def send(self, session: SessionHandle, message: Message, executor: Executor) -> NativeResult:
        mode = message.delivery
        if mode not in ('steer', 'follow-up'):
            return NativeResult('unavailable', f'Unknown delivery mode: {mode}')
        if not session.native_id or not session.endpoint or session.endpoint.transport != 'unix-websocket':
            return NativeResult('unavailable', 'Native delivery requires a Codex app-server launch and bound session ID')
        return executor.rpc(RpcSpec(session.endpoint.address, partial(send_message, session, message, mode)))
