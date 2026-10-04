"""A Claude Code session transcript as turns of leaves, before any compaction.

Ported from Multiverse v1's parser (multiverse-plugin/vendor/compaction/parser.py)
as v1's compaction_forest() uses it: one leaf per row (`asst_text`, `tool_use`,
`tool_result`, `user_text`) and v1's token counts (about four characters a
token, images by their pixel size). Then v1's preprocessing with
`auto_merge_tool_pairs` on: each tool call and its result become one
`tool_exchange` leaf at the call's place, so no merge can separate them. Changes
for mv2:

- Thinking is no part of compaction: no leaf, never shown to the merge model,
  never delivered (a built message cannot hold a signed block, and Opus 5.5
  drops thinking whose preceding conversation changed). Its estimated size is
  kept as the turn's `omitted` tokens, for the size before compaction.

- History crosses compactions. `/compact` keeps every earlier row in the same
  file and adds a boundary row whose `logicalParentUuid` names the row before
  it, so the walk from the newest row follows that link back. The rows a
  compaction added (Claude's summary, rows our install wrote, the boundary
  itself) are left out.
- Display-only command turns are marked `dropped` instead of being cut out of
  the rows, so compaction leaves them out wherever it looks. `/compact` and a
  few more display commands join v1's `/context`, `/model` and `/effort`, and
  the plain "/compact" prompt row Claude 2.1 also stores is recognized.
- The mod's `/multiverse inbox` and `/multiverse send` runs are conversation
  (the inbox they read, the messages they sent): each is a turn of its own,
  its command row the prompt and its recorded output the leaf. Bare
  `/multiverse`, `/multiverse list` and `/multiverse <name>` only display the
  catalog or confirm a prompt they submit, which is a turn of its own anyway:
  they are left out like the other display-only commands.
- A response with several thinking blocks shares its thinking estimate among
  them by signature length; v1 charged each block the whole estimate.
- v1 folded Multiverse v1's inter-agent messages into prompts; mv2 delivers
  messages as prompts or queued commands, which are read as they are.
"""
import json
from pathlib import Path
import re

from ....compaction.model import Leaf, Turn, estimate_tokens
from . import images as image_tokens

# Rows that carry no conversation content.
SKIP_TYPES = frozenset({'custom-title', 'agent-name', 'permission-mode', 'file-history-snapshot', 'attachment',
                        'last-prompt', 'queue-operation', 'system', 'summary', 'mode', 'cost-state', 'atis-latch'})
# Command turns that only display something: they are dropped from compaction.
SKIP_COMMANDS = frozenset({'/context', '/model', '/effort', '/compact', '/cost', '/usage', '/status', '/statusline'})
COMMAND_NAME = re.compile(r'<command-name>(/[\w:-]+)</command-name>')
COMMAND_ARGS = re.compile(r'<command-args>(.*?)</command-args>', re.DOTALL)
COMMAND_OUTPUT = '<local-command-stdout>'
# Commands whose runs are conversation, by subcommand: what /multiverse read from
# the inbox and the messages it sent. Its other forms only display something.
CONVERSATION = {'/multiverse': frozenset({'inbox', 'send'})}
# Claude also stores the command as typed, a prompt of its own: "/compact" or "/compact keep the plan".
TYPED_COMMAND = re.compile(r'\s*(/[\w:-]+)(?:\s|$)')
# Tools whose results Claude itself may evict under pressure: an unpaired result
# of one does not earn a turn a bigger share of the budget (v1's allocation weight).
EVICTABLE = frozenset({'Read', 'Bash', 'Grep', 'Glob', 'WebSearch', 'WebFetch', 'Edit', 'Write', 'MultiEdit',
                       'NotebookEdit', 'BashOutput'})


def read_rows(path) -> list[dict]:
    """Every complete row of a transcript; a row still being written is left for later."""
    rows = []
    with Path(path).open(encoding='utf-8') as handle:
        for line in handle:
            if not line.endswith('\n'):
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def is_boundary(row: dict) -> bool:
    return row.get('type') == 'system' and row.get('subtype') == 'compact_boundary'


def chain(rows: list[dict]) -> list[dict]:
    """The rows of the current conversation, oldest first, across every compaction.

    The newest leaf of the parent graph is the live tip (an abandoned branch of
    a rewound conversation is left out); the walk goes back through parent
    links and, at a compaction boundary, on to the conversation before it.
    Boundary rows are included so callers can tell what each compaction added.

    Parallel tool calls branch: Claude links each result to its own call, and
    only the last result stored continues the conversation. So the walk also
    takes every block of each response it passes and every result answering
    one of their calls (v1's walk left these out).
    """
    by_uuid, parented, order = {}, set(), {}
    for index, row in enumerate(rows):
        uuid = row.get('uuid')
        if not uuid or row.get('isSidechain'):
            continue
        by_uuid[uuid], order[uuid] = row, index
        for link in (row.get('parentUuid'), row.get('logicalParentUuid')):
            if link:
                parented.add(link)
    tips = [uuid for uuid in by_uuid if uuid not in parented]
    if not tips:
        return []
    current = max(tips, key=lambda uuid: (by_uuid[uuid].get('timestamp') or '', order[uuid]))
    seen = set()
    while current in by_uuid and current not in seen:
        seen.add(current)
        row = by_uuid[current]
        current = row.get('logicalParentUuid') if is_boundary(row) else row.get('parentUuid')
    responses = {_response(by_uuid[uuid]) for uuid in seen} - {None}
    seen |= {uuid for uuid, row in by_uuid.items() if _response(row) in responses}
    calls = {block.get('id') for uuid in seen for block in _blocks(by_uuid[uuid])
             if by_uuid[uuid].get('type') == 'assistant' and block.get('type') == 'tool_use'} - {None}
    seen |= {uuid for uuid, row in by_uuid.items() if row.get('type') == 'user' and any(
        block.get('type') == 'tool_result' and block.get('tool_use_id') in calls for block in _blocks(row))}
    return [by_uuid[uuid] for uuid in sorted(seen, key=order.get)]


def _response(row) -> str | None:
    message = row.get('message')
    return message.get('id') if row.get('type') == 'assistant' and isinstance(message, dict) else None


def _blocks(row) -> list[dict]:
    content = (row.get('message') or {}).get('content')
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _text_blob(row) -> str:
    parts = []
    message = row.get('message')
    for value in (row.get('content'), message.get('content') if isinstance(message, dict) else None):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(block.get('text', '') or '' for block in value if isinstance(block, dict) and block.get('type') == 'text')
    return '\n'.join(parts)


def _invocation(text: str) -> tuple[str, str] | None:
    """The command a row records and its arguments: from its command-name tag, or as typed."""
    named = COMMAND_NAME.search(text)
    if named:
        found = COMMAND_ARGS.search(text)
        return named.group(1), found.group(1).strip() if found else ''
    typed = TYPED_COMMAND.match(text)
    return (typed.group(1), text[typed.end():].strip()) if typed else None


def conversation_command(name: str, args: str) -> bool:
    """Whether a command's run is conversation: /multiverse inbox and /multiverse send."""
    words = args.split()
    return bool(words) and words[0] in CONVERSATION.get(name, ())


def display_only(name: str, args: str) -> bool:
    return name in SKIP_COMMANDS or (name in CONVERSATION and not conversation_command(name, args))


def _command(row) -> str | None:
    """The display-only command a user row records, if any."""
    text = _text_blob(row)
    found = _invocation(text) if text else None
    if found is None or not display_only(*found):
        return None
    return found[0] if found[0] in SKIP_COMMANDS else ' '.join(part for part in found if part)


def _command_leaf(row, text: str) -> Leaf:
    return Leaf('user_text', (row['uuid'],), estimate_tokens(text), _preview(text, 'user'), {'text': text})


def strip_command_rows(rows: list[dict]) -> list[dict]:
    """Drop the rows of display-only commands stored outside any turn.

    Some versions store a command as `system`/`local_command` rows followed by
    meta user rows, all without a promptId, which would otherwise read as part
    of the turn before. Commands stored as user rows under a promptId of their
    own form a turn, which `turns` marks as dropped.
    """
    result, in_command = [], False
    for row in rows:
        if row.get('type') == 'system' and row.get('subtype') == 'local_command':
            text = row.get('content') if isinstance(row.get('content'), str) else ''
            if COMMAND_NAME.search(text):
                in_command = display_only(*_invocation(text))
            if in_command:
                continue
        elif in_command and row.get('type') == 'user' and row.get('isMeta'):
            continue
        elif row.get('type') != 'system':
            in_command = False
        result.append(row)
    return result


def _preview(text: str, kind: str, limit: int = 120) -> str:
    if not text:
        return f'<empty {kind}>'
    flat = ' '.join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 3] + '...'


def _short_args(value, limit=80) -> str:
    if isinstance(value, dict):
        for key in ('command', 'file_path', 'path', 'url', 'query', 'pattern'):
            if isinstance(value.get(key), str):
                text = value[key]
                return text if len(text) <= limit else text[:limit - 3] + '...'
        text = json.dumps(value)
    else:
        text = str(value)
    return text if len(text) <= limit else text[:limit - 3] + '...'


def _attachment_leaf(row) -> Leaf | None:
    """A prompt the person queued mid-turn is real context; every other attachment is not."""
    attachment = row.get('attachment')
    if not isinstance(attachment, dict) or attachment.get('type') != 'queued_command':
        return None
    prompt = attachment.get('prompt')
    if isinstance(prompt, list):
        prompt = '\n'.join(block.get('text', '') for block in prompt if isinstance(block, dict) and block.get('type') == 'text')
    if not isinstance(prompt, str) or not prompt:
        return None
    return Leaf('user_text', (row['uuid'],), estimate_tokens(prompt), _preview(prompt, 'user'), {'text': prompt})


def _images(blocks) -> list[dict]:
    return [block for block in blocks if isinstance(block, dict) and block.get('type') == 'image']


def classify(row) -> Leaf | None:
    """One transcript row as a leaf, or None for rows without conversation content.

    Claude stores each content block of a response as its own row.
    """
    kind = row.get('type')
    if kind == 'attachment':
        return _attachment_leaf(row)
    if kind in SKIP_TYPES or kind is None:
        return None
    message = row.get('message')
    if not isinstance(message, dict):
        return None
    content, uuid = message.get('content'), row['uuid']
    if kind == 'user' and isinstance(content, str):
        return Leaf('user_text', (uuid,), estimate_tokens(content), _preview(content, 'user'), {'text': content})
    if kind == 'user' and isinstance(content, list):
        top = _images(content)
        pictures = sum(image_tokens.tokens(block) for block in top)
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_result':
                result = block.get('content')
                inner = []
                if isinstance(result, list):
                    text = '\n'.join(part.get('text', '') for part in result if isinstance(part, dict) and part.get('type') == 'text')
                    inner = _images(result)
                else:
                    text = '' if result is None else str(result)
                both = pictures + sum(image_tokens.tokens(part) for part in inner)
                return Leaf('tool_result', (uuid,), estimate_tokens(text) + both, _preview(text, 'tool_result'),
                            {'tool_use_id': block.get('tool_use_id'), 'text': text, 'is_error': bool(block.get('is_error'))},
                            images=both)
            if block.get('type') == 'text':
                text = block.get('text', '') or ''
                return Leaf('user_text', (uuid,), estimate_tokens(text) + pictures, _preview(text, 'user'),
                            {'text': text, 'images': len(top)}, images=pictures)
        if top:
            return Leaf('user_text', (uuid,), pictures, f'<{len(top)} image(s)>', {'text': '', 'images': len(top)},
                        images=pictures)
        return None
    if kind == 'assistant' and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text':
                text = block.get('text', '') or ''
                return Leaf('asst_text', (uuid,), estimate_tokens(text), _preview(text, 'asst'), {'text': text})
            if block.get('type') == 'tool_use':
                name, arguments = block.get('name', '?'), block.get('input', {})
                rendered = f'{name}({_short_args(arguments)})'
                return Leaf('tool_use', (uuid,), estimate_tokens(rendered) + estimate_tokens(json.dumps(arguments)),
                            rendered, {'tool_use_id': block.get('id'), 'name': name, 'input': arguments})
    return None


def _visible_tokens(message) -> int:
    total = 0
    for block in message.get('content') or []:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'text':
            total += estimate_tokens(block.get('text', '') or '')
        elif block.get('type') == 'tool_use':
            total += estimate_tokens(block.get('name', '') + json.dumps(block.get('input', {})))
    return total


def thinking_estimates(rows) -> dict[str, int]:
    """Estimated tokens of each thinking row: its response's output tokens less the visible text.

    The estimate is per response; a response with several thinking blocks shares
    it among them by signature length (equally when no signature tells). A
    block whose text is stored never counts less than that text.
    """
    output, visible, blocks = {}, {}, {}
    for row in rows:
        message = row.get('message') if row.get('type') == 'assistant' else None
        if not isinstance(message, dict) or not message.get('id'):
            continue
        response = message['id']
        usage = message.get('usage')
        if isinstance(usage, dict) and isinstance(usage.get('output_tokens'), int) and usage['output_tokens'] > 0:
            output[response] = max(output.get(response, 0), usage['output_tokens'])
        visible[response] = visible.get(response, 0) + _visible_tokens(message)
        for block in message.get('content') or []:
            if isinstance(block, dict) and block.get('type') in ('thinking', 'redacted_thinking'):
                weight = len(block.get('signature') or block.get('data') or '') or 1
                blocks.setdefault(response, []).append((row['uuid'], weight, estimate_tokens(block.get('thinking') or '')))
    estimates = {}
    for response, entries in blocks.items():
        total = max(0, output.get(response, 0) - visible.get(response, 0))
        weights = sum(weight for _, weight, _ in entries)
        for uuid, weight, shown in entries:
            estimates[uuid] = max(shown, total * weight // weights)
    return estimates


def _summarize_result(text: str, is_error: bool, limit: int = 50) -> str:
    """v1's one-line outcome of a tool result, for a tool exchange's preview."""
    if not text:
        return '<empty result>'
    flat = text.strip()
    if is_error:
        first = flat.splitlines()[0] if flat else ''
        return 'error: ' + (first if len(first) <= limit else first[:limit - 3] + '...')
    lines = flat.count('\n') + 1
    if lines == 1:
        return flat if len(flat) <= limit else flat[:limit - 3] + '...'
    return f'ok ({lines} lines, {estimate_tokens(flat)} tok)'


def pair_tools(leaves: list[Leaf]) -> list[Leaf]:
    """v1's auto_merge_tool_pairs: each call and its result as one `tool_exchange` leaf at the call's place."""
    results = {leaf.content.get('tool_use_id'): leaf for leaf in leaves
               if leaf.kind == 'tool_result' and leaf.content.get('tool_use_id')}
    used, paired = set(), []
    for leaf in leaves:
        result = results.get(leaf.content.get('tool_use_id')) if leaf.kind == 'tool_use' else None
        if result is not None:
            used.add(id(result))
            name, arguments = leaf.content.get('name', '?'), leaf.content.get('input', {})
            text, error = result.content.get('text', '') or '', bool(result.content.get('is_error'))
            preview = f'{name}({_short_args(arguments, 60)}) -> {_summarize_result(text, error)}'
            paired.append(Leaf('tool_exchange', (*leaf.refs, *result.refs), leaf.tokens + result.tokens,
                               preview if len(preview) <= 120 else preview[:117] + '...',
                               {'tool_use_id': leaf.content.get('tool_use_id'), 'name': name, 'input': arguments,
                                'result_text': text, 'is_error': error},
                               images=leaf.images + result.images))
        elif not (leaf.kind == 'tool_result' and id(leaf) in used):
            paired.append(leaf)
    return paired


def discount_evictable(leaves: list[Leaf]) -> None:
    """v1's allocation weight: an unpaired result of an evictable tool counts for nothing."""
    names = {leaf.content.get('tool_use_id'): leaf.content.get('name') for leaf in leaves
             if leaf.kind in ('tool_use', 'tool_exchange')}
    for leaf in leaves:
        if leaf.kind == 'tool_result' and names.get(leaf.content.get('tool_use_id')) in EVICTABLE:
            leaf.weight = 0


def _starts_turn(row, last_prompt) -> bool:
    """A real prompt starts a turn: Claude gives each a new promptId, inherited by its rows."""
    if row.get('type') != 'user' or not row.get('promptId') or row.get('promptId') == last_prompt:
        return False
    content = (row.get('message') or {}).get('content')
    return isinstance(content, str) or (isinstance(content, list) and any(
        isinstance(block, dict) and block.get('type') in ('text', 'image') for block in content))


def turns(rows: list[dict], added: set[str]) -> list[Turn]:
    """The conversation's turns, oldest first, without rows any compaction added.

    `added` are the rows our installs wrote (from their records); Claude's own
    compaction summaries carry `isCompactSummary`. A display-only command's turn
    is marked dropped.
    """
    path = [row for row in chain(rows) if not is_boundary(row) and row['uuid'] not in added
            and not row.get('isCompactSummary')]
    path = strip_command_rows(path)
    estimates = thinking_estimates(path)
    commands = {}
    for row in path:
        command = row.get('type') == 'user' and row.get('promptId') and _command(row)
        if command:
            commands.setdefault(row['promptId'], command)
    result, current, orphans, last_prompt = [], None, [], None
    omitted = 0  # thinking before the first prompt
    output = False  # the next command output belongs to the command turn just started
    for row in path:
        if row.get('type') == 'system' and row.get('subtype') == 'local_command':
            text = row.get('content') if isinstance(row.get('content'), str) else ''
            found = _invocation(text) if COMMAND_NAME.search(text) else None
            if found is not None and conversation_command(*found):
                # A /multiverse run that is conversation: a turn of its own, its output the leaf.
                if current is not None:
                    result.append(current)
                current = Turn(row['uuid'], _command_leaf(row, text), [*orphans], omitted=omitted)
                orphans, omitted, output = [], 0, True
            elif output and COMMAND_OUTPUT in text:
                current.leaves.append(_command_leaf(row, text))
                output = False
            else:
                output = False
            continue
        output = False
        leaf = classify(row)
        if row['uuid'] in estimates:
            if current is None:
                omitted += estimates[row['uuid']]
            else:
                current.omitted += estimates[row['uuid']]
        if _starts_turn(row, last_prompt):
            if current is not None:
                result.append(current)
            last_prompt = row['promptId']
            current = Turn(row['promptId'], leaf, [], omitted=omitted)
            current.leaves.extend(orphans)
            orphans, omitted = [], 0
            continue
        if leaf is None:
            continue
        if current is None:
            orphans.append(leaf)
        else:
            current.leaves.append(leaf)
    if current is not None:
        result.append(current)
    elif orphans or omitted:
        result.append(Turn('turn-0', None, orphans, omitted=omitted))
    for turn in result:
        turn.leaves = pair_tools(turn.leaves)
        discount_evictable(turn.leaves)
        if turn.id in commands:
            turn.dropped = f'the display-only command {commands[turn.id]}'
    return result


def unrecorded_boundaries(rows: list[dict], recorded: set[str]) -> list[dict]:
    """Our compaction boundaries with no install record, each with the user and assistant rows after it.

    Claude's own boundaries are followed by its summary (or record kept rows).
    """
    path = chain(rows)
    found = []
    for index, row in enumerate(path):
        if is_boundary(row) and row['uuid'] not in recorded:
            following = path[index + 1] if index + 1 < len(path) else {}
            if not following.get('isCompactSummary') and not (row.get('compactMetadata') or {}).get('preservedMessages'):
                # Each installed message is stored as one user or assistant row; Claude may put
                # attachment and system rows between them.
                after = [later for later in path[index + 1:] if later.get('type') in ('user', 'assistant')]
                found.append({'boundary': row, 'after': after})
    return found
