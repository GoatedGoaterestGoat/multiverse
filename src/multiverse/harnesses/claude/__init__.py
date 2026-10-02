"""Claude native terminal lifecycle; opt-in steering through the session's peer inbox.

With `inbox_bridge`, each launch binds Claude's peer inbox at the core-assigned
socket and loads the multiverse mod (`mod/`). `send` writes one steering
message to that inbox at `next` priority, its text tagged for the mod. The mod
shows the model the text alone: mid-turn it removes Claude's peer wrapper from
the queued message; otherwise it types the text in as a prompt. The mod
registers the `claude-mod-v1` bridge for its launch and reports the session's
status: working, waiting (a permission prompt or question) or idle.

Usage is measured from the session's own transcript, with or without the bridge.

With the bridge, a universe can opt this harness into tree compaction
(`compaction.json`; see multiverse.compaction). The launch then tells the mod
to answer the session's `/compact` with mv2's compacted conversation, and adds
a PreCompact hook that refuses Claude's own compaction should the mod ever fail
to answer. `compaction(session)` returns the protocol implementation in
`compaction/`.
"""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from ..base import ConfiguredAdapter
from ..types import Capability, NativeEndpoint, NativeResult, normalize_session_id
from ...usage import Usage

MOD = Path(__file__).parent / 'mod'
# The mod acts only on text between these tags and removes them. The opening
# tag names the delivery mode, which the mod honors, and the message ID for its
# receipt: `<<MV2 steer id=UUID>>` ... `<</MV2>>`.
CLOSE = '<</MV2>>'
MODES = {'steer': 'steer', 'follow-up': 'follow-up'}
ASK_TIMEOUT = 120
TOKEN = re.compile(r'^[0-9a-f]{32}$')
USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
# Claude's own compaction is refused unless a subagent asked for it (exit 2 blocks
# it). Runs with the agent's Python, like the mod's reports; a missing one blocks too.
COMPACTION_GUARD = '"${MV2_PYTHON:-python3}" -m multiverse.compaction guard || exit 2'


def claude_home():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


def peer_token(socket_path):
    """The inbox key Claude publishes for this socket, or None before it binds.

    Claude writes `<pid>.<sha256 of the socket path>.key` under its sessions
    directory; the socket path is unique to one launch.
    """
    digest = hashlib.sha256(os.path.abspath(socket_path).encode()).hexdigest()
    keys = list((claude_home() / 'sessions').glob(f'*.{digest}.key'))
    if len(keys) != 1:
        return None
    token = json.loads(keys[0].read_text()).get('peerToken')
    return token if isinstance(token, str) and TOKEN.match(token) else None


class Transcript:
    """Usage recorded in one transcript file, read once and then as it grows.

    Claude writes one record per content block of a response, each with the
    response's usage, so usage is counted once per response ID. A compaction
    that keeps a row stores it again with zero usage; that copy never replaces
    the response's real usage.
    """

    def __init__(self, path):
        self.path, self.inode, self.offset, self.responses, self.last = path, None, 0, {}, None

    def read(self):
        status = self.path.stat()
        if status.st_ino != self.inode or status.st_size < self.offset:
            self.inode, self.offset, self.responses, self.last = status.st_ino, 0, {}, None
        with self.path.open('rb') as handle:
            handle.seek(self.offset)
            data = handle.read()
        end = data.rfind(b'\n') + 1  # an unfinished last line waits for its newline
        for line in data[:end].splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get('message') if isinstance(record, dict) and record.get('type') == 'assistant' else None
            if isinstance(message, dict) and isinstance(message.get('usage'), dict) and message.get('id'):
                if message['id'] in self.responses and not any(counted(message['usage'], key) for key in USAGE_KEYS):
                    continue
                self.responses[message['id']] = message['usage']
                self.last = record.get('timestamp') or self.last
        self.offset += end
        return self


_transcripts = {}
_transcripts_lock = threading.Lock()


def counted(usage, key):
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def context_usage(native_id):
    """Tokens in the latest main-thread request (input + cache written + cache read), or None.

    Subagent transcripts are excluded: they have their own context windows.
    """
    mains = sorted((claude_home() / 'projects').glob(f'*/{native_id}.jsonl'), key=lambda path: path.stat().st_mtime)
    if not mains:
        return None
    with _transcripts_lock:
        responses = _transcripts.setdefault(mains[-1], Transcript(mains[-1])).read().responses
    if not responses:
        return None
    latest = list(responses.values())[-1]
    return sum(counted(latest, key) for key in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))


def measured_usage(native_id):
    """Cumulative usage of the session and its subagents, from their transcripts."""
    projects = claude_home() / 'projects'
    mains = sorted(projects.glob(f'*/{native_id}.jsonl'), key=lambda path: path.stat().st_mtime)
    if not mains:
        return None
    paths = [mains[-1], *sorted((mains[-1].parent / native_id / 'subagents').glob('**/*.jsonl'))]
    with _transcripts_lock:
        transcripts = [_transcripts.setdefault(path, Transcript(path)).read() for path in paths]
    responses = [usage for transcript in transcripts for usage in transcript.responses.values()]
    cached = sum(counted(usage, 'cache_read_input_tokens') for usage in responses)
    written = sum(counted(usage, 'cache_creation_input_tokens') for usage in responses)
    inputs = sum(counted(usage, 'input_tokens') for usage in responses) + cached + written
    outputs = sum(counted(usage, 'output_tokens') for usage in responses)
    last = max((transcript.last for transcript in transcripts if transcript.last), default='')
    return Usage(native_id, inputs, outputs, inputs + outputs, 'claude-transcript', last, cached, written)


class ClaudeAdapter(ConfiguredAdapter):
    resume_prompt_args = ('--', '{task}')
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
        settings = {'crossSessionInbound': 'accept'}
        environment = {**command.environment, 'CLAUDE_CODE_ENABLE_FUNCTION_HOOKS': '1'}
        native_mode = command.native_mode
        root = self._universe(context.values.get('agent_dir'))
        compaction, problem = self.compaction_state(root) if root else (None, None)
        if compaction is not None and compaction.enabled:
            settings['hooks'] = {'PreCompact': [{'hooks': [{'type': 'command', 'command': COMPACTION_GUARD}]}]}
            environment['MV2_COMPACTION'] = '1'
        elif problem:
            # An unusable compaction.json never stops a launch: this one runs without compaction.
            native_mode = {'policy': 'inbox_bridge', 'mode': 'bridge without compaction',
                           'fallback_reason': f'Launched without compaction: {problem}'}
        return replace(command,
            argv=(command.argv[0], '--plugin-dir', str(MOD), '--messaging-socket-path', socket,
                  '--settings', json.dumps(settings), *command.argv[1:]),
            control_endpoint=NativeEndpoint('unix-lines', socket),
            environment=environment, native_mode=native_mode)

    @staticmethod
    def _universe(agent_dir):
        """The universe an agent directory belongs to, or None outside one."""
        if not agent_dir:
            return None
        directory = Path(agent_dir)
        root = directory.parent.parent.parent
        if directory.parent.name != 'agents' or not (root / 'universe.json').is_file():
            return None
        return root

    @staticmethod
    def compaction_defaults():
        """Claude's builder settings where compaction.json gives none."""
        from .compaction import BUILDER_DEFAULTS
        return dict(BUILDER_DEFAULTS)

    def compaction_state(self, root, loaded=None):
        """(settings, None) for this harness in the universe at `root`, or (None, why its compaction.json cannot be used).

        `loaded` checks a parsed file (config.Config) before it is written
        instead of the one on disk. Launches, capabilities and `mv2 doctor` use
        it; `mv2 compaction` refuses loudly instead.
        """
        from ...compaction import config
        from .compaction import BUILDER_DEFAULTS, check_builder
        try:
            settings = (loaded or config.load(root)).for_harness(self.name, BUILDER_DEFAULTS)
        except ValueError as error:
            return None, str(error)
        try:
            check_builder(settings.builder)
        except ValueError as error:
            return None, f'{Path(root) / config.FILE}: harnesses.{self.name}.builder: {error}'
        return settings, None

    def compaction(self, session):
        """The compaction protocol for this exact session (multiverse.compaction.protocol)."""
        from .compaction import ClaudeCompaction
        return ClaudeCompaction(self, session)

    def capabilities(self, session, executor=None):
        result = super().capabilities(session, executor)
        result['session_name'] = Capability('supported', mechanism='--name')
        connected = (getattr(self, 'settings', {}).get('inbox_bridge') and session.state == 'running'
                     and session.native_id and session.endpoint and session.endpoint.transport == 'unix-lines'
                     and session.bridge == 'claude-mod-v1')
        for name in ('send_steer', 'send_follow_up'):
            result[name] = (Capability('supported', mechanism='claude-peer-inbox', required=True) if connected
                            else Capability('unavailable', 'The multiverse mod has not registered for this launch',
                                            required=True))
        if getattr(self, 'settings', {}).get('inbox_bridge') and session.session_file:
            result['get_delivery'] = Capability('supported', mechanism='claude-mod-receipt')
        if connected and session.session_file:
            result['ask'] = Capability('supported', mechanism='claude-model-fork')
        if session.native_id:
            result['get_usage'] = Capability('supported', mechanism='claude-transcript')
        result['compaction'] = self._compaction_capability(session)
        return result

    def _compaction_capability(self, session):
        if not getattr(self, 'settings', {}).get('inbox_bridge'):
            return Capability('unavailable', 'Needs the multiverse mod: set "inbox_bridge": true in harness.json')
        root = self._universe(Path(session.session_file).parent if session.session_file else None)
        if root is None:
            return Capability('unavailable', 'Not an agent of a universe')
        settings, problem = self.compaction_state(root)
        if problem:
            return Capability('error', f'{problem}; launches run without compaction')
        if not settings.enabled:
            return Capability('unavailable', f'Off for {self.name} in compaction.json')
        fallback = (session.native_mode or {}).get('fallback_reason')
        if session.state == 'running' and fallback:
            return Capability('error', f'{fallback}; restart the agent to use compaction')
        return Capability('supported', mechanism='claude-mod-session-compact')

    def get_usage(self, session, executor=None):
        """Transcript totals, plus the mod's context/cost/rate limits while they belong to this launch."""
        from ... import agents
        if not session.native_id:
            return NativeResult('unavailable', 'No Claude session ID recorded')
        reported = {}
        try:
            facts = agents.runtime(Path(session.session_file).parent)
            mod = facts.get('usage')
            if isinstance(mod, dict) and session.launch_id and facts.get('launch_id') == session.launch_id:
                reported = {key: mod[key] for key in ('context', 'cost_usd', 'rate_limits') if key in mod}
                reported['context_reported_at'] = mod.get('reported_at')
        except (OSError, ValueError, TypeError):
            pass
        usage = measured_usage(session.native_id)
        # Before the mod reports (or without it) the request size comes from the transcript; the window is unknown.
        if not isinstance(reported.get('context'), dict) or reported['context'].get('tokens') is None:
            tokens = context_usage(session.native_id)
            if tokens is not None:
                reported['context'] = {**(reported.get('context') or {}), 'tokens': tokens}
        if usage is None:
            if reported:
                return NativeResult('ok', 'Reported by the multiverse mod', {'native_session_id': session.native_id, **reported})
            return NativeResult('unavailable', 'Claude has not written a transcript for this session yet')
        return NativeResult('ok', 'Measured from the Claude session transcript', {**asdict(usage), **reported})

    def send(self, session, message, executor):
        mode = MODES.get(message.delivery)
        if mode is None:
            return NativeResult('unavailable', f'Unknown delivery mode: {message.delivery}')
        if self.capabilities(session)['send_steer'].state != 'supported':
            return NativeResult('unavailable', 'The multiverse mod has not registered for this launch')
        token = peer_token(session.endpoint.address)
        if token is None:
            return NativeResult('unavailable', 'Claude has not published the inbox key for this launch')
        # Always `next`: the mod decides from the tag when a follow-up runs.
        try:
            opening = f'<<MV2 {mode} id={normalize_session_id(message.id)}>>'
        except ValueError:
            opening = f'<<MV2 {mode}>>'  # no receipt without a UUID
        frame = {'type': 'user', 'priority': 'next', 'session_id': session.native_id, 'msg_id': message.id,
                 'message': {'role': 'user', 'content': opening + (message.framed or message.text) + CLOSE}}
        written = executor.write_lines(session, [json.dumps({'type': 'auth', 'token': token}),
                                                 json.dumps(frame, ensure_ascii=False)])
        if written.state != 'ok':
            return written
        return NativeResult('native_queued', 'Written to the Claude session inbox; the mod reports delivery')

    def ask(self, session, question, executor):
        """Answer a side question with $.model.fork over the session's transcript.

        The question travels tagged `ask` over the peer inbox; the mod consumes it,
        so nothing enters the conversation, and writes claude-asks/<id>.json.
        """
        if self.capabilities(session)['ask'].state != 'supported':
            return NativeResult('unavailable', 'The multiverse mod has not registered for this launch')
        token = peer_token(session.endpoint.address)
        if token is None:
            return NativeResult('unavailable', 'Claude has not published the inbox key for this launch')
        question_id = str(uuid4())
        answer = Path(session.session_file).parent / 'claude-asks' / f'{question_id}.json'
        frame = {'type': 'user', 'priority': 'next', 'session_id': session.native_id, 'msg_id': question_id,
                 'message': {'role': 'user', 'content': f'<<MV2 ask id={question_id}>>{question}{CLOSE}'}}
        written = executor.write_lines(session, [json.dumps({'type': 'auth', 'token': token}),
                                                 json.dumps(frame, ensure_ascii=False)])
        if written.state != 'ok':
            return written
        deadline = time.monotonic() + ASK_TIMEOUT
        while time.monotonic() < deadline:
            try:
                record = json.loads(answer.read_text())
            except (OSError, ValueError):  # not written yet, or mid-write
                time.sleep(0.2)
                continue
            answer.unlink(missing_ok=True)
            if record.get('answered'):
                return NativeResult('ok', 'Answered over the session transcript; nothing was added to it',
                                    {'answer': record.get('text', ''), 'usage': record.get('usage')})
            if record.get('reason') == 'nothing-to-fork':
                return NativeResult('unavailable', 'The session has no response to answer from yet')
            return NativeResult('error', f'Claude did not answer: {record.get("reason", "unknown reason")}')
        return NativeResult('unknown', f'No answer within {ASK_TIMEOUT} s; the question may still be answered')

    def get_delivery(self, session, message_id, executor=None):
        """The mod's receipt, written when the message's row entered the conversation."""
        if not session.session_file:
            return NativeResult('unavailable', 'No agent directory recorded')
        try:
            receipt = Path(session.session_file).parent / 'claude-receipts' / f'{normalize_session_id(message_id)}.json'
        except ValueError:
            return NativeResult('error', 'Message ID must be a UUID')
        if not receipt.is_file():
            return NativeResult('unknown', 'No delivery receipt from the multiverse mod yet')
        value = json.loads(receipt.read_text())
        return NativeResult('native_queued', 'Delivered into the Claude conversation',
                            {'transcript_confirmed': True, 'route': value.get('route'), 'delivered_at_ms': value.get('at')})
