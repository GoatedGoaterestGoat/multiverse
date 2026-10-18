"""Explicit interpreter and source boundaries for native callback fixtures."""
from pathlib import Path
import shlex
import sys


def mv2_launcher(directory):
    """Run this test's source with its interpreter, without checkout bin/venv discovery."""
    source = Path(__file__).resolve().parents[1] / 'src'
    code = f'import sys; sys.path.insert(0, {str(source)!r}); from multiverse.cli import main; main()'
    executable = Path(directory) / 'mv2 native fixture'
    executable.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-c', code]) + ' "$@"\n')
    executable.chmod(0o700)
    return str(executable)


def forbidden_python(directory):
    """Make accidental env-python3 callbacks fail instead of using the host PATH."""
    directory = Path(directory) / 'forbidden-python'
    directory.mkdir()
    executable = directory / 'python3'
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "Native fixture used PATH python3" >&2\nexit 73\n')
    executable.chmod(0o700)
    return directory
