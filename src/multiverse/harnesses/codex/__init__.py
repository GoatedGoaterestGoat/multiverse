"""Codex interactive CLI lifecycle and exact-thread native queue specifications."""
from pathlib import Path
import json
import tomllib
from ..base import ConfiguredAdapter
from functools import partial
from dataclasses import replace

from ..types import SessionHandle, Message, Executor, Capability, CommandSpec, NativeResult, RpcSpec, SharedServiceSpec, RpcProxySpec, NativeEndpoint
from .launch import launch_settings
from .protocol import check_connection, send_message, get_delivery, stop_session


class CodexAdapter(ConfiguredAdapter):
    package = Path(__file__).parent
    supports_bridge = True

    def _configure_launch(self):
        if self.settings.get('inbox_bridge'):
            for key in ('args', 'spawn_args', 'resume_args'):
                if any(arg == '--remote' or arg.startswith('--remote=') or arg == '--no-daemon'
                       for arg in self.settings.get(key, [])):
                    raise ValueError('inbox_bridge conflicts with a custom --remote or --no-daemon argument')

    def resume(self, session, context):
        settings = getattr(self, 'settings', {})
        if settings.get('inbox_bridge'):
            permission_flags = {'--sandbox', '-s', '--ask-for-approval', '-a', '--add-dir', '--approve-for-me',
                                '--dangerously-bypass-approvals-and-sandbox', '--profile', '-p'}
            args = [*settings.get('args', []), *settings.get('resume_args', [])]
            if any(arg.split('=', 1)[0] in permission_flags for arg in args):
                raise ValueError('Codex native remote resume rejects explicit permission/profile overrides. '
                                 'Use spawn_args for initial grants; native resume retains saved permissions. '
                                 'Your configuration has not been changed.')
        return super().resume(session, context)

    def _launch_command(self, context):
        command = super()._launch_command(context)
        if not getattr(self, 'settings', {}).get('inbox_bridge'):
            return command
        argv = list(command.argv)
        from .launch import remote_arguments
        argv, bootstrap, roots = remote_arguments(argv, context)
        position = argv.index('--') if '--' in argv else len(argv)
        workspace_policy = {}
        options = ['--remote', 'unix://' + context.values['control_socket'] if context.endpoint else 'unix://']
        if context.executor:
            observed = context.executor.rpc(RpcSpec(context.endpoint.address,
                partial(launch_settings, context.values['workspace'])))
            if observed.state != 'ok':
                raise ValueError(f'Cannot isolate shared native session environment: {observed.reason}')
            excludes = list(observed.value['exclude'])
            roots = list(dict.fromkeys([*observed.value['writable_roots'], *roots]))
            workspace_policy = observed.value.get('workspace_policy', {})
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
                           control_endpoint=NativeEndpoint('unix-websocket', context.values['control_socket']),
                           rpc_proxy=RpcProxySpec(context.values['control_socket'],
                               context.endpoint.address if context.endpoint else '',
                               {'bootstrap': bootstrap, 'roots': roots, 'workspace_policy': workspace_policy,
                                'workspace': context.values['workspace'],
                                'name': context.values['name'],
                                'native_id': context.values.get('session_id') if context.mode == 'resume' else None}),
                           shared_service=SharedServiceSpec(
                               (self.spec['spawn'][0], 'app-server', 'daemon', 'start'), 'socketPath', ('cliVersion', 'appServerVersion'),
                               {'CODEX_HOME': str(Path(context.environment.get('CODEX_HOME', '~/.codex')).expanduser().resolve())}))

    def launch_proxy(self, settings):
        from .launch import LaunchProtocol
        return LaunchProtocol(settings)

    def spawn(self, context):
        return super().spawn(context)

    def status_watch(self, session):
        """Optional passive stream: core runs the plan and owns its lifetime."""
        if (not self.settings.get('inbox_bridge') or not session.native_id
                or not session.endpoint or session.endpoint.transport != 'unix-websocket'):
            return None
        from .status import snapshot, notification
        return (RpcSpec(session.endpoint.address, partial(snapshot, session), max_requests=2),
                partial(notification, session))

    def stop(self, session, executor):
        if not session.endpoint:
            return super().stop(session, executor)
        if not session.native_id:
            return NativeResult('unavailable', 'Bind the exact native session ID before stopping shared-server work')
        return executor.rpc(RpcSpec(session.endpoint.address, partial(stop_session, session), max_requests=140))

    def capabilities(self, session: SessionHandle, executor: Executor | None = None) -> dict[str, Capability]:
        result = super().capabilities(session, executor)
        result['session_name'] = (Capability('supported', mechanism='thread/name/set')
            if self.settings.get('inbox_bridge') else Capability(
                'unavailable', 'Codex CLI has no name flag; native naming requires inbox_bridge'))
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
        if self.settings.get('inbox_bridge') and session.native_id and session.session_file:
            result['get_usage'] = Capability('supported', mechanism='codex-app-server-context')
        return result

    def get_usage(self, session, executor=None):
        if not self.settings.get('inbox_bridge'):
            return NativeResult('unavailable', 'Codex context reporting requires inbox_bridge')
        from .usage import measured
        return measured(session)

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
