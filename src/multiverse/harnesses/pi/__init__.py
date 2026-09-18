"""Pi interactive lifecycle with an opt-in, source-owned extension."""
from pathlib import Path
import json

from ..base import ConfiguredAdapter
from ..types import SessionHandle, Message, DeliveryMode, Executor, Capability, NativeResult


class PiAdapter(ConfiguredAdapter):
    package = Path(__file__).parent
    supports_bridge = True

    def configure_native(self):
        if self.settings.get('inbox_bridge'):
            for mode in ('spawn', 'resume'):
                self.spec[mode][1:1] = ['--extension', str(self.package / 'extension.ts')]

    def send(self, session: SessionHandle, message: Message, mode: DeliveryMode, executor: Executor) -> NativeResult:
        if mode != message.delivery:
            return NativeResult('unavailable', 'The bridge honors the saved delivery mode; overrides are unsupported')
        operation = {'steer': 'steer', 'follow-up': 'follow_up'}.get(mode)
        if operation is None:
            return NativeResult('unavailable', f'Unknown delivery mode: {mode}')
        result = self.control(operation, session.context(message_id=message.id), message.prompt())
        if result.state == 'pending':
            executor.check_session()
        return result

    def validate_resume(self, properties, agent_dir):
        super().validate_resume(properties, agent_dir)
        with (agent_dir / 'session.jsonl').open() as handle:
            try:
                header = json.loads(handle.readline(65536))
                if header.get('type') != 'session':
                    raise ValueError('Missing Pi session header')
                native = self.session_id(header['id'])
                if properties.get('native_session_id') not in (None, native):
                    raise ValueError('Pi session file belongs to a different native session')
            except (ValueError, KeyError, AttributeError) as error:
                raise ValueError(f'Invalid Pi resume target: {error}') from error

    def native_capabilities(self, context):
        result = super().native_capabilities(context)
        bridge = (context.runtime.get('native_bridge') == 'pi-inbox-v1'
                  and context.runtime.get('state') == 'running')
        for name in ('steer', 'follow_up', 'wake', 'activity'):
            result[name] = (Capability('supported', mechanism='pi-inbox-v1', required=name in ('steer', 'follow_up')) if bridge else
                            Capability('unavailable', 'Pi inbox bridge has not registered for this launch',
                                       required=name in ('steer', 'follow_up')))
        return result

    def control(self, operation, context, text):
        if operation == 'activity' and self.native_capabilities(context)['activity'].state == 'supported':
            return NativeResult('ok', value=context.runtime.get('activity', 'unknown'))
        if operation in ('steer', 'follow_up', 'wake') and self.native_capabilities(context)[operation].state == 'supported':
            return NativeResult('pending', 'The Pi bridge polls the durable inbox at native safe boundaries; no delivery confirmed')
        return super().control(operation, context, text)
