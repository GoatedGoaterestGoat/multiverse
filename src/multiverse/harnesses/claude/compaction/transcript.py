"""A Claude Code session transcript as turns of leaves, before any compaction.

Ported from Multiverse v1's parser (multiverse-plugin/vendor/compaction/parser.py)
with these changes for mv2:

- History crosses compactions. `/compact` keeps every earlier row in the same
  file and adds a boundary row whose `logicalParentUuid` names the row before
  it, so the walk from the newest row follows that link back. The rows a
  compaction added (Claude's summary, rows our install wrote, the boundary
  itself, the `/compact` command turn) are left out.
- A response with several thinking blocks shares its thinking estimate among
  them by signature length; v1 charged each block the whole estimate.
- A tool call and its result always form one `tool` leaf, so a call is never
  kept without its result (Claude drops an unanswered call from the request).
- Token counts are estimates (about four characters a token).
"""
import json
from pathlib import Path
import re

from ....compaction.model import Leaf, Turn, estimate_tokens

# Rows that carry no conversation content.
SKIP_TYPES = frozenset({'custom-title', 'agent-name', 'permission-mode', 'file-history-snapshot', 'attachment',
                        'last-prompt', 'queue-operation', 'system', 'summary', 'mode', 'cost-state', 'atis-latch'})
# Command turns that only display something; their rows never reach a compacted conversation.
SKIP_COMMANDS = frozenset({'/context', '/model', '/effort', '/compact', '/cost', '/usage', '/status', '/statusline'})
COMMAND_NAME = re.compile(r'<command-name>(/[\w:-]+)</command-name>')
# Claude also stores the command as typed, a prompt of its own: "/compact" or "/compact keep the plan".
TYPED_COMMAND = re.compile(r'\s*(/[\w:-]+)(?:\s|$)')
# Tools whose results Claude itself may evict under pressure: their output does
# not earn a turn a bigger share of the budget (it is still summarized).
EVICTABLE = frozenset({'Read', 'Bash', 'Grep', 'Glob', 'WebSearch', 'WebFetch', 'Edit', 'Write', 'MultiEdit',
                       'NotebookEdit', 'BashOutput'})
IMAGE_TOKENS = 1600


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
    result, seen = [], set()
    while current in by_uuid and current not in seen:
        seen.add(current)
        row = by_uuid[current]
        result.append(row)
        current = row.get('logicalParentUuid') if is_boundary(row) else row.get('parentUuid')
    result.reverse()
    return result


def _text_blob(row) -> str:
    parts = []
    message = row.get('message')
    for value in (row.get('content'), message.get('content') if isinstance(message, dict) else None):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(block.get('text', '') or '' for block in value if isinstance(block, dict) and block.get('type') == 'text')
    return '\n'.join(parts)


def _command(row) -> str | None:
    """The display-only command a user row records, if any."""
    text = _text_blob(row)
    match = COMMAND_NAME.search(text) or TYPED_COMMAND.match(text) if text else None
    return match.group(1) if match and match.group(1) in SKIP_COMMANDS else None


def strip_command_turns(rows: list[dict]) -> list[dict]:
    """Drop the display-only command turns (`/context`, `/compact`, ...) from a chain.

    Commands are stored as the prompt typed (`/compact`), as user rows sharing
    one promptId (a caveat, the command name, its output), or as
    `system`/`local_command` rows; all of them go.
    """
    skip_prompts = {row['promptId'] for row in rows if row.get('type') == 'user' and row.get('promptId') and _command(row)}
    result, in_command = [], False
    for row in rows:
        if row.get('promptId') in skip_prompts and row.get('type') == 'user':
            continue
        if row.get('type') == 'system' and row.get('subtype') == 'local_command':
            match = COMMAND_NAME.search(row.get('content') or '') if isinstance(row.get('content'), str) else None
            if match:
                in_command = match.group(1) in SKIP_COMMANDS
            if in_command:
                continue
        elif in_command and row.get('type') == 'user' and row.get('isMeta'):
            continue
        elif row.get('type') != 'system':
            in_command = False
        result.append(row)
    return result


def _preview(text: str, limit: int = 120) -> str:
    flat = ' '.join((text or '').split())
    return flat if len(flat) <= limit else flat[:limit - 3] + '...'


def _short_args(value, limit=80) -> str:
    if isinstance(value, dict):
        for key in ('command', 'file_path', 'path', 'url', 'query', 'pattern', 'description'):
            if isinstance(value.get(key), str):
                text = value[key]
                return text if len(text) <= limit else text[:limit - 3] + '...'
        text = json.dumps(value, ensure_ascii=False)
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
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return Leaf('user_text', (row['uuid'],), estimate_tokens(prompt), _preview(prompt), {'text': prompt})


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
        return Leaf('user_text', (uuid,), estimate_tokens(content), _preview(content), {'text': content})
    if kind == 'user' and isinstance(content, list):
        images = [block for block in content if isinstance(block, dict) and block.get('type') == 'image']
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_result':
                result = block.get('content')
                if isinstance(result, list):
                    text = '\n'.join(part.get('text', '') for part in result if isinstance(part, dict) and part.get('type') == 'text')
                    images += [part for part in result if isinstance(part, dict) and part.get('type') == 'image']
                else:
                    text = '' if result is None else str(result)
                return Leaf('tool_result', (uuid,), estimate_tokens(text) + IMAGE_TOKENS * len(images), _preview(text),
                            {'id': block.get('tool_use_id'), 'text': text, 'is_error': bool(block.get('is_error'))})
            if block.get('type') == 'text':
                text = block.get('text', '') or ''
                return Leaf('user_text', (uuid,), estimate_tokens(text) + IMAGE_TOKENS * len(images), _preview(text),
                            {'text': text, 'images': len(images)})
        if images:
            return Leaf('user_text', (uuid,), IMAGE_TOKENS * len(images), f'<{len(images)} image(s)>',
                        {'text': '', 'images': len(images)})
        return None
    if kind == 'assistant' and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') in ('thinking', 'redacted_thinking'):
                text = block.get('thinking', '') or ''
                return Leaf('thinking', (uuid,), estimate_tokens(text), _preview(text) if text else '<thinking>',
                            {'message': message.get('id'), 'signature': len(block.get('signature') or block.get('data') or '')})
            if block.get('type') == 'text':
                text = block.get('text', '') or ''
                return Leaf('text', (uuid,), estimate_tokens(text), _preview(text), {'text': text})
            if block.get('type') == 'tool_use':
                name, arguments = block.get('name', '?'), block.get('input', {})
                rendered = f'{name}({_short_args(arguments)})'
                return Leaf('tool_use', (uuid,), estimate_tokens(name + json.dumps(arguments, ensure_ascii=False)), rendered,
                            {'id': block.get('id'), 'name': name, 'input': arguments})
    return None


def _visible_tokens(message) -> int:
    total = 0
    for block in message.get('content') or []:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'text':
            total += estimate_tokens(block.get('text', '') or '')
        elif block.get('type') == 'tool_use':
            total += estimate_tokens(block.get('name', '') + json.dumps(block.get('input', {}), ensure_ascii=False))
    return total


def thinking_estimates(rows) -> dict[str, int]:
    """Estimated tokens of each thinking row: its response's output tokens less the visible text.

    The estimate is per response; a response with several thinking blocks shares
    it among them by signature length (equally when no signature tells).
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
                blocks.setdefault(response, []).append((row['uuid'], weight))
    estimates = {}
    for response, entries in blocks.items():
        total = max(0, output.get(response, 0) - visible.get(response, 0))
        weights = sum(weight for _, weight in entries)
        for uuid, weight in entries:
            estimates[uuid] = total * weight // weights
    return estimates


def pair_tools(leaves: list[Leaf]) -> list[Leaf]:
    """Join each tool call with its result into one `tool` leaf at the call's place."""
    results = {leaf.content.get('id'): leaf for leaf in leaves if leaf.kind == 'tool_result' and leaf.content.get('id')}
    used, paired = set(), []
    for leaf in leaves:
        if leaf.kind == 'tool_use' and leaf.content.get('id') in results:
            result = results[leaf.content['id']]
            used.add(id(result))
            name, outcome = leaf.content['name'], result.content.get('text', '')
            summary = ('error: ' + _preview(outcome, 50)) if result.content.get('is_error') else _preview(outcome, 50)
            weight = leaf.tokens if name in EVICTABLE else leaf.tokens + result.tokens
            paired.append(Leaf('tool', (*leaf.refs, *result.refs), leaf.tokens + result.tokens,
                               _preview(f'{leaf.preview} -> {summary}'),
                               {'id': leaf.content['id'], 'name': name, 'input': leaf.content.get('input', {}),
                                'result': outcome, 'is_error': result.content.get('is_error', False)}, weight))
        elif leaf.kind == 'tool_result' and id(leaf) in used:
            continue
        else:
            paired.append(leaf)
    return paired


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
    compaction summaries carry `isCompactSummary`.
    """
    path = [row for row in chain(rows) if not is_boundary(row) and row['uuid'] not in added
            and not row.get('isCompactSummary')]
    path = strip_command_turns(path)
    estimates = thinking_estimates(path)
    result, current, orphans, last_prompt = [], None, [], None
    for row in path:
        leaf = classify(row)
        if leaf is not None and leaf.kind == 'thinking':
            leaf.tokens = max(leaf.tokens, estimates.get(row['uuid'], 0))
        if _starts_turn(row, last_prompt):
            if current is not None:
                result.append(current)
            last_prompt = row['promptId']
            current = Turn(row['promptId'], leaf, [])
            current.leaves.extend(orphans)
            orphans = []
            continue
        if leaf is None:
            continue
        if current is None:
            orphans.append(leaf)
        else:
            current.leaves.append(leaf)
    if current is not None:
        result.append(current)
    elif orphans:
        result.append(Turn('turn-0', None, orphans))
    for turn in result:
        turn.leaves = pair_tools(turn.leaves)
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
