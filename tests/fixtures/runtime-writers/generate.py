"""Capture actual lifecycle/runner writers with private executables and IPC doubles.

Invoked by the tests/run.py matrix driver; never uses a real provider or home.
"""
import contextlib
import json
import inspect
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

source, output = map(Path, sys.argv[1:3])
sys.path.insert(0, str(source / 'src'))
from multiverse import harnesses
from multiverse.harnesses.types import NativeEndpoint, NativeResult
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write
from multiverse.terminal.tmux import Tmux
from multiverse.universe import agents, templates


def until(fn, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('matrix timed out')


RUNNER = r'''
import sys, os, threading
from pathlib import Path
sys.path.insert(0, SOURCE)
from multiverse.runtime import runner, rpc, recovery
from multiverse.store import read
class Observer:
    def __init__(self, *args, **kwargs): pass
    def start(self): pass
    def close(self): pass
class Proxy:
    def __init__(self, settings, protocol, bound, *args, **kwargs):
        self.protocol, self.bound, self.settings = protocol, bound, settings
    def start(self):
        launch = Path(sys.argv[1])
        def bind():
            props = read(launch.parent.parent / 'properties.json')
            self.bound(props.get('native_session_id') or props['id'])
        self.stop = threading.Event()
        self.thread = None
        if not (launch.parent.parent / 'hold-binding').exists():
            bind()
        else:
            def pending():
                while not self.stop.wait(.05):
                    if (launch.parent.parent / 'release-binding').exists():
                        bind()
                        return
            self.thread = threading.Thread(target=pending, daemon=True)
            self.thread.start()
    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(1)
rpc.LaunchProxy = Proxy
runner.StartupDelivery = Observer
runner.StatusWatcher = Observer
recovery.RecoveryMonitor = Observer
try:
    from multiverse.runtime import queue_continuation
    queue_continuation.QueueContinuation = Observer
except ImportError: pass
sys.exit(runner.main())
'''

CHILD = r'''
import json, os, sys, time
from pathlib import Path
if '--version' in sys.argv:
    print(VERSION)
    raise SystemExit
sys.path.insert(0, SOURCE)
from multiverse.store import read
from multiverse.universe import agents
directory = Path(os.environ['MV2_AGENT_DIR'])
props = read(directory / 'properties.json')
if FAMILY == 'codex' and '--remote' not in sys.argv:
    agents.bind_session(Path(os.environ['MV2_UNIVERSE']), directory.name,
                        props.get('native_session_id') or props['id'], os.environ['MV2_LAUNCH_ID'])
if FAMILY == 'pi':
    target = Path(sys.argv[sys.argv.index('--session') + 1])
    target.write_text(json.dumps({'type': 'session', 'id': props['id']}) + '\n')
(directory / 'child-seen').write_text('running')
print('PRIVATE WRITER STAND-IN', flush=True)
mode = directory / 'child-mode'
if mode.exists() and mode.read_text().strip().startswith('exit'):
    raise SystemExit(int(mode.read_text().split(':')[1]))
while not (directory / 'exit-now').exists(): time.sleep(.05)
raise SystemExit(0)
'''

output.mkdir(parents=True, exist_ok=True)
commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
records = []
work = Path(tempfile.mkdtemp(prefix='mv2-writer-', dir='/tmp'))
socket = str(work / 'tmux.sock')
root = work / '.multiverse'
env = {key: value for key, value in os.environ.items()
       if not key.startswith(('MV2_', 'CODEX_', 'CLAUDE_')) and key not in ('CLAUDECODE',)}
env.update(HOME=str(work / 'home'), CODEX_HOME=str(work / 'codex'),
           CLAUDE_CONFIG_DIR=str(work / 'claude'), XDG_CONFIG_HOME=str(work / 'config'),
           MV2_TMUX_SOCKET=socket, PYTHONPATH=str(source / 'src'))
for key in ('HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'XDG_CONFIG_HOME'):
    Path(env[key]).mkdir()
runner_path = work / 'runner.py'
runner_path.write_text(RUNNER.replace('SOURCE', repr(str(source / 'src'))))
cli = work / 'mv2'
import shlex
cli.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-c',
    f'import sys; sys.path.insert(0, {str(source / "src")!r}); from multiverse.cli import main; main()']) + ' "$@"\n')
cli.chmod(0o700)


def snapshot(profile, label, directory, **extra):
    props, facts = read(directory / 'properties.json'), read(directory / 'runtime.json')
    launch = directory / 'launches' / facts['launch_id']
    sidecars = {str(path.relative_to(launch)): read(path)
                for path in launch.rglob('*.json')}
    record = dict(profile=profile, label=label, directory=str(directory),
                  properties=props, runtime=facts, sidecars=sidecars,
                  ready=(launch / 'ready').exists(), **extra)
    records.append(record)
    write(output / 'records.json', records)
    return record


original_create = Tmux.create
profile = ''
failure = None
child_mode = 'hold'
hold_binding = False


def create(backend, name, cwd, argv):
    directory = Path(argv[-1]).parent.parent
    if child_mode != 'hold':
        (directory / 'child-mode').write_text(child_mode)
    if hold_binding:
        (directory / 'hold-binding').touch()
    snapshot(profile, 'terminal-free-preparing', directory)
    if failure == 'before-terminal':
        raise RuntimeError('matrix injected terminal creation failure')
    return original_create(backend, name, cwd, [sys.executable, str(runner_path), argv[-1]])


original_status = Tmux.configure_status
def configure(backend, terminal, *args, **kwargs):
    if failure == 'after-terminal':
        raise RuntimeError('matrix injected post-terminal pre-request failure')
    return original_status(backend, terminal, *args, **kwargs)


def endpoint(*args, **kwargs):
    return NativeEndpoint('unix-websocket', str(work / 'native-standin.sock'))


def native_settings(*args, **kwargs):
    return NativeResult('ok', value={'exclude': [], 'writable_roots': [],
                                     'workspace_policy': {}, 'daemon_version': '0.163.0'})


def kill(name):
    options = {'attachment_only': True} if 'attachment_only' in inspect.signature(lifecycle.kill).parameters else {}
    return lifecycle.kill(root, name, **options)


try:
    with patch.dict(os.environ, env, clear=True), \
         patch.object(lifecycle, '_cli_path', return_value=str(cli)), \
         patch.object(Tmux, 'create', create), patch.object(Tmux, 'configure_status', configure), \
         patch.object(native, 'start_shared_service', endpoint), \
         patch.object(native.NativeExecutor, 'rpc', native_settings):
        templates.create(root, 'writer-matrix', workdir=work)
        profiles = [('codex', 'plain'), ('codex', 'bridge'), ('codex', 'fallback'),
                    ('claude', 'plain'), ('claude', 'bridge'), ('claude', 'fallback'),
                    ('pi', 'plain'), ('pi', 'bridge'), ('custom', 'plain')]
        for family, mode in profiles:
            profile = f'{family}-{mode}'
            harness_dir = root / 'harnesses' / profile
            harness_dir.mkdir()
            executable = harness_dir / 'standin'
            version = '2.1.279' if family == 'claude' and mode == 'fallback' else (
                '2.1.302' if family == 'claude' else '0.1.0')
            executable.write_text(f'#!{sys.executable}\n' + CHILD.replace('SOURCE', repr(str(source / 'src')))
                                  .replace('VERSION', repr(version)).replace('FAMILY', repr(family)))
            executable.chmod(0o700)
            if family == 'custom':
                spec = {'schema': 1, 'spawn': [str(executable)], 'resume': [str(executable)], 'session': 'assigned'}
            else:
                spec = {'schema': 2, 'adapter': family, 'settings': {'executable': str(executable),
                        'inbox_bridge': False if mode == 'plain' else True if mode == 'bridge' else None}}
                if mode == 'fallback':
                    del spec['settings']['inbox_bridge']
                if family == 'codex' and mode == 'fallback':
                    spec['settings']['args'] = ['--no-daemon']
            write(harness_dir / 'harness.json', spec)
            for failure_stage in ('before-terminal', 'after-terminal'):
                failure = failure_stage
                name = profile + '-' + failure_stage
                try:
                    lifecycle.start(root, name, '', profile, socket_path=socket)
                except RuntimeError as error:
                    assert 'matrix injected' in str(error), error
                else:
                    raise AssertionError('injection did not fail')
                directory = agents.directory(root, name)
                snapshot(profile, 'failed-' + failure_stage, directory)
            failure = None
            for exit_mode in ('exit:0', 'exit:3', 'hold'):
                child_mode = exit_mode
                hold_binding = family == 'codex' and mode == 'bridge' and exit_mode == 'hold'
                name = profile + '-' + exit_mode.replace(':', '-')
                value = lifecycle.start(root, name, '', profile, socket_path=socket)
                directory = root / 'state' / 'agents' / value['id']
                until(lambda: (directory / 'child-seen').exists())
                if exit_mode != 'hold':
                    until(lambda: 'exit_code' in read(directory / 'runtime.json'))
                    snapshot(profile, 'stopped' if exit_mode == 'exit:0' else 'failed-child', directory)
                else:
                    until(lambda: read(directory / 'runtime.json').get('startup_checked'))
                    snapshot(profile, 'bridge-starting' if hold_binding else 'running', directory)
                    try:
                        lifecycle.start(root, resume=name, socket_path=socket)
                    except ValueError as error:
                        snapshot(profile, 'live-resume-refused', directory, outcome=str(error))
                    else:
                        raise AssertionError('live agent resumed')
                    if hold_binding:
                        (directory / 'release-binding').touch()
                        until(lambda: read(directory / 'runtime.json')['state'] == 'running')
                        snapshot(profile, 'bridge-validated', directory)
                kill(name)
                snapshot(profile, 'dormant-' + exit_mode, directory)
                # Native IDs/session files were produced by the executable/IPC double.
                # Early-exit bridge cases intentionally have not bound a session.
                (directory / 'hold-binding').unlink(missing_ok=True)
                (directory / 'child-mode').unlink(missing_ok=True)
                (directory / 'child-seen').unlink(missing_ok=True)
                hold_binding = False
                child_mode = 'hold'
                try:
                    resumed = lifecycle.start(root, resume=name, socket_path=socket)
                    until(lambda: (directory / 'child-seen').exists())
                    until(lambda: read(directory / 'runtime.json').get('startup_checked'))
                    snapshot(profile, 'resumed-' + exit_mode, directory, outcome='started')
                finally:
                    kill(name)
            print(commit[:8], profile, 'captured', flush=True)
finally:
    try: Tmux(socket).run('kill-server')
    except (OSError, RuntimeError): pass
    write(output / 'provenance.json', {'writer_commit': commit, 'source': str(source), 'work': str(work),
        'records': len(records), 'substitutions': ['provider executable', 'native IPC (service, relay binding, observers)'],
        'real': ['adapter CommandSpec', 'lifecycle', 'runner', 'tmux', 'processes', 'registration/cleanup sidecars'],
        'finished': len(records) > 200})
    # Keep the private raw records as evidence; the driver owns the bounded scratch directory.
    print('RAW_WORK', work, flush=True)
