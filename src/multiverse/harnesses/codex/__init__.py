"""Codex interactive CLI lifecycle and exact-thread native queue specifications."""
from ..legacy import LegacyAdapter
from functools import partial

from ..types import Capability, CommandSpec, NativeEndpoint, NativeResult, RpcSpec, ServiceSpec
from .control import plan


class CodexAdapter(LegacyAdapter):
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

    def endpoint(self, context=None):
        settings = getattr(self, 'settings', {})
        if settings.get('inbox_bridge') and context is not None:
            endpoint = context.runtime.get('control_endpoint') or {}
            if endpoint.get('transport') == 'unix-websocket':
                return endpoint.get('address')
        return None

    def launch(self, context):
        command = super().launch(context)
        endpoint = self.endpoint()
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

    def rpc(self, context, operation=None, text=''):
        return RpcSpec(self.endpoint(context),
                       partial(plan, context, operation, text))

    def probes(self, context=None):
        if self.endpoint(context) or getattr(self, 'settings', {}).get('inbox_bridge'):
            if (context and context.properties.get('native_session_id') and context.runtime.get('state') == 'running'
                    and context.runtime.get('control_endpoint') == {'transport': 'unix-websocket', 'address': self.endpoint(context)}):
                return {'native_control': self.rpc(context)}
            return {}
        return {'native_queue': CommandSpec((self.spec['spawn'][0], 'queue', '--help'))}

    def capabilities(self, context):
        result = super().capabilities(context)
        if self.endpoint(context) or getattr(self, 'settings', {}).get('inbox_bridge') or context.runtime.get('control_endpoint'):
            observed = context.probes.get('native_control', Capability('unavailable', 'Exact native endpoint has not been probed'))
            if context.runtime.get('control_endpoint') != {'transport': 'unix-websocket', 'address': self.endpoint(context)}:
                observed = Capability('unavailable', 'Native endpoint does not match the recorded launch')
            for name in ('steer', 'follow_up', 'native_queue'):
                result[name] = Capability(observed.state, observed.reason, observed.mechanism,
                                          required=name in ('steer', 'follow_up'))
            return result
        result['native_queue'] = context.probes.get('native_queue', Capability('unavailable', 'Native queue has not been probed'))
        if not context.properties.get('native_session_id'):
            result['native_queue'] = Capability('unavailable', 'No exact native thread ID recorded')
        if any(getattr(self, 'settings', {}).get(key) for key in ('args', 'spawn_args', 'resume_args')):
            result['native_queue'] = Capability('unavailable', 'Queue routing for custom launch arguments is not implemented')
        # Queue acceptance alone does not establish wake, delivery, reading or completion.
        result['wake'] = Capability('unavailable', 'Native queue does not confirm wake or execution')
        return result

    def control(self, operation, context, text):
        capability = self.capabilities(context).get(operation)
        if self.endpoint(context) and operation in ('steer', 'follow_up', 'native_queue') and capability.state == 'supported':
            self.session_id(context.properties['native_session_id'])
            return self.rpc(context, operation, text)
        if operation == 'native_queue' and capability.state == 'supported':
            native = self.session_id(context.properties['native_session_id'])
            return CommandSpec((self.spec['spawn'][0], 'queue', '--thread', native, '--message', text,
                                '--cd', context.properties['workspace']))
        return NativeResult('error' if capability and capability.state == 'error' else 'unavailable',
                            capability.reason if capability else f'Unknown capability: {operation}')
