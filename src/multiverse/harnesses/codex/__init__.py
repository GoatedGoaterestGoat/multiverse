"""Codex interactive CLI lifecycle and exact-thread native queue specifications."""
from pathlib import Path
import json
import shlex
import tomllib
from ..base import ConfiguredAdapter
from functools import partial
from dataclasses import replace
from collections.abc import Mapping

from ..types import (SessionHandle, Message, Executor, Capability, CommandSpec, NativeResult,
                     StartupRetrySpec, RpcSpec, SharedServiceSpec, RpcProxySpec, NativeEndpoint, ServiceIdentity, AttachmentSpec)
from .launch import launch_settings, tui_arguments, plain_arguments
from .protocol import (check_connection, send_message, get_delivery, stop_session, recovery_check,
                       RECOVERY_MAX_REQUESTS, continue_queued, CONTINUATION_MAX_REQUESTS)
from ..types import BridgeUnavailable


class CodexAdapter(ConfiguredAdapter):
    package = Path(__file__).parent
    supports_bridge = True
    family = 'codex'
    resume_prompt_args = ('{task}',)
    storage_env = ('CODEX_HOME', 'CODEX_SQLITE_HOME')

    def session_home(self, environment):
        from .storage import session_home
        return session_home(environment)

    def session_storage_evidence(self, session, home):
        from .storage import session_storage_evidence
        return session_storage_evidence(session, home)

    def agent_identity_policy(self):
        from .identity import POLICY
        return POLICY

    def allow_recorded_launch(self, session):
        return isinstance(session.native_mode, Mapping) and session.native_mode.get('mode') == 'bridge'

    def resume_notice(self, session, caller_environment, effective_environment):
        from .identity import resume_notice
        return resume_notice(caller_environment, effective_environment)

    def agent_identity_problem(self, session, environment):
        if (environment.get('MV2_IDENTITY_MODE') is None and environment.get('MV2_LAUNCH_ID')
                and environment.get('CODEX_THREAD_ID') and environment.get('MV2_AGENT_ID') == session.agent_id
                and (session.native_mode or {}).get('mode') == 'bridge'):
            return ('This Codex conversation has an unverified cached tool identity. Use the current mv2 '
                    "installation and inspect this agent's saved launch records. Codex can retain an idle "
                    'conversation for up to 30 minutes after its last client leaves.')
        return None

    def supports_launch_refusal(self, cli_version):
        from .refusal import supports_plain_refusal
        return supports_plain_refusal(cli_version)

    def startup_retryable(self, snapshot):
        from .launch import startup_retryable
        return startup_retryable(snapshot)

    def launch_refusal(self, evidence):
        from .refusal import plain_refusal
        return plain_refusal(evidence)

    def launch_started(self, evidence):
        from .refusal import plain_started
        return plain_started(evidence)

    def _configure_launch(self):
        if self.settings.get('inbox_bridge') is True:
            for key in ('args', 'spawn_args', 'resume_args'):
                if any(arg == '--remote' or arg.startswith('--remote=') or arg == '--no-daemon'
                       for arg in self.settings.get(key, [])):
                    raise ValueError('inbox_bridge conflicts with a custom --remote or --no-daemon argument')

    def _launch_command(self, context):
        command = super()._launch_command(context)
        command = replace(command, argv=tuple(tui_arguments(command.argv)), attachment=AttachmentSpec(mouse=True))
        from .plugins import prepare, codex_version, CHECKED_VERSION, version_text
        version = codex_version(command.argv[0], context.values['workspace'])
        if version is not None and version != CHECKED_VERSION:
            relation = 'older' if version < CHECKED_VERSION else 'newer'
            note = (f'Codex {version_text(version)} is {relation} than the checked release {version_text(CHECKED_VERSION)}; '
                    'mv2 startup screen checks are disabled. Native /btw requires a supported client and the checked daemon release.')
            command = replace(command, notices=(*command.notices, note))
        setting = getattr(self, 'settings', {}).get('inbox_bridge')
        policy = 'auto' if setting is None else 'required' if setting else 'plain'
        plain = None
        if setting is not True:
            from .identity import storage_environment
            additions = prepare(context, command, settings=getattr(self, 'settings', {}), harness_name=self.name)
            plain_command = additions.apply(command)
            argv, removable = plain_arguments(command.argv, self.spec[context.mode], context.values)
            retry = None
            if removable:
                name = json.dumps(context.values['name'])
                directory = json.dumps(context.values['agent_dir'])
                retry_argv = additions.apply(replace(command, argv=tuple(argv))).argv
                retry = StartupRetrySpec(retry_argv, (*plain_command.notices,
                    f"Codex runs this folder read-only, so {name}'s agent folder ({directory}) is not writable; "
                    "session-bind, report and inbox need approval to write there; with approval policy never, they fail. "
                    "To allow it, give the harness --sandbox workspace-write."))
            plain = replace(plain_command, startup_retry=retry,
                            environment={**plain_command.environment, **storage_environment(context)},
                            native_mode={'policy': policy, 'mode': 'plain', 'fallback_reason': None})
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
        from .launch import remote_arguments
        # Validate explicit grants before contacting the native service.
        remote_arguments(command.argv, context)
        native_settings = {}
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
                    home = str(Path(context.environment.get('CODEX_HOME') or '~/.codex').expanduser().resolve())
                    executable = self.spec['spawn'][0]
                    if '/' in executable and not Path(executable).is_absolute():
                        executable = str((Path(context.values['workspace']) / executable).resolve())
                    restart = (f'cd {shlex.quote(str(Path.home().resolve()))} && '
                               f'CODEX_HOME={shlex.quote(home)} {shlex.quote(executable)} app-server daemon restart')
                    reason += ('; the shared daemon may have a deleted working directory. '
                               f'Only when other Codex work is idle, explicitly run `{restart}`, then retry resume. '
                               'mv2 does not restart shared daemons.')
                raise BridgeUnavailable(reason)
            native_settings = observed.value
        from .identity import tool_environment, storage_environment
        stable = tool_environment(context)
        storage = storage_environment(context)
        tool_context = replace(context, environment={
            **{key: value for key, value in context.environment.items() if not key.startswith('MV2_')}, **stable})
        from .plugins import prepare, overrides
        additions = prepare(tool_context, command, bridge=True, daemon=native_settings.get('daemon_version'),
                            final=context.executor is not None, settings=getattr(self, 'settings', {}),
                            harness_name=self.name)
        command = additions.apply(command)
        argv = list(command.argv)
        argv, bootstrap, roots, permissions = remote_arguments(argv, context)
        # Bridge registration is the generation-owned relay's job. Cached old
        # bootstrap instructions are handled by core's already-bound verifier.
        explicit_instructions = any(key == 'developer_instructions' for key, _ in overrides(args))
        if self.bootstrap and not explicit_instructions:
            bootstrap = bootstrap.replace(self.bootstrap, '').strip()
        position = argv.index('--') if '--' in argv else len(argv)
        workspace_policy = native_settings.get('workspace_policy', {})
        options = ['--remote', 'unix://' + context.values['control_socket'] if context.endpoint else 'unix://']
        if context.executor:
            excludes = list(native_settings['exclude'])
            roots = list(dict.fromkeys([*native_settings['writable_roots'], *roots]))
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
        # Harness/plugin variables can contain credentials. Pass them to native
        # shell tools in the session RPC, never as process-list-visible flags.
        from .plugins import merge_config
        session_config = merge_config(
            {'shell_environment_policy': {'set': dict(command.environment)}} if command.environment else {},
            additions.session_config)
        argv[position:position] = options
        return CommandSpec(tuple(argv), command.unset_env, notices=command.notices,
                           environment=command.environment, plugins=command.plugins,
                           agent_identity=self.agent_identity_policy(),
                           attachment=AttachmentSpec('service', 'binding', 'idle-existing-service', True, True),
                           control_endpoint=NativeEndpoint('unix-websocket', context.values['control_socket']),
                           rpc_proxy=RpcProxySpec(context.values['control_socket'],
                               context.endpoint.address if context.endpoint else '',
                               {'bootstrap': bootstrap, 'roots': roots, 'workspace_policy': workspace_policy,
                                'workspace': context.values['workspace'],
                                'name': context.values['name'],
                                'resume_permissions': permissions,
                                'plugin_config': session_config,
                                'agent_environment': stable,
                                'storage_keys': context.storage_keys,
                                'plugin_mcp_environment': additions.mcp_environment,
                                'plugin_user_config': additions.user_config,
                                'native_id': context.values.get('session_id') if context.mode == 'resume' else None},
                               binds_session=True),
                           shared_service=SharedServiceSpec(
                               (self.spec['spawn'][0], 'app-server', 'daemon', 'start'), 'socketPath', ('cliVersion', 'appServerVersion'),
                               storage,
                               required_cli_flags=('--remote',), cwd=str(Path.home().resolve()),
                               require_matching_versions=False,
                               identity=ServiceIdentity('codex', storage)))

    def launch_proxy(self, settings):
        from .launch import LaunchProtocol
        return LaunchProtocol(settings)

    def attachment_recovery(self, session):
        if (not session.attachment or session.attachment.recovery != 'idle-existing-service'
                or not session.service or session.service.identity.provider != 'codex'
                or session.endpoint != session.service.endpoint
                or session.endpoint.transport != 'unix-websocket' or not session.native_id):
            return None
        return RpcSpec(session.endpoint.address, partial(recovery_check, session), max_requests=RECOVERY_MAX_REQUESTS)

    def retained_usage(self, session, runtime):
        from .usage import last_measurement
        return last_measurement(runtime, session.native_id)

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
        if self._plain_delivery(session) and self.settings.get('inbox_bridge') is False:
            result['send_steer'] = result['send_follow_up'] = replace(result['send_steer'], mechanism='launch-only')
        if self._uses_bridge(session) and session.native_id and session.session_file:
            result['get_usage'] = Capability('supported', mechanism='codex-app-server-context')
        elif self.get_usage(session).state == 'ok':
            result['get_usage'] = Capability('supported', 'Last known context from an earlier bridge reading',
                                             mechanism='codex-app-server-context')
        return result

    def get_usage(self, session, executor=None):
        from .usage import measured
        bridge = self._uses_bridge(session)
        result = measured(session, bridge=bridge)
        if result.state != 'ok' and not bridge:
            return NativeResult('unavailable', 'Codex context reporting requires inbox_bridge; no saved reading')
        return result

    def _uses_bridge(self, session):
        if session.native_mode:
            return session.native_mode.get('mode') == 'bridge'
        # Existing launches have no selection metadata. Their endpoint remains
        # authoritative; a configured-only strict profile still declares support.
        return bool((session.endpoint and session.endpoint.transport == 'unix-websocket') or
                    (not session.launch_id and self.settings.get('inbox_bridge') is True))

    def _plain_delivery(self, session):
        if session.native_mode:
            return session.native_mode.get('mode') == 'plain'
        return not session.endpoint and self.settings.get('inbox_bridge') is False

    def get_delivery(self, session, message_id, executor):
        if not session.native_id or not session.endpoint or session.endpoint.transport != 'unix-websocket':
            return NativeResult('unavailable', 'No Codex app-server endpoint')
        return executor.rpc(RpcSpec(session.endpoint.address, partial(get_delivery, session, message_id)))

    def continue_queued(self, session, accepted_receipts, executor):
        if (not isinstance(accepted_receipts, Mapping) or any(
                not isinstance(key, str) or not key or not isinstance(value, str) or not value
                for key, value in accepted_receipts.items())
                or len(set(accepted_receipts.values())) != len(accepted_receipts)):
            return NativeResult('error', 'Accepted native queue receipts must have unique nonempty IDs')
        from .queue_events import restart_claimed
        if not restart_claimed(session):
            return NativeResult('ok', 'No newly claimed message permits queue continuation', {'continued': False})
        if (not self._uses_bridge(session) or session.state != 'running' or not session.native_id
                or not session.endpoint or session.endpoint.transport != 'unix-websocket'
                or (session.native_relay and session.native_relay.get('state') == 'lost')):
            return NativeResult('unavailable', 'No running bound Codex bridge for queue continuation')
        return executor.rpc(RpcSpec(session.endpoint.address,
            partial(continue_queued, session, dict(accepted_receipts)), max_requests=CONTINUATION_MAX_REQUESTS), timeout=5)

    def send(self, session: SessionHandle, message: Message, executor: Executor) -> NativeResult:
        if session.native_relay and session.native_relay.get('state') == 'lost':
            return NativeResult('unavailable', session.native_relay['reason'])
        mode = message.delivery
        if mode not in ('steer', 'follow-up'):
            return NativeResult('unavailable', f'Unknown delivery mode: {mode}')
        if not session.native_id or not session.endpoint or session.endpoint.transport != 'unix-websocket':
            value = ({'launch_only': True, 'final': self.settings.get('inbox_bridge') is False}
                     if self._plain_delivery(session) else None)
            return NativeResult('unavailable', 'Native delivery requires a Codex app-server launch and bound session ID', value)
        if session.state != 'running':
            return NativeResult('unavailable', 'Native launch is still binding; retry after validation completes, or inspect runtime.json for a launch error')
        return executor.rpc(RpcSpec(session.endpoint.address, partial(send_message, session, message, mode)))
