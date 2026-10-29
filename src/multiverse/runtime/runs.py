"""Bounded one-shot model calls, separate from agent launch and delivery authority.

The caller interprets the native result. A disposable supervisor owns the native process
and its files, including when a plugin's worker dies. No agent lock spans a model call.
"""
import base64
import copy
from dataclasses import dataclass, fields, replace
import json
import math
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import sys
import time
from types import MappingProxyType
from uuid import uuid4

from ..harnesses.base import PARENT_CLAUDE
from ..harnesses.types import (CommandSpec, ModelCheck, RunDirectory, RunFailure, RunFile, RunImage,
                              RunMessage, RunOptions, RunOutcome, RunPlan, RunReply, RunRequest, RunRow, RunText)
from ..plugins import access, agent_files
from . import effects, run_files, run_supervisor
from .process import process_started

INPUT_BYTES = 32 << 20
IMAGE_BYTES = 5 << 20
ENV_BYTES = 1 << 20
IDENTITY = frozenset((*PARENT_CLAUDE, 'CLAUDE_CODE_ENABLE_FUNCTION_HOOKS', 'CODEX_THREAD_ID'))


def _environment(environment):
    if not hasattr(environment, 'items'):
        raise RunFailure('invalid_request', 'A run needs an environment mapping')
    result = {}
    for key, value in environment.items():
        if not isinstance(key, str) or not key or '=' in key or '\0' in key:
            raise RunFailure('invalid_request', 'Invalid run environment key')
        if value is not None and (not isinstance(value, str) or '\0' in value):
            raise RunFailure('invalid_request', 'Invalid run environment value')
        if not key.startswith('MV2_') and key not in IDENTITY:
            result[key] = value
    if sum(len(key.encode()) + len((value or '').encode()) + 2 for key, value in result.items()) > ENV_BYTES:
        raise RunFailure('invalid_request', 'The run environment is too large')
    return MappingProxyType(result)


def options(adapter, *, environment):
    """Observe the provider on the same scrubbed environment used to prepare a run."""
    effects.require('run_options', getattr(adapter, 'name', 'harness'))
    if not callable(getattr(adapter, 'run_options', None)):
        return RunOptions(supported=False, reason='This harness has no one-shot run provider')
    try:
        value = effects.call(adapter, 'run_options', environment=_environment(environment))
        if not isinstance(value, RunOptions) or type(value.supported) is not bool:
            raise TypeError
        return value
    except (RunFailure, access.Denied):
        raise
    except Exception:
        raise RunFailure('unavailable', 'The harness could not describe its one-shot run support') from None


def _request(request, supported):
    if not isinstance(request, RunRequest) or not isinstance(request.messages, (tuple, list)):
        raise RunFailure('invalid_request', 'Invalid one-shot request')
    if (not isinstance(request.prompt, str) or not request.prompt.strip() or not isinstance(request.system, str)
            or not isinstance(request.model, str) or not request.model.strip()
            or request.effort is not None and (not isinstance(request.effort, str) or not request.effort)):
        raise RunFailure('invalid_request', 'A run needs text, a model and a valid effort')
    size = sum(len(value.encode()) for value in (request.prompt, request.system, request.model, request.effort or ''))
    retries = request.transport_retries
    if retries is not None:
        limits = supported.transport_retries
        if (type(retries) is not int or not isinstance(limits, tuple) or len(limits) != 2
                or not limits[0] <= retries <= limits[1]):
            raise RunFailure('invalid_request', 'The harness does not accept these transport retries')
    for message in request.messages:
        if isinstance(message, RunRow):
            if message.format not in supported.formats or not isinstance(message.data, dict):
                raise RunFailure('invalid_request', 'A native run row needs this harness format and its body')
            try:
                size += len(json.dumps(message.data, allow_nan=False).encode())
            except (ValueError, TypeError):
                raise RunFailure('invalid_request', 'A native run row must be a JSON object') from None
        elif isinstance(message, RunMessage):
            if message.role not in ('user', 'assistant') or not isinstance(message.blocks, (tuple, list)) or not message.blocks:
                raise RunFailure('invalid_request', 'A run message needs a role and blocks')
            for block in message.blocks:
                if isinstance(block, RunText) and isinstance(block.text, str):
                    size += len(block.text.encode())
                elif isinstance(block, RunImage):
                    if (not supported.images or message.role != 'user' or not isinstance(block.data, bytes)
                            or len(block.data) > IMAGE_BYTES or not isinstance(block.media_type, str)
                            or block.media_type not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp')):
                        raise RunFailure('invalid_request', 'Invalid or unsupported run image')
                    size += len(block.data) * 4 // 3 + 128
                else:
                    raise RunFailure('invalid_request', 'Invalid run content block')
        else:
            raise RunFailure('invalid_request', 'Invalid run message')
        if size > INPUT_BYTES:
            raise RunFailure('too_long', 'The run input exceeds 32 MiB')
    if size > INPUT_BYTES:
        raise RunFailure('too_long', 'The run input exceeds 32 MiB')
    return copy.deepcopy(request)


def _scope(path, kind):
    path = Path(os.path.abspath(path))
    error = agent_files.AgentGone if kind == 'agent' else agent_files.UniverseGone
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise FileNotFoundError
    except FileNotFoundError:
        raise error(f'The {kind} folder is gone; no run was prepared') from None
    return {'path': str(path), 'kind': kind, 'identity': run_files.identity(info)}


@dataclass(frozen=True, kw_only=True)
class RunScope:
    """Directory identities captured before resolving the storage a one-shot call will use."""
    root: str
    root_identity: tuple
    agent_directory: str | None = None
    agent_identity: tuple | None = None

    def records(self):
        values = [(self.root, 'universe', self.root_identity)]
        if self.agent_directory is not None:
            values.append((self.agent_directory, 'agent', self.agent_identity))
        for path, _, identity in values:
            if (not isinstance(path, str) or not Path(path).is_absolute()
                    or not isinstance(identity, tuple) or len(identity) != 2
                    or not all(type(value) is int and value >= 0 for value in identity)):
                raise RunFailure('invalid_request', 'Invalid run scope snapshot')
        return [{'path': path, 'kind': kind, 'identity': list(identity)} for path, kind, identity in values]


def capture_scope(root, *, agent_directory=None):
    root = Path(os.path.abspath(root))
    agent = Path(os.path.abspath(agent_directory)) if agent_directory is not None else None
    if agent is not None and agent.parent != root / 'state' / 'agents':
        raise RunFailure('invalid_request', 'The run agent is outside this universe')
    universe = _scope(root, 'universe')
    own = _scope(agent, 'agent') if agent is not None else None
    result = RunScope(root=universe['path'], root_identity=tuple(universe['identity']),
                      agent_directory=own['path'] if own else None,
                      agent_identity=tuple(own['identity']) if own else None)
    check_scope(result)
    return result


def check_scope(scope):
    if not isinstance(scope, RunScope):
        raise RunFailure('invalid_request', 'Invalid run scope snapshot')
    _check_scopes(scope.records())


def _check_scopes(values):
    try:
        run_supervisor.scopes(values)
    except run_supervisor.Stopped as error:
        if error.gone:
            raise _gone(error.gone) from None
        raise RunFailure(error.kind, error.reason) from None


def _gone(kind):
    error = agent_files.AgentGone if kind == 'agent' else agent_files.UniverseGone
    return error(f'The {kind} folder was removed or moved during the run')


def base():
    # This is the initiating process's HOME, not an agent's replayed storage HOME.
    return (Path.home() / '.multiverse' / 'runs').resolve()


def leftovers(root):
    return run_files.leftovers(base(), root=root)


class RunExecutor:
    """One deadline and private control socket; usable from any caller thread."""
    def __init__(self, *, deadline, cancel, scopes):
        self.deadline, self.cancel, self.scopes = deadline, cancel, scopes

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise RunFailure('cancelled', 'The run was cancelled', confirmed=True)
        if time.monotonic() >= self.deadline:
            raise RunFailure('timeout', 'The run deadline passed', confirmed=True)
        _check_scopes(self.scopes)

    def execute(self, payload):
        self.check()
        data = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if len(data) > run_supervisor.WIRE_BYTES:
            raise RunFailure('too_long', 'The prepared run exceeds the transport size bound')
        output = bytearray()
        pending = memoryview(struct.pack('!Q', len(data)) + data)
        wanted, failure, cleanup_deadline, process = None, None, None, None
        connection, child = socket.socketpair()
        try:
            # Explicit source path: an editable install of another checkout must not run this supervisor.
            source = str(Path(__file__).resolve().parents[2])
            boot = ('import sys;sys.path.insert(0,sys.argv.pop(1));'
                    'from multiverse.runtime.run_supervisor import main;main()')
            process = subprocess.Popen([sys.executable, '-I', '-S', '-c', boot, source, str(child.fileno())],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       pass_fds=(child.fileno(),), start_new_session=True)
            child.close()
            connection.setblocking(False)
            while wanted is None or len(output) < wanted + 8:
                if failure is None:
                    try:
                        self.check()
                    except (RunFailure, agent_files.Gone) as error:
                        failure = error
                        cleanup_deadline = time.monotonic() + run_supervisor.CLEANUP_SECONDS + 1
                        pending = memoryview(b'')
                        connection.shutdown(socket.SHUT_WR)  # EOF also works if the request was only partly written.
                elif time.monotonic() >= cleanup_deadline:
                    if isinstance(failure, RunFailure):
                        failure.confirmed = False
                    raise failure
                readable, writable, _ = select.select([connection], [connection] if pending else [], [], .05)
                if writable:
                    pending = pending[connection.send(pending[:65536]):]
                if readable:
                    chunk = connection.recv(min(65536, wanted + 8 - len(output)) if wanted is not None else 8 - len(output))
                    if not chunk:
                        raise failure or RunFailure('failed', 'The private run supervisor ended without a result', confirmed=False)
                    output.extend(chunk)
                    if wanted is None and len(output) == 8:
                        wanted = struct.unpack('!Q', output)[0]
                        if wanted > run_supervisor.WIRE_BYTES:
                            raise RunFailure('failed', 'The private run result exceeds its size bound', confirmed=False)
            response = json.loads(output[8:])
            if failure is not None:
                if isinstance(failure, RunFailure):
                    failure.confirmed = response.get('failure', {}).get('confirmed') is True or 'outcome' in response
                raise failure
            if problem := response.get('failure'):
                if problem.get('gone'):
                    raise _gone(problem['gone'])
                raise RunFailure(problem['kind'], problem['reason'], confirmed=problem.get('confirmed'))
            return response
        finally:
            connection.close()  # Also cancels after KeyboardInterrupt or a caller exception.
            child.close()
            if process is not None:
                try:
                    process.wait(timeout=run_supervisor.CLEANUP_SECONDS + 1)
                except subprocess.TimeoutExpired:
                    pass  # Do not kill the owner of a possibly still-live child; its journal remains diagnosable.


def _plan(plan, directory, environment, adapter):
    if not isinstance(plan, RunPlan) or not isinstance(plan.command, CommandSpec) or not callable(plan.interpret):
        raise RunFailure('invalid_request', 'Invalid one-shot run plan')
    command = plan.command
    allowed = {'argv', 'environment', 'unset_env'}
    if any(getattr(command, field.name) for field in fields(command) if field.name not in allowed):
        raise RunFailure('invalid_request', 'A one-shot plan cannot describe a launch or a service')
    if (not isinstance(command.argv, (list, tuple)) or not command.argv
            or not all(isinstance(arg, str) and '\0' not in arg for arg in command.argv) or not command.argv[0]):
        raise RunFailure('invalid_request', 'A run needs an argv without NUL characters')
    if not isinstance(command.unset_env, (list, tuple)) or not all(isinstance(key, str) for key in command.unset_env):
        raise RunFailure('invalid_request', 'Invalid run environment removals')
    final_env = {key: value for key, value in environment.items() if key not in command.unset_env}
    final_env.update(command.environment)
    final_env = {key: value for key, value in _environment(final_env).items() if value is not None}
    if not isinstance(plan.files, (list, tuple)) or len(plan.files) > 16:
        raise RunFailure('invalid_request', 'Too many run files')
    size, targets, wire, store = sum(len(arg.encode()) for arg in command.argv), set(), [], None
    for item in plan.files:
        if not isinstance(item, RunFile) or item.root not in ('run', 'store') or not isinstance(item.content, bytes):
            raise RunFailure('invalid_request', 'Invalid run file')
        rel = run_files.relative(item.path)
        if item.root == 'store':
            if store is not None:
                raise RunFailure('invalid_request', 'A run may declare only one harness-store file')
            value = effects.call(adapter, 'session_home', environment)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise RunFailure('unavailable', 'The run storage cannot be resolved')
            store = str(Path(value).resolve())
            if store == '/' or rel.name != directory.id + '.jsonl':
                raise RunFailure('invalid_request', 'A store file must name this run session')
        if rel.parts[0] == 'run.json' or (item.root, item.path) in targets:
            raise RunFailure('invalid_request', 'A run file collides with another file or its journal')
        targets.add((item.root, item.path))
        size += len(item.content)
        wire.append({'root': item.root, 'path': item.path, 'content': base64.b64encode(item.content).decode('ascii')})
    if size > INPUT_BYTES:
        raise RunFailure('too_long', 'The prepared run input exceeds 32 MiB')
    return {'argv': list(command.argv), 'environment': final_env, 'files': wire, 'store': store}


def _reply(value):
    if not isinstance(value, RunReply) or not isinstance(value.text, str) or len(value.text.encode()) > run_supervisor.OUTPUT_BYTES:
        raise RunFailure('failed', 'The harness returned an invalid run reply')
    if not isinstance(value.usage, dict) or any(not isinstance(key, str) or type(n) is not int or n < 0 for key, n in value.usage.items()):
        raise RunFailure('failed', 'The harness returned invalid run usage')
    if value.cost_usd is not None and (type(value.cost_usd) not in (int, float) or not math.isfinite(value.cost_usd) or value.cost_usd < 0):
        raise RunFailure('failed', 'The harness returned invalid run cost')
    return replace(value, usage=dict(value.usage))


def _execute(executor, plan, payload):
    """The supervised run and the plan's reading of it: all that PluginTestCase's fake runs replace."""
    response = executor.execute(payload)
    executor.check()
    value = _reply(plan.interpret(RunOutcome(**response['outcome'])))
    # A paid reply remains useful even if cleanup failed; its journal is left for sweep and doctor.
    return value


def run_once(adapter, request, *, root, plugin, environment, agent_directory=None, timeout=600, cancel=None, scope=None):
    started = time.monotonic()
    effects.require('run_plan', getattr(adapter, 'name', 'harness'))
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise RunFailure('invalid_request', 'A run timeout must be positive finite seconds')
    if cancel is not None and not callable(getattr(cancel, 'is_set', None)):
        raise RunFailure('invalid_request', 'A run cancel value needs is_set()')
    try:
        root = Path(os.path.abspath(root))
        agent = Path(os.path.abspath(agent_directory)) if agent_directory is not None else None
        if agent is not None and agent.parent != root / 'state' / 'agents':
            raise RunFailure('invalid_request', 'The run agent is outside this universe')
        if scope is None:
            scope = capture_scope(root, agent_directory=agent)
        elif (not isinstance(scope, RunScope) or scope.root != str(root)
              or scope.agent_directory != (str(agent) if agent is not None else None)):
            raise RunFailure('invalid_request', 'The run scope snapshot belongs to another universe or agent')
        scopes = scope.records()
        owner = run_files.component(agent.name if agent is not None else getattr(adapter, 'name', 'harness'))
        executor = RunExecutor(deadline=started + timeout, cancel=cancel, scopes=scopes)
        executor.check()
        run_base = base()
        if (run_base == root.resolve() or root.resolve() in run_base.parents
                or agent_files.agent_folder(run_base) is not None):
            raise RunFailure('unavailable', 'One-shot run storage must be outside agents and universes')
        environment = _environment(environment)
        supported = options(adapter, environment=environment)
        executor.check()
        if not supported.supported:
            raise RunFailure('unavailable', supported.reason)
        request = _request(request, supported)
        if not callable(getattr(adapter, 'check_model', None)):
            raise RunFailure('unavailable', 'The harness cannot check a run model')
        checked = effects.call(adapter, 'check_model', request.model, effort=request.effort, environment=environment)
        executor.check()
        if not isinstance(checked, ModelCheck) or checked.ok is not True:
            raise RunFailure('invalid_request', checked.reason if isinstance(checked, ModelCheck) else 'Invalid model check')
        identifier = str(uuid4())
        directory = RunDirectory(path=run_base / run_files.component(plugin) / owner / identifier, id=identifier)
        plan = effects.call(adapter, 'run_plan', request, environment=environment, run=directory)
        executor.check()
        payload = _plan(plan, directory, environment, adapter)
        executor.check()
        # Bounded, metadata-only; never restores process authority or deletes an uncertain target.
        run_files.leftovers(run_base, sweep=True, deadline=min(executor.deadline, time.monotonic() + 1))
        executor.check()
        owner_start = process_started(os.getpid())
        if not owner_start:
            raise RunFailure('unavailable', 'Cannot record the calling process identity')
        payload.update(directory=str(directory.path), universe=str(root.resolve()), deadline=executor.deadline,
                       scopes=scopes, owner={'pid': os.getpid(), 'started': owner_start})
        value = _execute(executor, plan, payload)
        executor.check()
        return replace(value, duration=time.monotonic() - started)
    except RunFailure as error:
        error.duration = time.monotonic() - started
        raise
    except (access.Denied, agent_files.Gone):
        raise
    except Exception:
        raise RunFailure('failed', 'The one-shot run could not be prepared or interpreted',
                         duration=time.monotonic() - started) from None
