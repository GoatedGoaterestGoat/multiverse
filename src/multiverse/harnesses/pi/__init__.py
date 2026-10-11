"""Pi interactive lifecycle with an opt-in, source-owned extension."""
from pathlib import Path
import json
from dataclasses import replace

from ...plugins import harness
from ..base import ConfiguredAdapter
from ..types import SessionHandle, Message, Executor, Capability, NativeResult, NativeEndpoint
from ..types import normalize_session_id


class PiAdapter(ConfiguredAdapter):
    package = Path(__file__).parent
    supports_bridge = True
    family = 'pi'
    resume_prompt_args = ('--', '{task}')

    def spawn(self, context):
        return self._with_extension(super().spawn(context), context)

    def resume(self, session, context):
        command = super().resume(session, context)
        with Path(session.session_file).open() as handle:
            try:
                header = json.loads(handle.readline(65536))
                if header.get('type') != 'session':
                    raise ValueError('Missing Pi session header')
                native = normalize_session_id(header['id'])
                if session.native_id is not None and normalize_session_id(session.native_id) != native:
                    raise ValueError('Pi session file belongs to a different native session')
            except (ValueError, KeyError, AttributeError) as error:
                raise ValueError(f'Invalid Pi resume target: {error}') from error
        return self._with_extension(command, context)

    def _with_extension(self, command, context):
        bridge = bool(self.settings.get('inbox_bridge'))
        # Extensions of plugins on in the agent's universe, plain or bridged: each entry says if it needs the bridge.
        # Pi is asked its version only when a plugin here has something for Pi.
        plan = harness.plan_for(context, 'pi', self.name, version=lambda: harness.version_of(command.argv[0]),
                                bridge=bridge, base_environment=command.environment)
        extensions = tuple(argument for path in (plan.pi_extensions if plan else ())
                           for argument in ('--extension', path))
        if plan is not None:
            command = replace(command, environment=plan.environment, plugins=plan.record())
        if not bridge:
            return replace(command, argv=(command.argv[0], *extensions, *command.argv[1:])) if extensions else command
        return replace(command,
            argv=(command.argv[0], '--extension', str(self.package / 'extension.ts'), *extensions, *command.argv[1:]),
            control_endpoint=NativeEndpoint('unix-json', context.values['control_socket']))

    def capabilities(self, session: SessionHandle, executor: Executor | None = None) -> dict[str, Capability]:
        result = super().capabilities(session, executor)
        result['session_name'] = Capability('supported', mechanism='--name')
        connected = (session.state == 'running' and session.bridge == 'pi-socket-v1'
                     and session.native_id and session.endpoint and session.endpoint.transport == 'unix-json')
        support = Capability('supported', mechanism='pi-socket-v1', required=True) if connected else Capability(
            'unavailable', 'Pi extension socket has not registered for this launch', required=True)
        result['send_steer'] = result['send_follow_up'] = support
        result['get_delivery'] = replace(support, required=False)
        return result

    def get_delivery(self, session, message_id, executor):
        return executor.request(session, {'operation': 'get_delivery', 'message_id': message_id})

    def send(self, session: SessionHandle, message: Message, executor: Executor) -> NativeResult:
        mode = message.delivery
        if mode not in ('steer', 'follow-up'):
            return NativeResult('unavailable', f'Unknown delivery mode: {mode}')
        if self.capabilities(session)['send_steer'].state != 'supported':
            return NativeResult('unavailable', 'Pi extension socket is not registered')
        return executor.request(session, {'operation': 'send', 'message_id': message.id,
                                         'text': message.framed or message.text, 'mode': mode})
