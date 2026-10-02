"""Fail before a test can start, stop or connect to a developer's Codex daemon."""
import os
import json
from pathlib import Path
import socket
import sys


# Capture the caller's native home before a fixture changes its environment.
PROTECTED_HOMES = {(Path.home() / '.codex').resolve()}
if os.environ.get('CODEX_HOME'):
    PROTECTED_HOMES.add(Path(os.environ['CODEX_HOME']).expanduser().resolve())
_installed = False


def isolated_home(path):
    """Keep disposable daemons from spawning updater processes that outlive tests."""
    path = Path(path).resolve()
    if any(path == protected or protected in path.parents for protected in PROTECTED_HOMES):
        raise AssertionError('Cannot configure the default Codex daemon from a test')
    settings = path / 'app-server-daemon/settings.json'
    settings.parent.mkdir(parents=True, exist_ok=True)
    value = json.loads(settings.read_text()) if settings.exists() else {}
    value['updater'] = {**value.get('updater', {}), 'autoUpdateEnabled': False}
    settings.write_text(json.dumps(value) + '\n')
    return path


def check_spawn(argv, environment):
    if not isinstance(argv, (list, tuple)) or not argv:
        return
    args = [os.fsdecode(value) for value in argv]
    is_codex = Path(args[0]).name == 'codex'
    daemon_command = args[1:3] == ['app-server', 'daemon']
    if not is_codex and not daemon_command:
        return
    if args[1:] in (['--help'], ['-h'], ['--version'], ['-V']):
        return
    if args[1:3] in (['app-server', 'generate-json-schema'], ['app-server', 'generate-ts']):
        return
    env = os.environ if environment is None else environment
    home = Path(env.get('CODEX_HOME', Path.home() / '.codex')).expanduser().resolve()
    if any(home == protected or protected in home.parents for protected in PROTECTED_HOMES):
        raise AssertionError('Test attempted to access the default Codex daemon; use an isolated CODEX_HOME')
    try:
        settings = json.loads((home / 'app-server-daemon/settings.json').read_text())
    except (OSError, ValueError):
        settings = {}
    if settings.get('updater', {}).get('autoUpdateEnabled') is not False:
        raise AssertionError('Native tests must disable daemon automatic updates in their isolated CODEX_HOME')


def check_connect(address):
    if not isinstance(address, (str, bytes)):
        return
    path = Path(os.fsdecode(address)).expanduser().resolve()
    for home in PROTECTED_HOMES:
        controls = home / 'app-server-control', home / 'app-server-daemon'
        if any(path == control or control in path.parents for control in controls):
            raise AssertionError('Test attempted to connect to the default Codex daemon')
        # Codex redirects its long home path to a short Unix socket in /tmp.
        if path == (home / 'app-server-control/app-server-control.sock').resolve():
            raise AssertionError('Test attempted to connect to the default Codex daemon')


def install():
    global _installed
    if _installed:
        return
    def audit(event, args):
        if event == 'subprocess.Popen':
            check_spawn(args[1], args[3])
        elif event == 'socket.connect' and args[0].family == socket.AF_UNIX:
            check_connect(args[1])
    sys.addaudithook(audit)
    _installed = True
