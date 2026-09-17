"""Codex interactive CLI lifecycle and exact-thread native queue specifications."""
from ..legacy import LegacyAdapter
from ..types import Capability, CommandSpec, NativeResult


class CodexAdapter(LegacyAdapter):
    def probes(self):
        return {'native_queue': CommandSpec((self.spec['spawn'][0], 'queue', '--help'))}

    def capabilities(self, context):
        result = super().capabilities(context)
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
        if operation == 'native_queue' and capability.state == 'supported':
            native = self.session_id(context.properties['native_session_id'])
            return CommandSpec((self.spec['spawn'][0], 'queue', '--thread', native, '--message', text,
                                '--cd', context.properties['workspace']))
        return NativeResult('error' if capability and capability.state == 'error' else 'unavailable',
                            capability.reason if capability else f'Unknown capability: {operation}')
