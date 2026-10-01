"""Compacted turns as the messages a `session.compact` hook returns.

Each turn becomes its prompt, then its frontier in order. A row Claude offered
the hook (it has a handle) is kept whole: thinking with its signature, images,
tool calls and results exactly as stored. Anything else is built: summaries
and text as plain messages, a tool call as an assistant tool use followed by a
user tool result. Every built message holds exactly one block, so each message
becomes exactly one stored row (the install record counts on it).

What cannot be built is left out: a thinking block whose row is no longer
offered (it belongs to a conversation before an earlier compaction) and an
unanswered tool call or orphaned result, which Claude would drop anyway.
"""
from ....compaction.model import Leaf, Node


def render(turns, frontiers, offered, copies: dict[str, list[str]]) -> dict:
    """{'messages', 'sources', 'count', 'kept', 'built', 'dropped'} for the hook and the install record.

    `offered` is the hook's list of {handle, role}; `copies` maps an original
    row to the rows earlier installs stored for it (Claude stores a kept row
    again under a new id), newest last.
    """
    handles = {item['handle'] for item in offered or [] if isinstance(item, dict) and isinstance(item.get('handle'), str)}
    messages, sources = [], []
    counts = {'kept': 0, 'built': 0, 'dropped': 0}

    def current(ref):
        for candidate in [*reversed(copies.get(ref, [])), ref]:
            if candidate in handles:
                return candidate
        return None

    def keep(leaf) -> bool:
        found = [current(ref) for ref in leaf.refs]
        if not leaf.refs or None in found:
            return False
        for ref, handle in zip(leaf.refs, found):
            messages.append({'keep': handle})
            sources.append({'stands_for': ref})
            counts['kept'] += 1
        return True

    def build(message):
        messages.append(message)
        sources.append({'stands_for': None})
        counts['built'] += 1

    def text(role, value):
        build({'role': role, 'text': value, 'toolUses': []})

    for turn in turns:
        if turn.prompt is not None and not keep(turn.prompt):
            prompt = turn.prompt.content.get('text', '')
            if prompt or turn.prompt.content.get('images'):
                text('user', prompt or '(an image was here)')
        for entry in frontiers[turn.id]:
            if isinstance(entry, Node):
                text('assistant', entry.summary)
                continue
            if not isinstance(entry, Leaf) or keep(entry):
                continue
            content = entry.content
            if entry.kind == 'text':
                text('assistant', content.get('text', ''))
            elif entry.kind == 'user_text':
                text('user', content.get('text', '') or '(an image was here)')
            elif entry.kind == 'tool':
                build({'role': 'assistant', 'text': '', 'toolUses': [
                    {'tool_use_id': content['id'], 'tool': content['name'], 'input': content.get('input', {}),
                     'text': content.get('result', '')}]})
                build({'role': 'user', 'text': '', 'toolUses': [], 'toolResults': [
                    {'tool_use_id': content['id'], 'text': content.get('result', ''),
                     'isError': bool(content.get('is_error'))}]})
            else:
                counts['dropped'] += 1  # thinking no longer offered, unanswered call, orphaned result
    return {'messages': messages, 'sources': sources, 'count': len(messages), **counts}
