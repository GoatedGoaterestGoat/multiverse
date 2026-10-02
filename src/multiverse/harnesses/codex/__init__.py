"""Codex interactive CLI lifecycle and exact-thread native queue specifications."""
from pathlib import Path
import json
import shlex
import tomllib
from ..base import ConfiguredAdapter
from functools import partial
from dataclasses import replace

from ..types import SessionHandle, Message, Executor, Capability, CommandSpec, NativeResult, RpcSpec, SharedServiceSpec, RpcProxySpec, NativeEndpoint
from .launch import launch_settings, tui_arguments
from .protocol import check_connection, send_message, get_delivery, stop_session
from ..types import BridgeUnavailable


class CodexAdapter(ConfiguredAdapter):
    package = Path(__file__).parent
    supports_bridge = True
    resume_prompt_args = ('{task}',)

    def _configure_launch(self):
        if self.settings.get('inbox_bridge') is True:
            for key in ('args', 'spawn_args', 'resume_args'):
                if any(arg == '--remote' or arg.startswith('--remote=') or arg == '--no-daemon'
                       for arg in self.settings.get(key, [])):
                    raise ValueError('inbox_bridge conflicts with a custom --remote or --no-daemon argument')

    def _launch_command(self, context):
        command = super()._launch_command(context)
        command = replace(command, argv=tuple(tui_arguments(command.argv)))
        setting = getattr(self, 'settings', {}).get('inbox_bridge')
        policy = 'auto' if setting is None else 'required' if setting else 'plain'
        plain = replace(command, native_mode={'policy': policy, 'mode': 'plain', 'fallback_reason': None})
        if setting is False:
            return plain
        try:
            bridge = self._bridge_command(command, context)
        except BridgeUnavailable as error:
            if setting is True:
                raise
            return replace(plain, native_mode={**plain.native_mode, 'fallback_reason': str(error)})
        return replace(bridge, native_mode={'policy': policy, 'mode': 'bridge', 'fallback_reason': None},
                       fallback=plain if setting is None else None)

    def _bridge_command(self, command, context):
        settings = getattr(self, 'settings', {})
        args = [*settings.get('args', []), *settings.get(context.mode + '_args', [])]
        if any(arg == '--remote' or arg.startswith('--remote=') or arg == '--no-daemon' for arg in args):
            raise BridgeUnavailable('Codex bridge conflicts with a custom --remote or --no-daemon argument')
        if context.mode == 'resume':
            unsupported = {'--add-dir', '--approve-for-me', '--profile', '-p'}
            if any(arg.split('=', 1)[0] in unsupported or arg.startswith('-p') and not arg.startswith('--') for arg in args):
                raise BridgeUnavailable('Codex bridge resume does not support writable-directory, profile or automatic-review overrides. '
                                        'Bridge resume uses native saved permissions and TUI settings. '
                                        'Use plain mode or put initial-only settings in spawn_args; configuration is unchanged.')
        argv = list(command.argv)
        from .launch import remote_arguments
        argv, bootstrap, roots, permissions = remote_arguments(argv, context)
        position = argv.index('--') if '--' in argv else len(argv)
        workspace_policy = {}
        options = ['--remote', 'unix://' + context.values['control_socket'] if context.endpoint else 'unix://']
        if context.executor:
            try:
                observed = context.executor.rpc(RpcSpec(context.endpoint.address,
                    partial(launch_settings, context.values['workspace'])))
            except (OSError, RuntimeError, ValueError) as error:
                raise BridgeUnavailable(f'Cannot prepare shared native session environment: {error}') from error
            if observed.state != 'ok':
                reason = f'Cannot isolate shared native session environment: {observed.reason}'
                if ('failed to resolve feature override precedence' in observed.reason
                        and 'No such file or directory' in observed.reason):
                    home = str(Path(context.environment.get('CODEX_HOME', '~/.codex')).expanduser().resolve())
                    executable = self.spec['spawn'][0]
                    if '/' in executable and not Path(executable).is_absolute():
                        executable = str((Path(context.values['workspace']) / executable).resolve())
                    restart = (f'cd {shlex.quote(str(Path.home().resolve()))} && '
                               f'CODEX_HOME={shlex.quote(home)} {shlex.quote(executable)} app-server daemon restart')
                    reason += ('; the shared daemon may have a deleted working directory. '
                               f'Only when other Codex work is idle, explicitly run `{restart}`, then retry resume. '
                               'mv2 does not restart shared daemons.')
                raise BridgeUnavailable(reason)
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
                                'resume_permissions': permissions,
                                'native_id': context.values.get('session_id') if context.mode == 'resume' else None}),
                           shared_service=SharedServiceSpec(
                               (self.spec['spawn'][0], 'app-server', 'daemon', 'start'), 'socketPath', ('cliVersion', 'appServerVersion'),
                               {'CODEX_HOME': str(Path(context.environment.get('CODEX_HOME', '~/.codex')).expanduser().resolve())},
                               required_cli_flags=('--remote',), cwd=str(Path.home().resolve()),
                               require_matching_versions=False))

    def launch_proxy(self, settings):
        from .launch import LaunchProtocol
        return LaunchProtocol(settings)

    def spawn(self, context):
        return super().spawn(context)

    def status_watch(self, session):
        """Optional passive stream: core runs the plan and owns its lifetime."""
        if (not self._uses_bridge(session) or not session.native_id
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
        selection = session.native_mode
        if selection:
            reason = f'Native mode: {selection["mode"]} ({selection["policy"]})'
            if selection.get('fallback_reason'):
                reason += '; fallback: ' + selection['fallback_reason']
        else:
            setting = self.settings.get('inbox_bridge')
            reason = ('Native mode: auto; selected at each spawn/resume' if setting is None else
                      'Native mode: bridge required' if setting else 'Native mode: plain (explicit opt-out)')
        if session.native_versions:
            versions = session.native_versions
            reason += f'; CLI {versions["cli"]}, daemon {versions["daemon"]}'
            if versions['cli'] != versions['daemon']:
                reason += ' (versions differ; launch requires native protocol validation)'
        for operation in ('spawn', 'resume'):
            result[operation] = replace(result[operation], reason=reason)
        result['session_name'] = (Capability('supported', mechanism='thread/name/set')
            if self._uses_bridge(session) else Capability(
                'unavailable', 'Codex CLI has no name flag. ' + reason))
        available = (session.state == 'running' and session.native_id and session.endpoint
                     and session.endpoint.transport == 'unix-websocket')
        support = Capability('unavailable', 'No running Codex app-server session. ' + reason, required=True)
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
        if session.native_relay and session.native_relay.get('state') == 'lost':
            result['send_steer'] = result['send_follow_up'] = Capability(
                'unavailable', session.native_relay['reason'], required=True)
        if self._uses_bridge(session) and session.native_id and session.session_file:
            result['get_usage'] = Capability('supported', mechanism='codex-app-server-context')
        return result

    def get_usage(self, session, executor=None):
        if not self._uses_bridge(session):
            return NativeResult('unavailable', 'Codex context reporting requires inbox_bridge')
        from .usage import measured
        return measured(session)

    def _uses_bridge(self, session):
        if session.native_mode:
            return session.native_mode.get('mode') == 'bridge'
        # Existing launches have no selection metadata. Their endpoint remains
        # authoritative; a configured-only strict profile still declares support.
        return bool((session.endpoint and session.endpoint.transport == 'unix-websocket') or
                    (not session.launch_id and self.settings.get('inbox_bridge') is True))

    def get_delivery(self, session, message_id, executor):
        if not session.native_id or not session.endpoint or session.endpoint.transport != 'unix-websocket':
            return NativeResult('unavailable', 'No Codex app-server endpoint')
        return executor.rpc(RpcSpec(session.endpoint.address, partial(get_delivery, session, message_id)))

    def send(self, session: SessionHandle, message: Message, executor: Executor) -> NativeResult:
        if session.native_relay and session.native_relay.get('state') == 'lost':
            return NativeResult('unavailable', session.native_relay['reason'])
        mode = message.delivery
        if mode not in ('steer', 'follow-up'):
            return NativeResult('unavailable', f'Unknown delivery mode: {mode}')
        if not session.native_id or not session.endpoint or session.endpoint.transport != 'unix-websocket':
            return NativeResult('unavailable', 'Native delivery requires a Codex app-server launch and bound session ID')
        if session.state != 'running':
            return NativeResult('unavailable', 'Native launch is still binding; retry after validation completes, or inspect runtime.json for a launch error')
        return executor.rpc(RpcSpec(session.endpoint.address, partial(send_message, session, message, mode)))
