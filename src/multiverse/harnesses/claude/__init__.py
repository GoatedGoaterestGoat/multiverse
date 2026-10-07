"""Claude native terminal lifecycle; steering through the session's peer inbox ("auto" by default).

In the native-backends model (docs/harness-adapters.md, "Claude: a terminal-hosted bridge") Claude has no
native service, an exact session (the assigned UUID), a terminal attachment (the `claude` process in the agent's
pane, which does the work) and, optionally, a bridge: the multiverse mod and the inbox it hosts. The bridge is part
of the Claude process and ends with it, so it carries delivery, receipts, `ask` and status without implying an
external service: Claude declares no shared service, attachment recovery, status observer or retained usage.

Like Codex, `inbox_bridge` unset is auto (the bridge unless this Claude Code cannot run it, with the reason
shown), true requires it and false launches plain. With the bridge, each launch binds Claude's peer inbox at the core-assigned
socket and loads the multiverse mod (`mod/`). `send` writes one steering
message to that inbox at `next` priority, its text tagged for the mod. The mod
shows the model the text alone: mid-turn it removes Claude's peer wrapper from
the queued message; otherwise it types the text in as a prompt. The mod
registers the bridge (`BRIDGE`) for its launch and reports the session's
status: working, waiting (a permission prompt or question) or idle.

Usage is measured from the session's own transcript, with or without the bridge.

With the bridge, a universe can opt this harness into tree compaction
(`compaction.json`; see multiverse.compaction). The launch then tells the mod
to answer the session's `/compact` with mv2's compacted conversation, and adds
a PreCompact hook that refuses Claude's own compaction should the mod ever fail
to answer. `compaction(session)` returns the protocol implementation in
`compaction/`.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from uuid import uuid4

from ..base import ConfiguredAdapter
from ..types import Capability, NativeEndpoint, NativeResult, normalize_session_id
from ...usage import Usage

MOD = Path(__file__).parent / 'mod'
# Typeahead for /multiverse, a plugin of its own: it hooks `prompt.autocomplete`, which Claude Code
# 2.1.292 added and an older Claude refuses, so it is loaded only from that version on.
TYPEAHEAD = Path(__file__).parent / 'typeahead'
TYPEAHEAD_VERSION = (2, 1, 292)
# The mod acts only on text between these tags and removes them. The opening
# tag names the delivery mode, which the mod honors, and the message ID for its
# receipt: `<<MV2 steer id=UUID>>` ... `<</MV2>>`.
CLOSE = '<</MV2>>'
MODES = {'steer': 'steer', 'follow-up': 'follow-up'}
ASK_TIMEOUT = 120
# What the mod reports through `runtime-update --bridge` once it runs in a launch.
BRIDGE = 'claude-mod-v1'
NOT_REGISTERED = 'The multiverse mod has not registered for this launch'
TOKEN = re.compile(r'^[0-9a-f]{32}$')
USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
# Claude's own compaction is refused unless a subagent asked for it (exit 2 blocks
# it). Runs with the agent's Python, like the mod's reports; a missing one blocks too.
COMPACTION_GUARD = '"${MV2_PYTHON:-python3}" -m multiverse.compaction guard || exit 2'


def claude_home(environment=None):
    """Claude's storage root: `environment` as a launch sees it, else this process's."""
    source = environment or os.environ
    if source.get('CLAUDE_CONFIG_DIR'):
        return Path(source['CLAUDE_CONFIG_DIR'])
    return (Path(source['HOME']) if source.get('HOME') else Path.home()) / '.claude'


def has_saved_conversation(native_id, environment=None):
    """Whether Claude has written a transcript for this session.

    Claude writes nothing until the first message, so a session that was never
    used has none, and `--resume` refuses it.
    """
    return any((claude_home(environment) / 'projects').glob(f'*/{native_id}.jsonl'))


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


def context_reading(native_id):
    """(tokens, timestamp) of the latest main-thread request, or None.

    Tokens are input + cache written + cache read. Subagent transcripts are
    excluded: they have their own context windows.
    """
    mains = sorted((claude_home() / 'projects').glob(f'*/{native_id}.jsonl'), key=lambda path: path.stat().st_mtime)
    if not mains:
        return None
    with _transcripts_lock:
        transcript = _transcripts.setdefault(mains[-1], Transcript(mains[-1])).read()
        responses, stamp = transcript.responses, transcript.last
    if not responses:
        return None
    latest = list(responses.values())[-1]
    return sum(counted(latest, key) for key in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')), stamp


def context_usage(native_id):
    """Tokens in the latest main-thread request, or None."""
    reading = context_reading(native_id)
    return reading[0] if reading else None


def moment(text):
    """A timestamp as a datetime, or None."""
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')) if isinstance(text, str) else None
    except ValueError:
        return None


_compactions = {}


def latest_compaction(agent_dir):
    """The newest compaction the session has installed: {'install', 'at', 'tokens'} (tokens estimated), or None.

    The estimate is the compaction's own count of the conversation it left
    (`stats.tokens_after`: prompts, summaries and raw leaves at about four
    characters a token, so no system prompt or tools, and well under the real
    count); read again only when the install log changes.
    """
    directory = Path(agent_dir) / 'compaction'
    try:
        log = directory / 'installed.jsonl'
        status = log.stat()
        key = (status.st_mtime_ns, status.st_size)
        cached = _compactions.get(log)
        if cached and cached[0] == key:
            return cached[1]
        record = None
        for line in log.read_text().splitlines():
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, dict) and re.fullmatch(r'[0-9a-f-]{36}', str(found.get('install', ''))) and found.get('at'):
                record = found
        result = None
        if record:
            # installed.jsonl's `at` is when the record was derived from the transcript, possibly much later;
            # the install file is written just before the rows are handed to Claude.
            payload = json.loads((directory / 'installs' / f"{record['install']}.json").read_text())
            tokens, created = payload.get('stats', {}).get('tokens_after'), payload.get('created_at')
            if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 and created:
                result = {'install': record['install'], 'at': created, 'tokens': tokens}
        _compactions[log] = (key, result)
        return result
    except (OSError, ValueError, AttributeError):
        return None



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


# The oldest Claude Code the mod and the peer inbox were built and tested on.
MIN_BRIDGE_VERSION = (2, 1, 280)
_versions = {}


def claude_version(executable):
    """The (major, minor, patch) of this Claude Code binary, or None when it cannot be told.

    Asked once per binary (an upgrade installs a new binary path), never fatal.
    """
    path = shutil.which(executable)
    if path is None:
        return None
    key = os.path.realpath(path)
    if key not in _versions:
        try:
            output = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        found = re.search(r'(\d+)\.(\d+)\.(\d+)', output)
        if not found:
            return None
        _versions[key] = tuple(int(part) for part in found.groups())
    return _versions[key]


def bridge_problem(executable):
    """Why this Claude Code cannot run the bridge, or None. A version that cannot be read is not a reason."""
    version = claude_version(executable)
    if version is not None and version < MIN_BRIDGE_VERSION:
        return (f'Claude Code {".".join(map(str, version))} is older than '
                f'{".".join(map(str, MIN_BRIDGE_VERSION))}, which the multiverse mod needs')
    return None


FRESH_SESSION = 'No saved Claude conversation for this session; started it fresh under the same session ID'


@dataclass(frozen=True)
class Bridge:
    """What one launch's terminal-hosted integration, the multiverse mod, can do now.

    `launched`: this launch loaded the mod (not plain, not an auto fallback). `registered`: the mod
    reported itself for this launch, so its inbox exists. `ready`: steering, `ask` and receipts can be used
    now. `reason` says why not. None of this is a service or a reconnectable terminal.
    """
    launched: bool
    registered: bool
    ready: bool
    reason: str | None


def bridge_of(enabled, session):
    """The `Bridge` of a launch: the harness setting says whether the mod is wanted, the launch's own record
    whether it ran (an auto launch may have fallen back to plain), and the mod's report whether it is up."""
    launch = session.native_mode or {}
    plain = launch.get('mode') == 'plain'
    launched = bool(enabled) and not plain
    registered = bool(launched and session.native_id and session.endpoint
                      and session.endpoint.transport == 'unix-lines' and session.bridge == BRIDGE)
    ready = registered and session.state == 'running'
    if ready:
        reason = None
    elif plain and launch.get('fallback_reason'):
        reason = f'Launched without the bridge: {launch["fallback_reason"]}'
    else:
        reason = NOT_REGISTERED
    return Bridge(launched, registered, ready, reason)


class ClaudeAdapter(ConfiguredAdapter):
    resume_prompt_args = ('--', '{task}')
    package = Path(__file__).parent
    supports_bridge = True
    bridge_policy = 'auto'

    @classmethod
    def configured(cls, name, settings):
        # Like Codex: unset is auto, true requires the bridge, false launches plain. `settings['inbox_bridge']`
        # holds the effective on/off (auto is on) for everything that reads it; the policy is kept apart.
        raw = settings.get('inbox_bridge')
        adapter = super().configured(name, {**settings, 'inbox_bridge': True if raw is None else raw})
        adapter.bridge_policy = 'auto' if raw is None else 'required' if raw is True else 'plain'
        return adapter

    def spawn(self, context):
        return self._with_bridge(super().spawn(context), context)

    def resume(self, session, context):
        command = super().resume(session, context)
        fresh = None
        if session.native_id and not has_saved_conversation(session.native_id, context.environment):
            fresh = self._reserve_session(command, session.native_id)
        command = self._with_bridge(fresh or command, context)
        if fresh is None:
            return command
        # Say so in the agent's details: it now runs without any earlier context.
        mode = dict(command.native_mode)
        mode['fallback_reason'] = '; '.join(filter(None, [mode.get('fallback_reason'), FRESH_SESSION]))
        return replace(command, native_mode=mode)

    @staticmethod
    def _reserve_session(command, native_id):
        """The command starting this exact session fresh, or None if it does not resume it.

        Claude never reserved the ID, so `--session-id` takes it over with every
        other flag, and any wake prompt after `--`, unchanged. If a transcript does
        exist, Claude refuses the ID as already in use, so this cannot overwrite one.
        """
        argv = list(command.argv)
        options = argv.index('--') if '--' in argv else len(argv)
        for index in range(options - 1):
            if argv[index] == '--resume' and argv[index + 1] == native_id:
                argv[index] = '--session-id'
                return replace(command, argv=tuple(argv))
        return None

    def _with_bridge(self, command, context):
        # auto uses the bridge unless this Claude cannot run it (and says why), required raises instead of
        # falling back, plain never loads the mod.
        policy = self.bridge_policy
        plain = replace(command, native_mode={'policy': policy, 'mode': 'plain', 'fallback_reason': None})
        if policy == 'plain':
            return plain
        problem = bridge_problem(command.argv[0])
        if problem:
            if policy == 'required':
                raise ValueError(problem)
            return replace(plain, native_mode={**plain.native_mode, 'fallback_reason': problem})
        socket = context.values['control_socket']
        # mv2 sends from outside the session, which Claude otherwise holds for
        # approval when the session skips permission prompts.
        settings = {'crossSessionInbound': 'accept'}
        environment = {**command.environment, 'CLAUDE_CODE_ENABLE_FUNCTION_HOOKS': '1'}
        native_mode = {'policy': policy, 'mode': 'bridge', 'fallback_reason': None}
        root = self._universe(context.values.get('agent_dir'))
        compaction, problem = self.compaction_state(root) if root else (None, None)
        if compaction is not None and compaction.enabled:
            settings['hooks'] = {'PreCompact': [{'hooks': [{'type': 'command', 'command': COMPACTION_GUARD}]}]}
            environment['MV2_COMPACTION'] = '1'
        elif problem:
            # An unusable compaction.json never stops a launch: this one runs without compaction.
            native_mode = {'policy': policy, 'mode': 'bridge without compaction',
                           'fallback_reason': f'Launched without compaction: {problem}'}
        version = claude_version(command.argv[0])
        typeahead = ('--plugin-dir', str(TYPEAHEAD)) if version is not None and version >= TYPEAHEAD_VERSION else ()
        return replace(command,
            argv=(command.argv[0], '--plugin-dir', str(MOD), '--messaging-socket-path', socket,
                  '--settings', json.dumps(settings), *typeahead, *command.argv[1:]),
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

    def _bridge(self, session):
        return bridge_of(getattr(self, 'settings', {}).get('inbox_bridge'), session)

    def attachment_ready(self, session):
        """A launch with no inbox is ready once its process runs; a bridged one when the mod has registered.

        Core asks this on every readiness poll, so it stays cheap and independent of the other capabilities.
        """
        return not session.endpoint or self._bridge(session).ready

    def capabilities(self, session, executor=None):
        result = super().capabilities(session, executor)
        result['session_name'] = Capability('supported', mechanism='--name')
        # The launch's own record decides whether it used the bridge: an auto launch may have fallen back to plain.
        bridge = self._bridge(session)
        for name in ('send_steer', 'send_follow_up'):
            result[name] = (Capability('supported', mechanism='claude-peer-inbox', required=True) if bridge.ready
                            else Capability('unavailable', bridge.reason, required=True))
        if bridge.launched and session.session_file:
            result['get_delivery'] = Capability('supported', mechanism='claude-mod-receipt')
        if bridge.ready and session.session_file:
            result['ask'] = Capability('supported', mechanism='claude-model-fork')
        if session.native_id:
            result['get_usage'] = Capability('supported', mechanism='claude-transcript')
        result['compaction'] = self._compaction_capability(session)
        return result

    def _compaction_capability(self, session):
        if not getattr(self, 'settings', {}).get('inbox_bridge'):
            return Capability('unavailable', 'Needs the multiverse mod: remove "inbox_bridge": false from harness.json')
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
        reading = context_reading(session.native_id)
        context = dict(reported['context']) if isinstance(reported.get('context'), dict) else {}
        responded = moment(reading[1]) if reading else None
        reported_at = moment(reported.get('context_reported_at'))
        try:
            compaction = latest_compaction(Path(session.session_file).parent)
        except TypeError:
            compaction = None
        installed_at = moment(compaction['at']) if compaction else None
        if installed_at and not (responded and responded > installed_at):
            # No response has come since our compaction: its count of the conversation stands in until one does.
            context = {key: value for key, value in context.items() if key not in ('tokens', 'percent')}
            context.update(tokens=compaction['tokens'], estimated=True, estimated_at=compaction['at'],
                           estimated_from=compaction['install'])
        elif reading and (context.get('tokens') is None or (responded and (reported_at is None or reported_at < responded))):
            # No report from the mod yet, or a response newer than it: the transcript has the real request size.
            context = {key: value for key, value in context.items() if key not in ('tokens', 'percent')}
            context['tokens'] = reading[0]
        if context:
            reported['context'] = context
        if usage is None:
            if reported:
                return NativeResult('ok', 'Reported by the multiverse mod', {'native_session_id': session.native_id, **reported})
            return NativeResult('unavailable', 'Claude has not written a transcript for this session yet')
        return NativeResult('ok', 'Measured from the Claude session transcript', {**asdict(usage), **reported})

    def send(self, session, message, executor):
        mode = MODES.get(message.delivery)
        if mode is None:
            return NativeResult('unavailable', f'Unknown delivery mode: {message.delivery}')
        bridge = self._bridge(session)
        if not bridge.ready:
            return NativeResult('unavailable', bridge.reason)
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
        bridge = self._bridge(session)
        if not bridge.ready:
            return NativeResult('unavailable', bridge.reason)
        if not session.session_file:
            return NativeResult('unavailable', 'No agent directory recorded')
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
