"""One disposable run supervisor. A private socket's EOF cancels the child; no native content is logged."""
import base64
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

from . import run_files
from .process import process_snapshot, process_started

WIRE_BYTES = 64 << 20
OUTPUT_BYTES = 16 << 20
ERROR_BYTES = 64 << 10
CLEANUP_SECONDS = 5
POLL = .05


class Stopped(Exception):
    def __init__(self, kind, reason, *, gone=None):
        self.kind, self.reason, self.gone = kind, reason, gone


def scopes(values):
    for item in values:
        try:
            info = Path(item['path']).lstat()
        except FileNotFoundError:
            raise Stopped('cancelled', 'The run scope was removed or moved', gone=item['kind']) from None
        except OSError:
            raise Stopped('failed', 'The run scope cannot be verified') from None
        if not stat.S_ISDIR(info.st_mode) or run_files.identity(info) != item['identity']:
            raise Stopped('cancelled', 'The run scope was removed or moved', gone=item['kind'])


def _read_request(connection):
    deadline = time.monotonic() + CLEANUP_SECONDS
    data = bytearray()
    wanted = None
    while wanted is None or len(data) < wanted + 8:
        connection.settimeout(max(.001, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            raise Stopped('timeout', 'The run request did not arrive within its preparation bound')
        chunk = connection.recv(min(65536, (wanted + 8 - len(data)) if wanted is not None else 8 - len(data)))
        if not chunk:
            raise Stopped('cancelled', 'The calling process ended before preparation')
        data.extend(chunk)
        if wanted is None and len(data) == 8:
            wanted = struct.unpack('!Q', data)[0]
            if wanted > WIRE_BYTES:
                raise Stopped('invalid_request', 'The run plan is too large')
    return json.loads(data[8:])


def _parent(connection):
    try:
        value = connection.recv(1)
    except BlockingIOError:
        return
    if not value:
        raise Stopped('cancelled', 'The calling process ended')
    raise Stopped('cancelled', 'The run was cancelled')


def _check(connection, payload):
    _parent(connection)
    if time.monotonic() >= payload['deadline']:
        raise Stopped('timeout', 'The run deadline passed')
    scopes(payload['scopes'])


def _exited(child):
    # Observe without reaping: its unreaped PID pins the private group number until cleanup has finished.
    if all(hasattr(os, name) for name in ('waitid', 'WNOWAIT', 'P_PID')):
        result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        return result is not None and result.si_pid == child.pid
    snapshot = process_snapshot(child.pid)
    return snapshot is not None and snapshot[1].startswith('Z')


def _members(pgid, timeout):
    result = subprocess.run(['ps', '-axo', 'pid=,pgid=,stat='], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=timeout)
    if result.returncode:
        raise OSError('Process group observation failed')
    return [int(parts[0]) for line in result.stdout.splitlines()
            if len(parts := line.split()) == 3 and parts[1] == str(pgid) and not parts[2].startswith('Z')]


def _empty_group(pgid, timeout):
    try:
        return not _members(pgid, timeout)
    except (OSError, subprocess.SubprocessError):
        return False  # A failed observation cannot authorize cleanup, or prevent the next owned signal.


def _stop(child):
    """The supervisor still owns its unreaped leader; no stored PID ever grants signal authority."""
    deadline = time.monotonic() + CLEANUP_SECONDS - .5
    for sig, grace in ((signal.SIGTERM, 1), (signal.SIGKILL, 3)):
        # Even a transient ps failure must not leave a TERM-ignoring child running.
        # This group remains ours until its leader is reaped, regardless of ps.
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass  # macOS can reject signalling a zombie-only group; still prove it empty below.
        end = min(deadline, time.monotonic() + grace)
        while time.monotonic() < end:
            if _empty_group(child.pid, max(.05, min(.5, end - time.monotonic()))):
                child.wait(timeout=max(.05, deadline - time.monotonic()))
                return True
            time.sleep(.03)
    return False


# The gate prevents native execution until its PID/start token is in run.json.
# -I -S keeps the trampoline from importing caller site hooks. The native exec receives the selected env normally.
CHILD = """import os,sys
fd=int(sys.argv[1])
go=os.read(fd,1)
os.close(fd)
if go != b'g': os._exit(125)
try: os.execvpe(sys.argv[2],sys.argv[2:],dict(os.environ))
except OSError:
 os.write(2,b'mv2: the one-shot native command could not start\\n')
 os._exit(127)
"""


def supervise(connection, payload):
    directory = Path(payload['directory'])
    record = {'run_id': directory.name, 'directory': str(directory), 'universe': payload['universe'],
              'store': payload.get('store'), 'declared': [{k: item[k] for k in ('root', 'path')} for item in payload['files']],
              'owner': payload['owner'], 'supervisor': {'pid': os.getpid(), 'started': process_started(os.getpid())},
              'child': None}
    created, child, gate = False, None, None
    response, outputs = {}, {'stdout': bytearray(), 'stderr': bytearray()}
    connection.setblocking(False)
    try:
        _check(connection, payload)
        if not record['supervisor']['started']:
            raise Stopped('unavailable', 'Cannot record the run supervisor identity')
        run_files.create(directory, record)
        created = True
        for item in payload['files']:
            _check(connection, payload)
            root = directory if item['root'] == 'run' else Path(payload['store'])
            path = root / run_files.relative(item['path'])
            run_files.write_file(directory, record, path, base64.b64decode(item['content'], validate=True))
        _check(connection, payload)
        read_gate, gate = os.pipe()
        try:
            child = subprocess.Popen([sys.executable, '-I', '-S', '-c', CHILD, str(read_gate), *payload['argv']],
                                     cwd=directory, env=payload['environment'], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                     pass_fds=(read_gate,))
        finally:
            os.close(read_gate)
        token = process_started(child.pid)
        if not token:
            raise Stopped('unavailable', 'Cannot record the private child identity')
        record.update(phase='running', child={'pid': child.pid, 'started': token, 'pgid': child.pid})
        run_files.save(directory, record)
        _check(connection, payload)
        os.write(gate, b'g')
        os.close(gate)
        gate = None
        with selectors.DefaultSelector() as selector:
            selector.register(connection, selectors.EVENT_READ, ('parent', 0))
            for name, stream, limit in (('stdout', child.stdout, OUTPUT_BYTES), ('stderr', child.stderr, ERROR_BYTES)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, (name, limit))
            while True:
                _check(connection, payload)
                for key, _ in selector.select(min(POLL, max(0, payload['deadline'] - time.monotonic()))):
                    name, limit = key.data
                    if name == 'parent':
                        _parent(connection)
                        continue
                    chunk = os.read(key.fd, min(65536, limit - len(outputs[name]) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        outputs[name].extend(chunk)
                        if len(outputs[name]) > limit:
                            raise Stopped('failed', f'Native {name} exceeded the run size bound')
                if _exited(child):
                    # Drain buffered output, but do not wait forever for a descendant keeping a pipe open.
                    for name, stream, limit in (('stdout', child.stdout, OUTPUT_BYTES), ('stderr', child.stderr, ERROR_BYTES)):
                        while True:
                            try:
                                chunk = os.read(stream.fileno(), min(65536, limit - len(outputs[name]) + 1))
                            except BlockingIOError:
                                break
                            if not chunk:
                                break
                            outputs[name].extend(chunk)
                            if len(outputs[name]) > limit:
                                raise Stopped('failed', f'Native {name} exceeded the run size bound')
                    break
    except Stopped as error:
        response = {'failure': {'kind': error.kind, 'reason': error.reason, 'gone': error.gone}}
    except Exception:
        response = {'failure': {'kind': 'failed', 'reason': 'The private run could not be prepared or observed'}}
    finally:
        if gate is not None:
            os.close(gate)
        stopped = child is None
        if child is not None:
            try:
                stopped = _stop(child)
            except (OSError, subprocess.SubprocessError):
                stopped = False
            for stream in (child.stdout, child.stderr):
                if stream:
                    stream.close()
        if not stopped:
            response = {'failure': {'kind': response.get('failure', {}).get('kind', 'failed'),
                                   'reason': 'The private run could not be proven stopped', 'confirmed': False}}
        elif response.get('failure'):
            response['failure']['confirmed'] = True
        elif child is not None:
            response = {'outcome': {'returncode': child.returncode,
                                   **{key: bytes(value).decode('utf-8', errors='replace') for key, value in outputs.items()}}}
        if created and stopped:
            try:
                record['phase'] = 'ended'
                run_files.save(directory, record)
                run_files.cleanup(directory, record)
            except (OSError, ValueError, TypeError, KeyError):
                response['cleanup_problem'] = 'Private run files remain because their ownership or cleanup could not be verified'
    return response


def main():
    connection = socket.socket(fileno=int(sys.argv[1]))
    try:
        try:
            payload = _read_request(connection)
            result = supervise(connection, payload)
        except Exception:
            result = {'failure': {'kind': 'failed', 'reason': 'The private run supervisor could not read its request'}}
        data = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
        connection.settimeout(CLEANUP_SECONDS)
        connection.sendall(struct.pack('!Q', len(data)) + data)
    except (OSError, ValueError):
        pass  # Caller death cannot prevent cleanup, and content is never written to a log.
    finally:
        connection.close()


if __name__ == '__main__':
    main()
