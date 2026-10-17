"""Local process identity, independent of a harness or its metadata files."""
import os
import re
import subprocess


START = re.compile(r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) '
                   r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) '
                   r'(?:[1-9]|[12][0-9]|3[01]) (?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9] [0-9]{4}')


def process_started(pid):
    """A PID's UTC start in normalized ctime form, or unknown; never a guess.

    The runner samples its unreaped child immediately after Popen. Adapters
    compare that saved identity with both the current process and native files.
    execve preserves it; a later process reusing the PID does not.
    """
    value = _query(pid, 'lstart=')
    return value if value is not None and START.fullmatch(value) else None


def process_snapshot(pid):
    """UTC start and process state from one observation; zombies have exited even before reaping."""
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
