"""Launch-local session claims for a callback denied the universe namespace lock.

The runner performs the ordinary claim; this grants no extra filesystem roots
or native authority. Requests and replies never create an agent or launch.
"""
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from ..harnesses.types import normalize_session_id
from ..store import existing_lock, lock, read, uid
from ..universe import agents, sessions, records


LIMIT = 16384
WAIT_SECONDS = 10


def _read(path):
    with path.open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('Session binding request or response is too large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Session binding request or response must be an object')
    return value


def _write(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.bind-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def namespace_denied(root, error):
    return (isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
            and error.filename is not None
            and Path(error.filename).resolve() == sessions.top(root) / 'state' / '.sessions.lock')


def request_binding(root, directory, launch_id, native_id):
    from .agent_identity import sender_actor
    caller = sender_actor()
    if (caller is None or caller.directory.resolve() != directory.resolve()
            or caller.root.resolve() != root.resolve() or not launch_id):
        raise ValueError('Only this agent\'s current terminal launch may request its runner to bind a session')
    native_id = normalize_session_id(native_id)
    launch = directory / 'launches' / launch_id
    request_id = uid()
    with lock(launch / '.bind.lock', create_parent=False):
        with existing_lock(directory / '.launch.lock'):
            _current(directory, launch_id)
            data = read(directory / 'properties.json')
            effective, _ = sessions.effective(directory, data)
            if data.get('native_session_id') is not None:
                if data.get('native_session_id') == effective.get('native_session_id') == native_id:
                    return data  # Exact repeat: no claim or runner request is needed.
                raise ValueError('This agent is already bound to a different native session')
            _write(launch / 'bind-request.json', {'id': request_id, 'agent_id': directory.name,
                   'launch_id': launch_id, 'native_session_id': native_id})
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            with existing_lock(directory / '.launch.lock'):
                _current(directory, launch_id)
                try:
                    reply = _read(launch / 'bind-response.json')
                except FileNotFoundError:
                    reply = {}
                if reply.get('id') == request_id:
                    if reply.get('ok') is not True:
                        raise ValueError(reply.get('error') or 'The runner refused the session binding')
                    data = read(directory / 'properties.json')
                    if data.get('native_session_id') != native_id:
                        raise ValueError('Session binding changed before confirmation')
                    return data
            time.sleep(.05)
    raise ValueError('Session binding is not confirmed: the runner did not answer; check the agent before retrying')


def _current(directory, launch_id):
    facts = agents.runtime(directory)
    if facts.get('launch_id') != launch_id:
        raise ValueError('Outdated launch; refusing session binding')
    if facts.get('state') not in ('starting', 'running') or facts.get('ending_at') or facts.get('native_refusal'):
        raise ValueError('The terminal launch is ending or failed; refusing session binding')
    return facts


class BindingRequests:
    """One runner-owned listener, bounded to one serialized request per launch."""
    def __init__(self, root, launch, alive):
        self.root, self.launch, self.alive = root, launch, alive
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='session binding', daemon=True)
        self.last_request = None

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError('Session binding claim is still finishing')

    def _run(self):
        while not self.stopped.is_set():
            try:
                if self.check():
                    return
            except (OSError, ValueError, KeyError, TypeError):
                pass  # No valid correlation or writable reply: caller gets no success.
            self.stopped.wait(.2)

    def check(self):
        request = _read(self.launch / 'bind-request.json')
        request_id = request.get('id')
        if normalize_session_id(request_id) != request_id or request_id == self.last_request:
            return
        directory = self.launch.parent.parent
        with existing_lock(directory / '.launch.lock'):
            try:
                facts = _current(directory, self.launch.name)
                if (self.stopped.is_set() or not self.alive(facts) or facts.get('control_endpoint')
                        or request.get('agent_id') != directory.name or request.get('launch_id') != self.launch.name):
                    raise ValueError('Session binding request does not belong to this active plain launch')
                data = records.properties(directory / 'properties.json')
                if (data.get('id') != directory.name
                        or data.get('universe_id') != read(self.root / 'universe.json')['id']):
                    raise ValueError('Session binding agent no longer matches its universe')
                native_id = normalize_session_id(request.get('native_session_id'))
                agents._bind_session_locked(self.root, directory, native_id, self.launch.name)
                reply = {'id': request_id, 'ok': True}
            except (OSError, ValueError, KeyError, TypeError) as error:
                reply = {'id': request_id, 'ok': False, 'error': str(error)[:4096]}
            _write(self.launch / 'bind-response.json', reply)
            self.last_request = request_id
            return reply['ok']
