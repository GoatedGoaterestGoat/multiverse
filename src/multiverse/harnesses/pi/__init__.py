"""Pi interactive lifecycle with an opt-in, source-owned extension."""
import json

from ..legacy import LegacyAdapter
from ..types import Capability, NativeResult


class PiAdapter(LegacyAdapter):
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

    def capabilities(self, context):
        result = super().capabilities(context)
        bridge = (context.runtime.get('native_bridge') == 'pi-inbox-v1'
                  and context.runtime.get('state') == 'running')
        for name in ('steer', 'follow_up', 'wake', 'activity'):
            result[name] = (Capability('supported', mechanism='pi-inbox-v1', required=name in ('steer', 'follow_up')) if bridge else
                            Capability('unavailable', 'Pi inbox bridge has not registered for this launch',
                                       required=name in ('steer', 'follow_up')))
        return result

    def control(self, operation, context, text):
        if operation == 'activity' and self.capabilities(context)['activity'].state == 'supported':
            return NativeResult('ok', value=context.runtime.get('activity', 'unknown'))
        if operation in ('steer', 'follow_up', 'wake') and self.capabilities(context)[operation].state == 'supported':
            return NativeResult('pending', 'The Pi bridge polls the durable inbox at native safe boundaries; no delivery confirmed')
        return super().control(operation, context, text)
