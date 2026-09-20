"""Codex interactive CLI lifecycle and exact-thread native queue specifications."""
from pathlib import Path
from ..base import ConfiguredAdapter
from functools import partial
from dataclasses import replace

from ..types import SessionHandle, Message, Executor, Capability, CommandSpec, NativeEndpoint, NativeResult, RpcSpec, ServiceSpec
from .protocol import check_connection, send_message, get_delivery


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
        endpoint = None
        service = None
        if getattr(self, 'settings', {}).get('inbox_bridge'):
            endpoint = context.values['control_socket']
            service = ServiceSpec((self.spec['spawn'][0], 'app-server', '--listen', 'unix://' + endpoint), endpoint)
        if endpoint:
            argv = list(command.argv)
            if context.mode == 'resume':
                # The source-owned spawn grant is persisted by native Codex.
                # Remote resume inherits it and rejects repeating --add-dir.
                # Remove only this built-in pair; user preferences stay literal.
                position = argv.index('--add-dir')
                if argv[position + 1] != context.values['agent_dir']:
                    raise ValueError('Unexpected built-in Codex writable directory')
                del argv[position:position + 2]
            position = argv.index('--') if '--' in argv else len(argv)
            argv[position:position] = ['--remote', 'unix://' + endpoint]
            return CommandSpec(tuple(argv), command.unset_env, NativeEndpoint('unix-websocket', endpoint), service)
        return command

    def spawn(self, context):
        return super().spawn(context)

    def resume(self, context):
        return super().resume(context)

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
