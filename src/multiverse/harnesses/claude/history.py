"""Read only exact Claude transcripts. Absence, unreadability and history differ."""
import json
import os
from pathlib import Path
import re
import stat


def environment(source):
    home = Path(source.get('HOME') or os.environ.get('HOME') or Path.home()).expanduser().resolve()
    storage = Path(source.get('CLAUDE_CONFIG_DIR') or (os.environ.get('CLAUDE_CONFIG_DIR') if not source else None) or home / '.claude').expanduser()
    if not storage.is_absolute():
        raise ValueError('Claude storage must be an absolute path')
    return {'HOME': str(home), 'CLAUDE_CONFIG_DIR': str(storage.resolve())}


def location(env, workspace, session_id):
    project = re.sub(r'[^a-zA-Z0-9]', '-', workspace)
    return Path(env['CLAUDE_CONFIG_DIR']) / 'projects' / project / (session_id + '.jsonl')


def fingerprint(env):
    """Detect removed/replaced storage; never mistake that for an unused session."""
    try:
        paths = [Path(env['CLAUDE_CONFIG_DIR']), Path(env['CLAUDE_CONFIG_DIR']) / 'projects']
        return [[p.stat().st_dev, p.stat().st_ino] for p in paths]
    except OSError:
        return None


def discover(env, workspace, session_id):
    """Bounded scan; no latest-session selection and no transcript text returned."""
    target = location(env, workspace, session_id)
    try:
        # Enumerate rather than glob: glob hides permission and I/O failures.
        projects = target.parent.parent
        present = False
        with os.scandir(projects) as entries:
            for index, entry in enumerate(entries):
                if index >= 10000:
                    return 'unknown'
                if entry.is_symlink():
                    return 'unknown'
                if not entry.is_dir():
                    continue
                candidate = Path(entry.path) / target.name
                try:
                    candidate.lstat()
                except FileNotFoundError:
                    continue
                if candidate.is_symlink():
                    return 'unknown'
                if candidate != target:
                    return 'unknown'  # exact UUID in another workspace, never follow it
                present = True
        try:
            descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            stream = os.fdopen(descriptor, 'rb')
        except FileNotFoundError:
            return 'unknown' if present else 'absent'
        with stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return 'unknown'
            scanned = 0
            for _ in range(256):
                line = stream.readline(1024 * 1024 + 1)
                if not line:
                    break
                scanned += len(line)
                if len(line) > 1024 * 1024 or scanned > 4 * 1024 * 1024:
                    return 'unknown'
                record = json.loads(line)
                if record.get('type') in ('user', 'assistant'):
                    if record.get('sessionId') != session_id or record.get('cwd') != workspace:
                        return 'unknown'
                    if record.get('message'):
                        return 'saved'
        return 'unknown'  # empty/metadata-only/corrupt files are not proof of absence
    except (OSError, ValueError, TypeError, AttributeError):
        return 'unknown'
