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
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True,
                                env={**os.environ, 'TZ': 'UTC', 'LC_ALL': 'C'}, timeout=2)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    if not isinstance(result.stdout, str):
        return None
    value = ' '.join(result.stdout.split())
    return value if result.returncode == 0 and START.fullmatch(value) else None
