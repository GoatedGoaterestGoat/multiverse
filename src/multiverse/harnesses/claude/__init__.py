"""Claude native terminal lifecycle; opt-in steering through the session's peer inbox.

With `inbox_bridge`, each launch binds Claude's peer inbox at the core-assigned
socket and loads the multiverse mod (`mod/`). `send` writes one steering
message to that inbox at `next` priority, its text tagged for the mod. The mod
shows the model the text alone: mid-turn it removes Claude's peer wrapper from
the queued message; otherwise it types the text in as a prompt. The mod
registers the `claude-mod-v1` bridge for its launch and reports the session's
activity: working, waiting (a permission prompt or question) or idle.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re

from ..base import ConfiguredAdapter
from ..types import Capability, NativeEndpoint, NativeResult

MOD = Path(__file__).parent / 'mod'
# The mod acts only on text between these tags and removes them.
OPEN, CLOSE = '<<MV2>>', '<</MV2>>'
TOKEN = re.compile(r'^[0-9a-f]{32}$')


def peer_token(socket_path):
    """The inbox key Claude publishes for this socket, or None before it binds.

    Claude writes `<pid>.<sha256 of the socket path>.key` under its sessions
    directory; the socket path is unique to one launch.
    """
    digest = hashlib.sha256(os.path.abspath(socket_path).encode()).hexdigest()
    sessions = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'sessions'
    keys = list(sessions.glob(f'*.{digest}.key'))
    if len(keys) != 1:
        return None
    token = json.loads(keys[0].read_text()).get('peerToken')
    return token if isinstance(token, str) and TOKEN.match(token) else None


class ClaudeAdapter(ConfiguredAdapter):
    package = Path(__file__).parent
    supports_bridge = True

    def spawn(self, context):
        return self._with_bridge(super().spawn(context), context)

    def resume(self, session, context):
        return self._with_bridge(super().resume(session, context), context)

    def _with_bridge(self, command, context):
        if not self.settings.get('inbox_bridge'):
            return command
        socket = context.values['control_socket']
        # mv2 sends from outside the session, which Claude otherwise holds for
        # approval when the session skips permission prompts.
        settings = json.dumps({'crossSessionInbound': 'accept'})
        return replace(command,
            argv=(command.argv[0], '--plugin-dir', str(MOD), '--messaging-socket-path', socket,
                  '--settings', settings, *command.argv[1:]),
            control_endpoint=NativeEndpoint('unix-lines', socket),
            environment={**command.environment, 'CLAUDE_CODE_ENABLE_FUNCTION_HOOKS': '1'})

    def capabilities(self, session, executor=None):
        result = super().capabilities(session, executor)
        connected = (getattr(self, 'settings', {}).get('inbox_bridge') and session.state == 'running'
                     and session.native_id and session.endpoint and session.endpoint.transport == 'unix-lines'
                     and session.bridge == 'claude-mod-v1')
        result['send_steer'] = (Capability('supported', mechanism='claude-peer-inbox', required=True) if connected
                                else Capability('unavailable', 'The multiverse mod has not registered for this launch',
                                                required=True))
        result['send_follow_up'] = Capability('unavailable', 'Claude follow-up delivery is not implemented yet',
                                              required=True)
        return result

    def send(self, session, message, executor):
        if message.delivery != 'steer':
            return NativeResult('unavailable', 'Claude follow-up delivery is not implemented yet')
        if self.capabilities(session)['send_steer'].state != 'supported':
            return NativeResult('unavailable', 'The multiverse mod has not registered for this launch')
        token = peer_token(session.endpoint.address)
        if token is None:
            return NativeResult('unavailable', 'Claude has not published the inbox key for this launch')
        frame = {'type': 'user', 'priority': 'next', 'session_id': session.native_id, 'msg_id': message.id,
                 'message': {'role': 'user', 'content': OPEN + (message.framed or message.text) + CLOSE}}
        written = executor.write_lines(session, [json.dumps({'type': 'auth', 'token': token}),
                                                 json.dumps(frame, ensure_ascii=False)])
        if written.state != 'ok':
            return written
        return NativeResult('native_queued', 'Written to the Claude session inbox; Claude sends no receipt')
