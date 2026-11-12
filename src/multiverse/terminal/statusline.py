"""Tmux status renderer with a time-limited core inbox peek in a separate process.

The wrapper invokes this file with exact root, agent, launch and adapter-family
arguments; tmux's command environment need not belong to the displayed agent.
"""
from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import select
import signal
import subprocess
import sys
import time
import threading
import unicodedata


DEFAULTS = {
    'refresh_seconds': 2,
    'items': {'agent': True, 'harness': True, 'focus': True, 'status': False, 'phase': True,
              'summary': False, 'inbox': True, 'context': False},
    'harnesses': {'codex': {'items': {'status': True}},
                  'claude': {'items': {'phase': False, 'inbox': False}}},
}
INBOX_TIMEOUT = 1.0  # seconds, including the isolated shared-reader process
READ_LIMIT = 1024 * 1024
# Plugin items: what a launch declared (runtime.json `plugins.status_items`), each read from
# status-items/<name>.txt. Names follow the plugin manifest rule and never take a core item's name.
ITEM_NAME = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*')
ITEM_NAME_LENGTH = 40
ITEM_READ_LIMIT = 1024
FOCUS_LONGEST = 86400  # universe.focus.LONGEST: a later `until` is damage, not a focus
ITEM_LENGTH = 200
# Only the items this bar draws itself; a plugin's items are the plugin's.
CORE_ITEMS = frozenset(DEFAULTS['items'])
SUMMARY_LENGTH = 60
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))')


def object_value(value):
    return value if isinstance(value, dict) else {}


def _retry_missing(operation):
    """Standalone counterpart of store.retry_missing, only for replaced records."""
    deadline = time.monotonic() + .05
    for attempt in range(5):
        try:
            return operation()
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
            remaining = deadline - time.monotonic()
            if attempt == 4 or remaining <= 0:
                raise
            time.sleep(min(.005, remaining))


def missing_records(paths):
    """Observe optional absences together for this render, never cache identity or file contents."""
    def missing(candidates):
        pending = []
        for path in candidates:
            try:
                path.stat()
            except OSError as error:
                if error.errno == errno.ENOENT:
                    pending.append(path)
                # The reader still diagnoses permission errors and special files.
        return pending

    pending = missing(dict.fromkeys(Path(path) for path in paths))
    if not pending:
        return frozenset()
    deadline = time.monotonic() + .05
    for _ in range(1, 5):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(.005, remaining))
        pending = missing(pending)
        if not pending:
            break
    return frozenset(pending)


def read_object(path, *, observed_missing=()):
    if path in observed_missing:
        return {}
    try:
        # Non-blocking, so a FIFO in a record's place never hangs the bar. The descriptor is closed here:
        # wrapping a directory's would fail and leak it.
        descriptor = _retry_missing(lambda: os.open(path, os.O_RDONLY | os.O_NONBLOCK))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return {}
            with os.fdopen(descriptor, 'rb', closefd=False) as source:
                data = source.read(READ_LIMIT + 1)
        finally:
            os.close(descriptor)
        return object_value(json.loads(data)) if len(data) <= READ_LIMIT else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def config(data, family='', declared=()):
    """Resolve ordered tmux items independently of the Claude mod's items.

    `declared` is this launch's plugin items, [(name, shown by default)]: an
    item `tmux.items` names takes its place among core items, and the others
    follow the core items in declared order. Each is its default, then
    `tmux.items`, then the family override.
    """
    data = object_value(data)
    if type(data.get('schema', 1)) not in (int, float) or data.get('schema', 1) != 1:
        data = {}
    known = {name for name, _ in declared}
    block = object_value(data.get('tmux'))
    supplied = object_value(block.get('items'))
    items = {key: value for key, value in supplied.items()
             if (key in DEFAULTS['items'] or key in known) and isinstance(value, bool)}
    for key, value in DEFAULTS['items'].items():
        items.setdefault(key, value)
    for name, default in declared:
        items.setdefault(name, default)
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


def _read_unread(directory):
    """The helper's only inbox operation is the dashboard's non-consuming, nonblocking peek."""
    # Direct execution uses -S and has only terminal/ on sys.path. Load this installation's core,
    # not an editable installation or code selected by the displayed agent's environment.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from multiverse.universe import mailbox
    return len(mailbox.unread(directory, blocking=False))


def _exec_unread(directory):
    """Threaded callers use exec isolation; the standalone bar takes the cheaper fork path."""
    try:
        result = subprocess.run([sys.executable, '-B', '-S', str(Path(__file__).resolve()), '--inbox', str(directory)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=INBOX_TIMEOUT, check=True, text=True)
        value = result.stdout.strip()
        return int(value) if value.isascii() and value.isdecimal() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def unread(directory):
    """A private reader process bounds work without importing the CLI or starting another interpreter."""
    # Forking with another thread or an external child reaper cannot preserve this helper's ownership.
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        return None
    if threading.active_count() != 1:
        return _exec_unread(directory)
    reader = writer = child = None
    try:
        reader, writer = os.pipe()
        child = os.fork()
        if child == 0:
            try:
                os.close(reader)
                value = _read_unread(directory)
                os.write(writer, str(value).encode('ascii'))
            except BaseException:
                pass  # Unreadable, busy or damaged: no count, and no inbox text in diagnostics.
            finally:
                os._exit(0)
        os.close(writer)
        writer = None
        if not select.select([reader], [], [], INBOX_TIMEOUT)[0]:
            return None
        value = os.read(reader, 32).decode('ascii').strip()
        return int(value) if value.isascii() and value.isdecimal() else None
    except (OSError, ValueError):
        return None
    finally:
        try:
            if child:
                try:
                    ended, _ = os.waitpid(child, os.WNOHANG)
                except ChildProcessError:
                    ended = child  # No ownership proof: never signal a possibly reused PID.
                if not ended:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        os.waitpid(child, 0)
                    except ChildProcessError:
                        pass
        finally:
            for descriptor in (reader, writer):
                if descriptor is not None:
                    os.close(descriptor)


def focus_text(directory, *, observed_missing=()):
    """`focused`, `focused 18m` or `focused (turn)` while the agent's focus lasts.

    universe.focus owns the file; this follows its `active` rules for a record in effect.
    """
    record = read_object(directory / 'focus.json', observed_missing=observed_missing)
    def moment(value):
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    try:
        moment(record['since'])
        turns = record.get('turns')
        if record.get('by') not in ('agent', 'user') or turns is not None and (type(turns) is not int
                                                                               or turns not in (1, 2)):
            return ''
        until = record.get('until')
        if until is None:
            return 'focused (turn)' if turns else 'focused'
        left = (moment(until) - datetime.now(timezone.utc)).total_seconds()
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return ''
    if not 0 < left <= FOCUS_LONGEST:
        return ''
    minutes = math.ceil(left / 60)
    return f'focused {minutes}m' if minutes < 60 else f'focused {minutes // 60}h{minutes % 60:02d}m'


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


def declared_items(runtime):
    """[(name, shown by default)] of the plugin items this launch declared; a malformed record declares nothing."""
    record = object_value(object_value(runtime).get('plugins'))
    items = record.get('status_items')
    found = []
    for item in items if isinstance(items, list) else []:
        item = object_value(item)
        name = item.get('name')
        if (not isinstance(name, str) or len(name) > ITEM_NAME_LENGTH or not ITEM_NAME.fullmatch(name)
                or name in CORE_ITEMS or any(name == known for known, _ in found)):
            continue
        found.append((name, item.get('tmux') is True))
    return found


def plugin_item(directory, name, *, observed_missing=()):
    """The first line of a plugin item's file, bounded and cleaned; '' when missing, empty or unreadable."""
    path = directory / 'status-items' / f'{name}.txt'
    if path in observed_missing:
        return ''
    try:
        with _retry_missing(lambda: path.open('rb')) as source:
            data = source.read(ITEM_READ_LIMIT)
    except OSError:
        return ''
    lines = data.decode('utf-8', errors='replace').splitlines()
    return clean(lines[0] if lines else '', ITEM_LENGTH)


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
    declared = declared_items(runtime)
    # Optional files that have never been published share one retry delay, including all declared plugin items.
    # This snapshot belongs only to this render; required identity and runtime above always read fresh records.
    observed_missing = missing_records([
        root / 'statusline.json', directory / 'focus.json',
        *(directory / 'status-items' / f'{name}.txt' for name, _ in declared),
    ])
    items, _ = config(read_object(root / 'statusline.json', observed_missing=observed_missing), family, declared)
    plugins = {name for name, _ in declared}
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
        elif item == 'focus':
            text = focus_text(directory, observed_missing=observed_missing)
            if text:
                parts.append(text)
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
                parts.append(f'inbox {count}')
        elif item == 'context':
            text = context_text(properties, runtime, family)
            if text:
                parts.append(text)
        elif item in plugins:
            text = plugin_item(directory, item, observed_missing=observed_missing)
            if text:
                parts.append(text)
    # tmux interprets styles in job output; agent text must remain literal.
    return ' · '.join(parts).replace('#', '##')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--inbox':
        try:
            print(_read_unread(Path(sys.argv[2])))
        except Exception:
            pass  # No message bodies or filesystem diagnostics belong in tmux output.
        return
    if len(sys.argv) != 5:
        return
    print(render(*sys.argv[1:]))


if __name__ == '__main__':
    main()
