"""One tool-less Claude reply over a given conversation: the session file a run resumes, the checks made before
anything is spent, the command line, and how Claude's JSON result is read.

Claude takes a conversation only as a stored session, so a run writes one into its own project folder, resumes it
with the prompt as the next user message, and keeps nothing (`--no-session-persistence`). This module only builds
and reads; core writes the file, starts and supervises the process, and removes what it created.

Messages are the run protocol's (harnesses/types.py), read by their fields: a `RunRow` (`format`, `data`) is a
row of a Claude transcript, passed through; a `RunMessage` (`role`, `blocks`) is built, each block a `RunText`
(`text`) or a `RunImage` (`media_type`, `data`, on user messages only).
"""
import base64
import json
import re
import uuid as uuidlib

FORMAT = 'claude-jsonl'
RETRIES = (0, 15)  # CLAUDE_CODE_MAX_RETRIES, Claude's own quick retries of a failed request

# Set on every run: a refusal comes back as one, from the model asked (2.1.312 retries a refused request and can
# switch models after a flagged message), and no CLAUDE.md or auto-memory enters its context. (A real 2.1.312 run read
# no CLAUDE.md in its own folder even without the switch; it stays for the user's own ~/.claude/CLAUDE.md.)
ENVIRONMENT = {'CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK': '1', 'CLAUDE_CODE_DISABLE_REFUSAL_RETRY': '1',
               'CLAUDE_CODE_DISABLE_CLAUDE_MDS': '1', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'}
# Removed on top of the identity every launch drops (harnesses/base.py PARENT_CLAUDE): a run loads no mod.
UNSET = ('CLAUDE_CODE_ENABLE_FUNCTION_HOOKS', 'CODEX_THREAD_ID')


class Unusable(ValueError):
    """The messages can't be sent as given; the reason names the rule, never their content."""


def _uuid():
    return str(uuidlib.uuid4())


def _base(kind, parent, session_id, cwd, at, message):
    return {'parentUuid': parent, 'uuid': _uuid(), 'sessionId': session_id, 'type': kind, 'timestamp': at,
            'cwd': cwd, 'userType': 'external', 'message': message}


def _built(item, parent, session_id, cwd, at, model):
    """Rows for one built message: a user message as one row; an assistant message as one row per block, sharing
    one message ID, `end_turn` on the last only, as Claude stores a response."""
    role, blocks = getattr(item, 'role', None), getattr(item, 'blocks', None)
    if role not in ('user', 'assistant') or not isinstance(blocks, (tuple, list)):
        raise Unusable('a message needs a role of user or assistant and its blocks')
    content = []
    for block in blocks:
        text, media, data = (getattr(block, name, None) for name in ('text', 'media_type', 'data'))
        if isinstance(text, str) and media is None:
            content.append({'type': 'text', 'text': text})
        elif isinstance(media, str) and isinstance(data, (bytes, bytearray)) and text is None:
            if role != 'user':
                raise Unusable('an image can only be on a user message')
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media,
                                                        'data': base64.b64encode(bytes(data)).decode()}})
        else:
            raise Unusable('a block is a text or an image')
    if role == 'user':
        return [_base('user', parent, session_id, cwd, at, {'role': 'user', 'content': content})]
    if not content:
        raise Unusable('an assistant message needs a block')
    message_id, rows = f'msg_{uuidlib.uuid4().hex}', []
    for position, block in enumerate(content):
        row = _base('assistant', parent, session_id, cwd, at, {
            'id': message_id, 'type': 'message', 'role': 'assistant', 'model': model, 'content': [block],
            'stop_reason': 'end_turn' if position == len(content) - 1 else None, 'stop_sequence': None, 'usage': {}})
        rows.append(row)
        parent = row['uuid']
    return rows


def _copied(row, parent, session_id, cwd):
    """A transcript row in the run's session: its own session, folder and parent; recorded usage cleared (Claude
    sizes a resumed session from the last usage it finds, and refuses "Prompt is too long" before any request);
    its model kept, since copied thinking carries the writing model's signature."""
    if not isinstance(row, dict) or row.get('type') not in ('user', 'assistant') or not isinstance(row.get('message'), dict):
        raise Unusable('a passed-through row must be a user or assistant row of a Claude transcript, with its body')
    copy = {key: value for key, value in row.items() if key not in ('isSidechain', 'logicalParentUuid', 'agentId')}
    copy.update(parentUuid=parent, uuid=row.get('uuid') if isinstance(row.get('uuid'), str) else _uuid(),
                sessionId=session_id, cwd=cwd)
    if 'usage' in copy['message']:
        copy['message'] = {**copy['message'], 'usage': {}}
    return copy


def session(messages, *, session_id, cwd, model, at):
    """The session file's rows for `messages`, as one linear conversation in the order given (a real 2.1.312 run
    needed no title row, which GOAT writes); none for no messages, a prompt asked on its own."""
    rows, parent, seen = [], None, set()
    for item in messages:
        if getattr(item, 'format', None) is not None:
            if item.format != FORMAT:
                raise Unusable(f'a passed-through row must be of the {FORMAT} format')
            made = [_copied(item.data, parent, session_id, cwd)]
        else:
            made = _built(item, parent, session_id, cwd, at, model)
        for row in made:
            if row['uuid'] in seen:  # a row passed twice: a second uuid, or the chain would loop
                row['uuid'] = _uuid()
            seen.add(row['uuid'])
        rows += made
        parent = made[-1]['uuid']
    rows = ordered(rows)
    for index, row in enumerate(rows):  # one linear chain in the stored order, which ordered() may have changed
        row['parentUuid'] = rows[index - 1]['uuid'] if index else None
    if rows:
        check(rows)
    return rows


def _answers(row):
    """The tool call IDs a user row answers, when it holds tool results and nothing else; else None."""
    content = row.get('message', {}).get('content') if row.get('type') == 'user' else None
    if not isinstance(content, list) or not content:
        return None
    if not all(isinstance(block, dict) and block.get('type') == 'tool_result' for block in content):
        return None
    return {block.get('tool_use_id') for block in content}


def ordered(rows):
    """`rows` with each response's rows first and then the results of its calls, as the API saw that turn.

    Claude stores one response as a row per block and runs its tool calls as they stream in, storing each result as
    soon as the call ends, so a response with several calls can be stored call, call, result, call, result... (with
    forked parent links). Read in that order its calls and results don't pair. A result row moves only when it holds
    results and nothing else, for calls of the response it sits inside; anything else (a prompt, another response,
    a result for some other call) ends the response there, and check() then judges the rows as they are. The
    response's own rows keep their order and content, so a thinking block's signature still holds."""
    result, index = [], 0
    while index < len(rows):
        row = rows[index]
        message_id = row.get('message', {}).get('id') if row.get('type') == 'assistant' else None
        if not message_id:
            result.append(row)
            index += 1
            continue
        said, answers, calls = [], [], set()
        while index < len(rows):
            row = rows[index]
            answered = _answers(row)
            if row.get('type') == 'assistant' and row.get('message', {}).get('id') == message_id:
                said.append(row)
                content = row['message'].get('content')
                calls |= {block.get('id') for block in content if isinstance(block, dict) and
                          block.get('type') == 'tool_use'} if isinstance(content, list) else set()
            elif answered is not None and answered <= calls:
                answers.append(row)
            else:
                break
            index += 1
        result += said + answers
    return result


def _messages(rows):
    """The conversation as the API sees it: consecutive rows of one role, and one response's rows, as one message."""
    merged = []
    for row in rows:
        if row.get('type') not in ('user', 'assistant'):
            continue
        message = row['message']
        content = message.get('content')
        blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content}]
        if merged and merged[-1][0] == row['type']:
            merged[-1][1].extend(blocks)
        else:
            merged.append((row['type'], list(blocks)))
    return merged


def check(rows):
    """Refuse what Claude would refuse after spending nothing: the last message must be the assistant's (the prompt
    is the next user message), and every tool call needs its result in the next user message, with no result
    without its call and no call ID twice (Claude fails those as a 400 tool_history_mismatch)."""
    merged, calls = _messages(rows), set()
    if not merged:
        return  # no messages: a prompt on its own, which needs no session
    if merged[-1][0] != 'assistant':
        raise Unusable('the messages must end with an assistant message; the prompt is the next user message')
    waiting = set()
    for role, blocks in merged:
        ids = [block.get('id') for block in blocks if isinstance(block, dict) and block.get('type') == 'tool_use']
        answered = [block.get('tool_use_id') for block in blocks
                    if isinstance(block, dict) and block.get('type') == 'tool_result']
        if role == 'user':
            if set(answered) != waiting or len(answered) != len(set(answered)):
                raise Unusable('every tool call needs exactly one result in the next user message, '
                               'and every result its call in the message before')
            waiting = set()
        else:
            if waiting:
                raise Unusable('a tool call has no result before the next assistant message')
            if any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)) or calls & set(ids):
                raise Unusable('a tool call ID is missing or used twice')
            calls |= set(ids)
            waiting = set(ids)
    if waiting:
        raise Unusable('the last assistant message has a tool call without a result')


def command(executable, *, prompt, session_id, model, effort, system):
    """The run's command line: no tools, MCP servers, commands or project settings, one turn, JSON out, and the
    run's own system prompt fresh on every request (`--system-prompt-snapshot off`, 2.1.267 and newer). With no
    `session_id` (a run without messages) there is nothing to resume, and Claude answers the prompt alone.

    The prompt comes last, after `--`, so Claude reads it as text whatever it starts with: before `--`, a prompt
    of `--version` or `--resume=ID` is an option, and the second makes the run read another conversation."""
    strip = lambda text: text.replace('\x00', '')
    resume = ['--resume', session_id] if session_id is not None else []
    argv = [executable, '-p', *resume, '--no-session-persistence', '--tools', '',
            '--strict-mcp-config', '--disable-slash-commands', '--setting-sources', 'user', '--max-turns', '1',
            '--output-format', 'json', '--model', model]
    if effort is not None:
        argv += ['--effort', effort]
    return argv + ['--system-prompt', strip(system), '--system-prompt-snapshot', 'off', '--', strip(prompt)]


def environment(transport_retries):
    """What a run sets: Claude's own retries, and the variables in ENVIRONMENT."""
    low, high = RETRIES
    if type(transport_retries) is not int or not low <= transport_retries <= high:
        raise Unusable(f'transport_retries is a whole number from {low} to {high}')
    return {**ENVIRONMENT, 'CLAUDE_CODE_MAX_RETRIES': str(transport_retries)}


# Claude's typed error kinds (2.1.312's result `api_error`), read before its text; new ones are added over time,
# so the text rules below stay the fallback.
TYPED = {'usage_limit_reached': 'usage_limit', 'model_requires_usage_credits': 'usage_limit',
         'long_context_credits_required': 'usage_limit', 'tool_history_mismatch': 'invalid_request',
         'safety_monitor_blocked': 'refused', 'no_response': 'rate_limit',
         'provider_credentials': 'failed', 'gateway_signin_required': 'failed',
         'gateway_session_expired': 'failed', 'api_key_auth_disabled': 'failed'}
USAGE_LIMIT = re.compile(r'usage limit|limit reached|hit your limit|out of extra usage|spend limit', re.IGNORECASE)
TRANSIENT = re.compile(r'rate.?limit|overloaded|529|too many requests|timed? ?out|ECONNRESET|socket hang up',
                       re.IGNORECASE)
TOO_LONG = re.compile(r'prompt is too long', re.IGNORECASE)
LIMIT = 300  # characters of Claude's own error text kept in a message
# The reply's usage, under the run protocol's names, from Claude's.
USAGE = {'input': 'input_tokens', 'output': 'output_tokens', 'cache_read': 'cache_read_input_tokens',
         'cache_write': 'cache_creation_input_tokens'}


def _said(*texts):
    return ' '.join(' '.join(text.split()) for text in texts if text)[:LIMIT]


def _spent(envelope, model):
    """What a result says the run used, a failed one included: the model that answered, usage and cost."""
    used = envelope.get('modelUsage')
    usage = envelope.get('usage') if isinstance(envelope.get('usage'), dict) else {}
    cost = envelope.get('total_cost_usd')
    return {'model': next(iter(used)) if isinstance(used, dict) and len(used) == 1 else model,
            'usage': {name: usage[key] for name, key in USAGE.items() if type(usage.get(key)) is int},
            'cost_usd': cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None}


def reply(returncode, stdout, stderr, *, model):
    """Claude's JSON result as an outcome: `{'kind': None, 'text', 'model', 'usage', 'cost_usd', 'stop'}` for a
    reply, or `{'kind': KIND, 'message': ...}` with Claude's own error text, bounded, never the reply's, plus what
    a failed run used when the result says (`model`, `usage`, `cost_usd`)."""
    try:
        envelope = json.loads(stdout)
    except (TypeError, ValueError):
        envelope = None
    if not isinstance(envelope, dict):
        said = _said(stderr, stdout) or f'exit {returncode} with no output'
        kind = 'too_long' if TOO_LONG.search(said) else 'usage_limit' if USAGE_LIMIT.search(said) else \
            'rate_limit' if TRANSIENT.search(said) else 'failed'
        return {'kind': kind, 'message': f'claude exited {returncode}: {said}'}
    result = envelope.get('result') if isinstance(envelope.get('result'), str) else ''
    status, typed = envelope.get('api_error_status'), envelope.get('api_error')
    status = status if type(status) is int else None
    if envelope.get('is_error') is True or returncode != 0:
        said = _said(result, stderr) or (f'HTTP {status}' if status else f'exit {returncode}')
        kind = TYPED.get(typed) if isinstance(typed, str) else None
        if kind is None:
            kind = 'refused' if envelope.get('stop_reason') == 'refusal' else 'too_long' if TOO_LONG.search(said) \
                else 'invalid_request' if status == 404 else 'usage_limit' if USAGE_LIMIT.search(said) \
                else 'rate_limit' if status in (408, 429, 529) or (status or 0) >= 500 or TRANSIENT.search(said) \
                else 'failed'
        return {'kind': kind, 'message': said, **_spent(envelope, model)}
    if envelope.get('stop_reason') == 'refusal':
        return {'kind': 'refused', 'message': 'the model refused', **_spent(envelope, model)}
    return {'kind': None, 'text': result, 'stop': envelope.get('stop_reason'), **_spent(envelope, model)}
