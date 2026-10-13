"""Compacted turns as the messages a `session.compact` hook returns.

Each turn becomes its prompt, then its frontier in order, every message built
from the transcript's content: prompts and messages from you as user text,
summaries and text as assistant text, and a tool exchange as an assistant tool
use followed at once by its user tool result. Nothing is kept by handle. Claude 2.1.287
stores a kept row again under a new id but with its old links: a kept tool
result keeps its `sourceToolAssistantUUID` and so hangs off the call from before
the compaction, and a kept response keeps its `message.id`, so loading the
session again (Claude's own resume) gathers the original rows with the copies.
Built rows get links and ids of their own.

Thinking is no part of compaction (see transcript.py). Images cannot be built:
a prompt notes how many it had, as v1 did for images the budget left out, and a
tool result's images are dropped, as in v1. Every built message holds exactly
one block, so each becomes exactly one stored row (the install record counts on
it).

A call and its result normally form one leaf, so a merge never separates them.
A call or result left unpaired (a session cut off mid-call) is handled as in
Multiverse v1's renderer (render_frontier_as_messages): a call whose result is
missing is answered with "(result summarized)", and a result whose call is
missing gets a `summarized_tool` call before it; every call is answered before
anything else follows it.
"""
import re

from ...engine.model import Leaf, Node

PLACEHOLDER_TOOL = 'summarized_tool'
PLACEHOLDER_RESULT = '(result summarized)'
# Tool names the API accepts.
TOOL_NAME = re.compile(r'[^a-zA-Z0-9_-]')


def _tool_name(name) -> str:
    return TOOL_NAME.sub('_', str(name or '')).strip('_') or 'tool'


def _images_note(count: int) -> str:
    return f'_[{count} image(s) omitted to fit compaction budget]_'


def render(turns, frontiers) -> dict:
    """{'messages', 'sources', 'count', 'built', 'dropped'} for the hook and the install record."""
    messages = []
    counts = {'dropped': 0}
    pending: list[str] = []  # calls delivered and not yet answered

    def build(role, text='', uses=(), results=None):
        message = {'role': role, 'text': text, 'toolUses': list(uses)}
        if results is not None:
            message['toolResults'] = results
        messages.append(message)

    def user_text(value, images, empty=None):
        if images:
            value = f'{value}\n\n{_images_note(images)}' if value else _images_note(images)
        if value or empty:
            build('user', value or empty)
        else:
            counts['dropped'] += 1

    def call(tool_use_id, name, arguments):
        build('assistant', uses=[{'tool_use_id': tool_use_id, 'tool': name, 'input': arguments}])

    def result(tool_use_id, value, error=False):
        build('user', results=[{'tool_use_id': tool_use_id, 'text': value, 'isError': error}])

    def answer(orphans_only=True):
        """Answer pending calls whose result is not coming (or, at the end, every one) with v1's placeholder."""
        for tool_use_id in list(pending):
            if not orphans_only or tool_use_id not in coming:
                pending.remove(tool_use_id)
                result(tool_use_id, PLACEHOLDER_RESULT)

    def deliver(entry):
        result(entry.content.get('tool_use_id'), entry.content.get('text', '') or '', bool(entry.content.get('is_error')))

    def content(entry):
        if isinstance(entry, Leaf) and entry.kind == 'tool_exchange':
            call(entry.content.get('tool_use_id'), _tool_name(entry.content.get('name')), entry.content.get('input') or {})
            result(entry.content.get('tool_use_id'), entry.content.get('result_text', '') or '',
                   bool(entry.content.get('is_error')))
        elif isinstance(entry, Leaf) and entry.kind == 'tool_result':  # its call was summarized away
            call(entry.content.get('tool_use_id'), PLACEHOLDER_TOOL, {})
            deliver(entry)
        elif isinstance(entry, Node):
            build('assistant', entry.summary.strip())
        elif entry.kind == 'asst_text' and entry.content.get('text', '').strip():
            build('assistant', entry.content['text'])
        elif entry.kind == 'user_text':
            user_text(entry.content.get('text', '') or '', entry.content.get('images', 0))
        else:
            counts['dropped'] += 1  # empty text, or a kind a built message cannot hold

    for turn in turns:
        if turn.prompt is not None:
            user_text(turn.prompt.content.get('text', '') or '', turn.prompt.content.get('images', 0),
                      '_(no user prompt for this turn)_')
        frontier = frontiers[turn.id]
        # Results still raw on this frontier, not yet delivered.
        coming = {entry.content.get('tool_use_id') for entry in frontier
                  if isinstance(entry, Leaf) and entry.kind == 'tool_result'}
        held = []  # content waiting until this batch's raw results have followed their calls
        replied = False  # a result was delivered since the last call

        def waiting():
            return any(tool_use_id in coming for tool_use_id in pending)

        for entry in frontier:
            kind = entry.kind if isinstance(entry, Leaf) else None
            tool_use_id = entry.content.get('tool_use_id') if kind in ('tool_use', 'tool_result') else None
            if kind == 'tool_use':
                if replied:  # a new response: answer what the last one left open
                    answer()
                    replied = False
                call(tool_use_id, _tool_name(entry.content.get('name')), entry.content.get('input') or {})
                pending.append(tool_use_id)
            elif kind == 'tool_result' and tool_use_id in pending:
                pending.remove(tool_use_id)
                coming.discard(tool_use_id)
                deliver(entry)
                replied = True
                if not waiting():
                    answer()
                    for later in held:
                        content(later)
                    held = []
            else:
                if kind == 'tool_result':
                    coming.discard(tool_use_id)
                if waiting():
                    held.append(entry)
                else:
                    answer()
                    content(entry)
        answer(orphans_only=False)
        for later in held:
            content(later)
    return {'messages': messages, 'sources': [{'stands_for': None}] * len(messages), 'count': len(messages),
            'built': len(messages), **counts}
