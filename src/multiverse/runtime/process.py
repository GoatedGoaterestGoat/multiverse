"""Local process identity, independent of a harness or its metadata files."""
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime


START = re.compile(r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) '
                   r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) '
                   r'(?:[1-9]|[12][0-9]|3[01]) (?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9] [0-9]{4}')
LINUX_START = re.compile(r'linux:([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}):([0-9]+)')


def valid_start(value):
    if not isinstance(value, str):
        return False
    if LINUX_START.fullmatch(value):
        return True
    if not START.fullmatch(value):
        return False
    try:
        _, month, day, clock, year = value.split()
        datetime(int(year), 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split().index(month) + 1,
                 int(day), *map(int, clock.split(':')))
        return True
    except ValueError:
        return False


def _linux_snapshot(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text[text.rfind(')') + 2:].split() if ')' in text else []
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        token = f'linux:{boot}:{fields[19]}'
        return (token, fields[0]) if LINUX_START.fullmatch(token) and len(fields[0]) == 1 and fields[0].isalpha() else None
    except (OSError, ValueError, IndexError):
        return None


def boot_time():
    """The current boot's wall-clock boundary, only for historical records."""
    try:
        if sys.platform.startswith('linux'):
            for line in Path('/proc/stat').read_text().splitlines():
                if line.startswith('btime '):
                    return float(line.split()[1])
        elif sys.platform == 'darwin':
            result = subprocess.run(['sysctl', '-n', 'kern.boottime'], capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=2)
            if result.returncode == 0 and (match := re.search(r'sec = ([0-9]+)', result.stdout)):
                return float(match[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return None


def start_matches(recorded, pid, *, observed=None):
    """Positive identity proof, including old Linux ctime records; unknown is false."""
    if not valid_start(recorded):
        return False
    observed = process_started(pid) if observed is None else observed
    if observed == recorded:
        return True
    if (sys.platform.startswith('linux') and START.fullmatch(recorded)
            and isinstance(observed, str) and LINUX_START.fullmatch(observed)):
        return _query(pid, 'lstart=') == recorded
    return False


def process_started(pid):
    """Boot-scoped Linux start ticks, or macOS UTC ctime; unknown is never guessed.

    The runner samples its unreaped child immediately after Popen. Adapters
    compare that saved identity with both the current process and native files.
    execve preserves it; a later process reusing the PID does not.
    """
    if type(pid) is not int or pid <= 0:
        return None
    if sys.platform.startswith('linux'):
        snapshot = _linux_snapshot(pid)
        return snapshot[0] if snapshot else None
    value = _query(pid, 'lstart=')
    return value if value is not None and START.fullmatch(value) else None


def process_snapshot(pid):
    """Start token and process state from one observation; zombies have exited before reaping."""
    if type(pid) is not int or pid <= 0:
        return None
    if sys.platform.startswith('linux'):
        return _linux_snapshot(pid)
    value = _query(pid, 'lstart=,stat=')
    if value is None or ' ' not in value:
        return None
    started, state = value.rsplit(' ', 1)
    return (started, state) if START.fullmatch(started) and state and state[0].isalpha() else None


def process_command(pid):
    """Executable name for a blocked-cleanup diagnostic; no arguments or environment."""
    return _query(pid, 'comm=')


def _query(pid, fields):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', fields], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, env={**os.environ, 'TZ': 'UTC', 'LC_ALL': 'C'}, timeout=2)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    if not isinstance(result.stdout, str):
        return None
    value = ' '.join(result.stdout.split())
    return value if result.returncode == 0 else None
