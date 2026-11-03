"""Private, tool-free Codex turns. This module builds plans and speaks JSON-RPC.

Core owns the process, files and cleanup. Credentials are read only by Codex,
through links into the selected file-login home. The checked catalogue/settings
are the tools boundary; production does not proxy or log model requests.
"""
from functools import lru_cache
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tomllib
from uuid import uuid4

from ..types import CommandSpec, RunFailure, RunFile, RunLink, RunOptions, RunPlan, RunReply
from .plugins import codex_version, server_version
from .storage import session_home

FORMAT = 'codex-jsonl'
VERSIONS = {(0, 166, 0)}
SAFE_ENV = frozenset(('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'SSL_CERT_FILE', 'SSL_CERT_DIR'))
# Source audit: spec_plan.rs, app-server/extensions.rs and native model metadata.
# stable_environment_tools was removed; it deliberately is not the tools switch.
DISABLED = ('apps', 'plugins', 'hooks', 'memories', 'goals', 'image_generation', 'shell_snapshot',
            'shell_tool', 'view_image', 'multi_agent', 'code_mode', 'code_mode_only',
            'context_management', 'current_time_reminder', 'deferred_executor', 'enable_fanout',
            'multi_agent_v2', 'request_permissions_tool', 'standalone_web_search', 'token_budget',
            'tool_suggest', 'unified_exec', 'sleep_tool', 'responses_websockets', 'responses_websockets_v2')


@lru_cache(maxsize=1)
def catalogue():
    return json.loads(Path(__file__).with_name('run_models.json').read_text())


def executable(spec, environment):
    paths = os.pathsep.join(os.get_exec_path({k: v for k, v in environment.items() if isinstance(v, str)}))
    found = shutil.which(spec['spawn'][0], path=paths)
    return str(Path(found).resolve()) if found else None


def login_home(environment):
    """Read configuration and file metadata, never the credential file's contents."""
    home = session_home(environment)
    if home is None:
        raise RunFailure('unavailable', 'The Codex file-login home cannot be resolved')
    home = Path(home)
    try:
        # Metadata only. A missing optional lock is different from a dangling or
        # unreadable one. Native account/read later validates the login itself.
        credentials = [(home / 'auth.json').stat()]
        lock = home / 'auth.lock'
        try:
            credentials.append(lock.stat())
        except FileNotFoundError:
            try:
                lock.lstat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError
        if any(not stat.S_ISREG(info.st_mode) for info in credentials):
            raise ValueError
        config_path = home / 'config.toml'
        config = {}
        try:
            descriptor = os.open(config_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            pass
        else:
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or any(
                        (info.st_dev, info.st_ino) == (credential.st_dev, credential.st_ino)
                        for credential in credentials):
                    raise ValueError
                # The validated descriptor, not a reopened path, is the only
                # source of bytes. Nonblocking open also keeps FIFOs from
                # hanging synchronous preflight before core can check its deadline.
                raw = bytearray()
                while len(raw) <= 1 << 20:
                    block = os.read(descriptor, (1 << 20) + 1 - len(raw))
                    if not block:
                        break
                    raw.extend(block)
            finally:
                os.close(descriptor)
            if len(raw) > 1 << 20:
                raise ValueError
            config = tomllib.loads(raw.decode())
        if config.get('cli_auth_credentials_store', 'file') != 'file':
            raise RunFailure('unavailable', 'Codex one-shot runs require a file login; keyring and auto stores are unsupported')
        if config.get('model_provider', 'openai') != 'openai' or config.get('openai_base_url'):
            raise RunFailure('unavailable', 'Codex one-shot runs require the standard ChatGPT provider')
        if any(config.get(key) is not None for key in ('forced_login_method', 'forced_chatgpt_workspace_id')):
            raise RunFailure('unavailable', 'Codex one-shot runs do not yet replay forced login constraints')
    except RunFailure:
        raise
    except (OSError, ValueError, UnicodeError):
        raise RunFailure('unavailable', 'The selected Codex file login or configuration cannot be verified') from None
    return home


def options(spec, environment):
    binary = executable(spec, environment)
    if binary is None:
        return RunOptions(supported=False, reason='Codex is not installed on the selected PATH')
    version = codex_version(binary, str(Path(binary).parent))
    if version not in VERSIONS:
        return RunOptions(supported=False, reason='Codex one-shot runs need a checked zero-tools release; '
                          f'{".".join(map(str, version)) if version else "this version"} has not been checked')
    try:
        login_home(environment)
    except RunFailure as error:
        return RunOptions(supported=False, reason=error.reason)
    return RunOptions(supported=True, images=True, formats=(FORMAT,))


def model_info(model, effort):
    item = next((m for m in catalogue()['models'] if m['slug'] == model), None)
    if item is None:
        raise RunFailure('unavailable', 'This model has no checked zero-tools Codex catalogue entry')
    efforts = [m['effort'] for m in item['supported_reasoning_levels']]
    if effort is not None and effort not in efforts:
        raise RunFailure('invalid_request', f'{model} does not take effort {effort}')
    return item, effort if effort is not None else item.get('default_reasoning_level')


def configuration(*, model, catalogue_path, retries=None):
    values = {f'features.{name}': False for name in DISABLED}
    values.update({'model': model, 'model_provider': 'openai', 'web_search': 'disabled',
                   'cli_auth_credentials_store': 'file', 'analytics.enabled': False,
                   'history.persistence': 'none', 'tools.update_plan.enabled': False,
                   'tools.experimental_request_user_input.enabled': False,
                   'features.skip_host_skill_discovery': True, 'agents.enabled': False,
                   'cloud.skills.enabled': False, 'skills.include_instructions': False,
                   'project_doc_max_bytes': 0, 'project_doc_fallback_filenames': [],
                   'check_for_update_on_startup': False,
                   'model_catalog_json': str(catalogue_path)})
    if retries is not None:
        raise RunFailure('invalid_request', 'This Codex release does not expose transport retry control')
    return values


def command(binary, values):
    argv = [binary, 'app-server', '--listen', 'stdio://']
    for key, value in values.items():
        argv += ['-c', key + '=' + json.dumps(value, ensure_ascii=False)]
    return tuple(argv)


def environment(source, private):
    """An allowlist prevents native debug, auth, plugin and provider env inheritance."""
    result = {k: v for k, v in source.items() if k in SAFE_ENV and isinstance(v, str)}
    result.update(HOME=str(private / 'home'), CODEX_HOME=str(private / 'codex'),
                  CODEX_SQLITE_HOME=str(private / 'codex'), TMPDIR=str(private / 'tmp'),
                  TMP=str(private / 'tmp'), TEMP=str(private / 'tmp'),
                  XDG_CONFIG_HOME=str(private / 'home'), XDG_DATA_HOME=str(private / 'home'),
                  XDG_CACHE_HOME=str(private / 'home'), XDG_STATE_HOME=str(private / 'home'),
                  RUST_LOG='off')
    return result


def plan(spec, request, *, source, directory):
    supported = options(spec, source)
    if not supported.supported:
        raise RunFailure('unavailable', supported.reason)
    binary, home = executable(spec, source), login_home(source)
    model, effort = model_info(request.model, request.effort)
    private = directory.path / 'private'
    cwd = private / 'work'
    at = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    from .run_history import session
    from .ask import AskRequest
    replay = session(request.messages, model=request.model, session_id=directory.id, cwd=cwd, at=at,
                     system=request.system, own_conversation=isinstance(request, AskRequest))
    fallback_id = str(uuid4()) if replay.reasoning_accounts else None
    fallback = (session(request.messages, model=request.model, session_id=fallback_id, cwd=cwd, at=at,
                        system=request.system, drop_reasoning=True) if replay.reasoning_accounts else None)
    # Both copies count against the same request budget, not just the public input.
    if len(replay.content) + (len(fallback.content) if fallback else 0) + len(request.prompt.encode()) > 32 << 20:
        raise RunFailure('too_long', 'The private Codex history copies exceed 32 MiB')
    catalogue_path = private / 'catalogue.json'
    values = configuration(model=request.model, catalogue_path=catalogue_path, retries=request.transport_retries)
    files = [RunFile(path='private/catalogue.json', content=json.dumps({'models': [model]}).encode()),
             RunFile(path='private/codex/config.toml', content=b'check_for_update_on_startup = false\n'),
             RunFile(path='private/codex/app-server-daemon/settings.json',
                     content=b'{"updater":{"autoUpdateEnabled":false}}\n'),
             *(RunFile(path=f'private/{name}/.keep', content=b'') for name in ('home', 'tmp', 'work'))]
    rollout, fallback_path = None, None
    if replay.content:
        path = f'private/codex/sessions/rollout-{at[:19].replace(":", "-")}-{directory.id}.jsonl'
        files.append(RunFile(path=path, content=replay.content))
        rollout = directory.path / path
        if fallback and fallback.content:
            fallback_name = f'private/codex/sessions/rollout-{at[:19].replace(":", "-")}-{fallback_id}.jsonl'
            files.append(RunFile(path=fallback_name, content=fallback.content))
            fallback_path = directory.path / fallback_name
    def select_history(account):
        try:
            replay.check_login(account)
        except RunFailure:
            if fallback is None:
                raise
            fallback.check_login(account)  # An encrypted compaction remains mandatory.
            return fallback_path, fallback.lost
        return rollout, replay.lost
    def dialogue(channel):
        return answer(channel, request, cwd=cwd, rollout=rollout, effort=effort,
                      lost=replay.lost, select_history=select_history)
    return RunPlan(command=CommandSpec(command(binary, values), tuple(source), environment=environment(source, private)),
                   files=tuple(files), disposable=('private',),
                   links=tuple(RunLink(path=f'private/codex/{name}', target=str(home / name))
                               for name in ('auth.json', 'auth.lock')), dialogue=dialogue)


def failure(error):
    """Classify native errors without returning request bodies or unbounded diagnostics."""
    info = error.get('codexErrorInfo') if isinstance(error, dict) else None
    name = next(iter(info), '') if isinstance(info, dict) else info
    message = error.get('message', '') if isinstance(error, dict) else ''
    folded = message.lower() if isinstance(message, str) else ''
    kind = {'contextWindowExceeded': 'too_long', 'usageLimitExceeded': 'usage_limit',
            'sessionBudgetExceeded': 'usage_limit', 'rateLimitExceeded': 'rate_limit',
            'serverOverloaded': 'rate_limit', 'flexUnavailable': 'rate_limit',
            'policyCapacityUnavailable': 'rate_limit', 'internalServerError': 'rate_limit',
            'modelNotFound': 'invalid_request', 'cyberPolicy': 'refused', 'misalignmentPolicyViolation': 'refused',
            'httpConnectionFailed': 'rate_limit', 'responseStreamConnectionFailed': 'rate_limit',
            'responseStreamDisconnected': 'rate_limit', 'responseTooManyFailedAttempts': 'rate_limit',
            'badRequest': 'invalid_request', 'unauthorized': 'unavailable'}.get(name)
    if kind is None and isinstance(error, dict):
        kind = {-32600: 'invalid_request', -32601: 'unavailable', -32602: 'invalid_request',
                -32001: 'rate_limit'}.get(error.get('code'))
    if kind is None:
        kind = 'usage_limit' if 'usage limit' in folded else 'rate_limit' if 'rate limit' in folded else 'failed'
    # Codex diagnostics can contain upstream request fragments. Keep a fixed reason.
    return RunFailure(kind, f'Codex did not complete the private turn ({kind})')


class Dialogue:
    """Caller-side protocol state; the transport has no Codex or adapter decisions."""
    def __init__(self, channel):
        self.channel, self.sequence, self.events = channel, 0, []

    def notify(self, method, params=None):
        self.channel.send({'jsonrpc': '2.0', 'method': method, **({'params': params} if params is not None else {})})

    def receive(self):
        item = self.channel.receive()
        if not isinstance(item, dict):
            raise RunFailure('unavailable', 'Codex returned an unsupported private protocol frame')
        if 'method' in item and 'id' in item:
            # No approval, auth refresh callback, tool or elicitation is part of this protocol.
            raise RunFailure('unavailable', 'Codex requested an unsupported operation during a tool-free run')
        return item

    def request(self, method, params, *, unavailable=False):
        self.sequence += 1
        identifier = self.sequence
        self.channel.send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params})
        while True:
            item = self.receive()
            if item.get('id') == identifier:
                if 'error' in item:
                    if unavailable:
                        raise RunFailure('unavailable', f'Codex could not complete private setup ({method})')
                    raise failure(item['error'])
                if not isinstance(item.get('result'), dict):
                    raise RunFailure('unavailable', 'Codex returned an unsupported private protocol result')
                return item['result']
            if 'id' in item:
                raise RunFailure('unavailable', 'Codex returned an unexpected private protocol response')
            self.events.append(item)
            if len(self.events) > 10000:
                raise RunFailure('unavailable', 'Codex emitted too many setup events')


def answer(channel, request, *, cwd, rollout=None, effort=None, lost=(), select_history=None):
    dialogue = Dialogue(channel)
    initialized = dialogue.request('initialize', {'clientInfo': {'name': 'multiverse-run', 'version': '1'},
                                                 'capabilities': {'experimentalApi': True}}, unavailable=True)
    if server_version(initialized.get('userAgent')) not in VERSIONS:
        raise RunFailure('unavailable', 'The private Codex version has not passed the zero-tools check')
    dialogue.notify('initialized')
    account = dialogue.request('account/read', {'refreshToken': False}, unavailable=True)
    if not isinstance(account.get('account'), dict) or account['account'].get('type') != 'chatgpt':
        raise RunFailure('unavailable', 'The private Codex could not use a ChatGPT file login')
    if select_history is not None:
        rollout, lost = select_history(account)
    config = dialogue.request('config/read', {'includeLayers': False}, unavailable=True).get('config')
    if not isinstance(config, dict) or config.get('mcp_servers'):
        raise RunFailure('unavailable', 'The private Codex configuration contains MCP servers or is unreadable')
    params = {'model': request.model, 'modelProvider': 'openai', 'cwd': str(cwd),
              'runtimeWorkspaceRoots': [], 'approvalPolicy': 'never', 'sandbox': 'read-only',
              'baseInstructions': request.system, 'developerInstructions': ''}
    if rollout is None:
        params.update(ephemeral=True, environments=[], dynamicTools=[], selectedCapabilityRoots=[])
        result = dialogue.request('thread/start', params)
    else:
        params.update(threadId='', path=str(rollout), excludeTurns=True)
        result = dialogue.request('thread/resume', params)
    if result.get('model') != request.model or result.get('modelProvider') != 'openai':
        raise RunFailure('unavailable', 'Codex changed the private run model or provider')
    if result.get('instructionSources'):
        raise RunFailure('unavailable', 'Codex discovered instructions outside the copied conversation')
    thread = result.get('thread', {}).get('id')
    if not isinstance(thread, str) or not thread:
        raise RunFailure('unavailable', 'Codex did not create a private thread')
    result = dialogue.request('turn/start', {'threadId': thread, 'environments': [], 'effort': effort,
                             'input': [{'type': 'text', 'text': request.prompt, 'text_elements': []}]})
    turn = result.get('turn', {}).get('id')
    if not isinstance(turn, str) or not turn:
        raise RunFailure('unavailable', 'Codex did not start a private turn')
    usage, texts, deltas = {}, {}, {}
    pending = iter(dialogue.events)
    while True:
        item = next(pending, None)
        if item is None:
            item = dialogue.receive()
        method, params = item.get('method'), item.get('params', {})
        if not isinstance(params, dict) or params.get('threadId') != thread:
            continue
        if method == 'thread/tokenUsage/updated':
            last = params.get('tokenUsage', {}).get('last', {})
            usage = {key: last[native] for key, native in (('input', 'inputTokens'), ('output', 'outputTokens'),
                     ('cache_read', 'cachedInputTokens'), ('reasoning', 'reasoningOutputTokens'))
                     if type(last.get(native)) is int and last[native] >= 0}
        if params.get('turnId') == turn:
            if method == 'item/agentMessage/delta' and isinstance(params.get('delta'), str):
                identifier = params.get('itemId', '')
                deltas[identifier] = deltas.get(identifier, '') + params['delta']
            if method == 'item/completed':
                native = params.get('item', {})
                if native.get('type') == 'agentMessage' and isinstance(native.get('text'), str):
                    texts[native.get('id', '')] = native['text']
        if method == 'turn/completed' and params.get('turn', {}).get('id') == turn:
            final = params['turn']
            if final.get('status') != 'completed':
                error = failure(final.get('error'))
                error.model, error.usage = request.model, usage
                raise error
            text = '\n'.join((texts or deltas).values())
            return RunReply(text=text, model=request.model, usage=usage, stop='completed', lost=tuple(lost))
