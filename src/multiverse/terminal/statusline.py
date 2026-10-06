"""Read-only tmux status renderer. Direct execution imports only the standard library.

The wrapper invokes this file with exact root, agent, launch and adapter-family
arguments; tmux's command environment need not belong to the displayed agent.
"""
import json
import math
from pathlib import Path
import re
import sys
import unicodedata


DEFAULTS = {
    'refresh_seconds': 2,
    'items': {'agent': True, 'harness': True, 'status': False, 'phase': True,
              'summary': False, 'inbox': True, 'context': False},
    'harnesses': {'codex': {'items': {'status': True}},
                  'claude': {'items': {'phase': False, 'inbox': False}}},
}
INBOX_LIMIT = 50
READ_LIMIT = 1024 * 1024
SUMMARY_LENGTH = 60
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))')


def object_value(value):
    return value if isinstance(value, dict) else {}


def read_object(path):
    try:
        with Path(path).open('rb') as source:
            data = source.read(READ_LIMIT + 1)
        return object_value(json.loads(data)) if len(data) <= READ_LIMIT else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def config(data, family=''):
    """Resolve ordered tmux items independently of the Claude mod's items."""
    data = object_value(data)
    if type(data.get('schema', 1)) not in (int, float) or data.get('schema', 1) != 1:
        data = {}
    block = object_value(data.get('tmux'))
    supplied = object_value(block.get('items'))
    items = {key: value for key, value in supplied.items()
             if key in DEFAULTS['items'] and isinstance(value, bool)}
    for key, value in DEFAULTS['items'].items():
        items.setdefault(key, value)
    items.update(DEFAULTS['harnesses'].get(family, {}).get('items', {}))
    override = object_value(object_value(block.get('harnesses')).get(family))
    for key, value in object_value(override.get('items')).items():
        if key in items and isinstance(value, bool):
            items[key] = value
    interval = block.get('refresh_seconds', DEFAULTS['refresh_seconds'])
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or isinstance(interval, float) and not math.isfinite(interval)):
        interval = DEFAULTS['refresh_seconds']
    return items, max(1, min(60, math.ceil(interval)))


def clean(value, limit=80):
    if not isinstance(value, str):
        return ''
    value = ANSI.sub('', value)
    value = ''.join(' ' if unicodedata.category(char) in ('Cc', 'Cf', 'Zl', 'Zp') else char
                    for char in value)
    value = ' '.join(value.split())
    if len(value) <= limit:
        return value
    cut = value[:limit - 1]
    if not value[limit - 1].isspace() and ' ' in cut:
        cut = cut[:cut.rindex(' ')]
    return cut.rstrip(' ,;:.-') + '…'


def unread(directory):
    """Bounded observation of unread manual messages; never locks or advances a cursor.

    Native-delivered messages do not count. Incomplete append tails are ignored,
    like store.inbox. Unknown/corrupt/over-budget counts are None, never zero.
    """
    try:
        try:
            with (directory / 'inbox.cursor').open() as source:
                cursor = int(source.read(64))
        except FileNotFoundError:
            cursor = 0
        if cursor < 0:
            return None
        try:
            source = (directory / 'inbox.jsonl').open('rb')
        except FileNotFoundError:
            return 0 if cursor == 0 else None
        with source:
            if cursor > source.seek(0, 2):
                return None
            if cursor:
                source.seek(cursor - 1)
                if source.read(1) != b'\n':
                    return None
            source.seek(cursor)
            data = source.read(READ_LIMIT + 1)
        count = 0
        for line in data[:READ_LIMIT].splitlines(keepends=True):
            if not line.endswith(b'\n'):
                break
            message = json.loads(line)
            if not isinstance(message, dict):
                return None
            if message.get('schema') != 2 or message['delivery'] == 'inbox-only':
                count += 1
                if count >= INBOX_LIMIT:
                    return INBOX_LIMIT
        return count if len(data) <= READ_LIMIT else None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 2**63 - 1 and math.isfinite(value))


def tokens(value):
    if value >= 1000000:
        return f'{value / 1000000:.1f}m'
    if value >= 10000:
        return f'{round(value / 1000)}k'
    return f'{value / 1000:.1f}k' if value >= 1000 else str(int(value))


def context_text(properties, runtime, family):
    if (runtime.get('state') != 'running' or 'exit_code' in runtime
            or object_value(runtime.get('native_relay')).get('state') == 'lost'):
        return ''
    usage = object_value(runtime.get('usage'))
    if family == 'codex' and (not properties.get('native_session_id')
            or usage.get('native_session_id') != properties['native_session_id']
            or usage.get('source') != 'codex-app-server'):
        return ''
    context = object_value(usage.get('context'))
    used, window = context.get('tokens'), context.get('window')
    if not number(used):
        return ''
    return 'ctx ' + tokens(used) + (f'/{tokens(window)}' if number(window) and window else '')


def render(root, agent_id, launch_id, family=''):
    root = Path(root)
    # A fixed directory, never name resolution or a scan of the universe.
    if not agent_id or Path(agent_id).name != agent_id or agent_id in ('.', '..'):
        return ''
    directory = root / 'state' / 'agents' / agent_id
    properties = read_object(directory / 'properties.json')
    runtime = read_object(directory / 'runtime.json')
    meta = read_object(root / 'universe.json')
    if (properties.get('id') != agent_id or runtime.get('launch_id') != launch_id
            or not launch_id or not meta.get('id') or properties.get('universe_id') != meta['id']):
        return ''
    items, _ = config(read_object(root / 'statusline.json'), family)
    phase = clean(properties.get('phase'), 24)
    summary = clean(properties.get('summary'), SUMMARY_LENGTH)
    status = runtime.get('status') if runtime.get('state') == 'running' and 'exit_code' not in runtime else None
    if (runtime.get('state') == 'running' and 'exit_code' not in runtime
            and object_value(runtime.get('native_relay')).get('state') == 'lost'):
        status = 'disconnected'
    if status not in ('working', 'waiting', 'idle', 'disconnected'):
        status = None
    parts, joined = [], False
    for item, enabled in items.items():
        if not enabled:
            continue
        if item == 'agent':
            name = clean(properties.get('name'))
            scope = clean(meta.get('name')) if root.parent.name == 'subverses' else ''
            if name:
                parts.append(f'{name} ({scope})' if scope else name)
        elif item == 'harness':
            name = clean(properties.get('harness'), 40)
            if name:
                parts.append(name)
        elif item == 'status':
            if status and not (items['phase'] and phase == status):
                parts.append(f'status {status}')
        elif item in ('phase', 'summary'):
            if joined:
                continue
            joined = True
            left, right = f'phase {phase}' if items['phase'] and phase else '', summary if items['summary'] else ''
            text = f'{left}: {right}' if left and right else left or right
            if text:
                parts.append(text)
        elif item == 'inbox':
            count = unread(directory)
            if count is None:
                parts.append('inbox ?')
            elif count:
                parts.append(f'inbox {count}+' if count >= INBOX_LIMIT else f'inbox {count}')
        elif item == 'context':
            text = context_text(properties, runtime, family)
            if text:
                parts.append(text)
    # tmux interprets styles in job output; agent text must remain literal.
    return ' · '.join(parts).replace('#', '##')


def main():
    if len(sys.argv) != 5:
        return
    print(render(*sys.argv[1:]))


if __name__ == '__main__':
    main()
