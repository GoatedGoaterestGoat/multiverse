"""Recover a tmux address only for a positively identified server process."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import time

from ..runtime.process import process_command, process_parent, process_snapshot, start_matches, valid_start
from ..store import write


class Unreachable(RuntimeError):
    """A terminal server is unreachable, rather than a damaged agent record."""


class UnreachableReason(str):
    """A JSON-compatible observation that retains its recovery error category."""


def address(backend):
    if backend.prefix[1] == '-S':
        path = Path(backend.prefix[2])
    else:
        path = Path(os.environ.get('TMUX_TMPDIR') or '/tmp') / f'tmux-{os.getuid()}' / backend.prefix[2]
    # Canonicalize the existing prefix even if a cleaner removed the final
    # directory: /tmp and /private/tmp must share one recovery lock.
    return path.resolve()


def registry(socket):
    return Path.home() / '.multiverse' / 'tmux-servers' / (hashlib.sha256(os.fsencode(socket)).hexdigest() + '.json')


@contextmanager
def _regular(path, *, create=False):
    flags = os.O_NONBLOCK | os.O_NOFOLLOW | (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(f'{path} is not a regular file owned by this user')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


@contextmanager
def serialized(socket):
    path = registry(socket).with_suffix('.lock')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _regular(path, create=True) as stream:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Unreachable(f'Timed out waiting for tmux socket recovery at {socket}; retry') from None
                time.sleep(.02)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _tmux(pid):
    command = process_command(pid)
    return isinstance(command, str) and Path(command.removesuffix(': server')).name == 'tmux'


def _state(proof):
    pid, started = proof.get('pid'), proof.get('started')
    if type(pid) is not int or pid <= 0 or not valid_start(started):
        return 'unknown'
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'gone'
    except (PermissionError, OverflowError):
        return 'unknown'
    observed = process_snapshot(pid)
    if observed is None:
        return 'unknown'
    if observed[1].startswith(('Z', 'X')):
        return 'gone'
    if not start_matches(started, pid, observed=observed[0]):
        return 'gone'  # This PID belongs to a later process. Never signal it.
    return 'live' if _tmux(pid) else 'unknown'


def runner_server(facts):
    """Legacy launches prove the server through their still-live runner's parent."""
    pid, started = facts.get('runner_pid'), facts.get('runner_started')
    observed = process_snapshot(pid)
    if (observed is None or observed[1].startswith(('Z', 'X'))
            or not start_matches(started, pid, observed=observed[0])):
        return None
    parent = process_parent(pid)
    if parent == 1:
        return None  # Its tmux exited; the orphaned runner may still be draining cleanup.
    command = process_command(parent)
    if isinstance(command, str) and Path(command.removesuffix(': server')).name != 'tmux':
        return None
    server = process_snapshot(parent)
    if server is None:
        raise Unreachable(f'Cannot verify the tmux parent of live runner PID {pid}; no signal sent; retry after restoring process access')
    proof = {'pid': parent, 'started': server[0]}
    if (_state(proof) != 'live' or process_parent(pid) != parent
            or not start_matches(started, pid)):
        raise Unreachable(f'Cannot verify the tmux parent of live runner PID {pid}; no signal sent; retry after restoring process access')
    return proof


def _saved(socket):
    path = registry(socket)
    try:
        with _regular(path) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('server identity is too large')
        value = json.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise Unreachable(f'Cannot read tmux server identity at {path}: {error}; repair the record before retrying') from None
    if (not isinstance(value, dict) or value.get('socket') != str(socket)
            or type(value.get('pid')) is not int or value['pid'] <= 0 or not valid_start(value.get('started'))):
        raise Unreachable(f'Invalid tmux server identity at {path}; repair the record before retrying')
    return value


def _problem(socket, proof, reason):
    pid = proof.get('pid', 'unknown')
    command = f'mkdir -m 700 -p {shlex.quote(str(socket.parent))} && kill -USR1 {pid}'
    return Unreachable(f'Tmux server PID {pid} is unreachable at {socket}: {reason}. '
                       f'After verifying that PID still names this tmux server, recover its socket with: {command}')


def _conflict(socket, proofs):
    names = ', '.join(f'{source}: PID {proof["pid"]}' for source, proof in proofs)
    return Unreachable(f'Tmux socket {socket} has conflicting server identities ({names}). No signal sent. '
                       'Resolve this server conflict before retrying; do not send SIGUSR1 while another server owns the path')


def _probe(backend, socket):
    try:
        # Server identity and pane tags come from the same server response.
        output = backend._run('list-panes', '-a', '-F', backend.pane_format)
    except RuntimeError as error:
        reason = str(error)
        if reason == 'no current target':
            output = ''  # An older tmux with no sessions still answers display-message.
        elif ('no server running' in reason or 'No such file or directory' in reason and 'connecting to' in reason
              or 'Connection refused' in reason and 'connecting to' in reason):
            return None
        else:
            raise
    rows = [line.rsplit('\t', 2) for line in output.splitlines()]
    if rows:
        if any(len(row) != 3 for row in rows) or len({tuple(row[1:]) for row in rows}) != 1:
            raise Unreachable(f'Cannot read the answering tmux server identity at {socket}')
        pid, epoch = rows[0][1:]
        output = '\n'.join(row[0] for row in rows)
    else:
        pid, epoch = backend._run('display-message', '-p', '#{pid}\t#{start_time}').split('\t')
    if not pid.isdecimal() or int(pid) <= 0:
        raise Unreachable(f'Cannot read the answering tmux server PID at {socket}')
    return output, {'pid': int(pid), 'epoch': int(epoch) if epoch.isdecimal() and int(epoch) > 0 else None}


def _same(left, right):
    return (left['pid'], left['started']) == (right['pid'], right['started'])


def _remember(socket, current):
    observed = process_snapshot(current['pid'])
    if observed is None or observed[1].startswith(('Z', 'X')) or not _tmux(current['pid']):
        raise Unreachable(f'Cannot verify the answering tmux server PID {current["pid"]} at {socket}')
    proof = {**current, 'started': observed[0], 'socket': str(socket)}
    write(registry(socket), proof)
    return proof


def connected(backend, *, facts=None):
    """Under serialized(), return a verified pane listing, or None for a gone server."""
    socket = address(backend)
    saved_error = None
    try:
        saved = _saved(socket)
    except Unreachable as error:
        saved, saved_error = None, error
    terminal = (facts or {}).get('tmux') or {}
    recorded = terminal.get('server') if isinstance(terminal, dict) else None
    if recorded is not None and (not isinstance(recorded, dict) or recorded.get('socket') != str(socket)
                                or type(recorded.get('pid')) is not int or recorded['pid'] <= 0
                                or not valid_start(recorded.get('started'))):
        raise Unreachable(f'Tmux server identity does not name its recorded socket {socket}; repair runtime.json')
    answer = _probe(backend, socket)
    if answer is None and saved_error is not None:
        raise saved_error  # A missing socket cannot repair an unreadable process identity.
    # Healthy observation stays one tmux query, with no process queries. PID
    # alone is insufficient: a later server may have reused it. Old tmux without
    # start_time and older registry entries take the full proof path.
    if answer is not None and saved is not None:
        current = answer[1]
        if (current['epoch'] is not None and (current['pid'], current['epoch']) == (saved['pid'], saved.get('epoch'))
                and (recorded is None or _same(recorded, saved)
                     or (facts or {}).get('state') in ('stopped', 'dormant', 'failed'))):
            return answer[0]
    legacy = runner_server(facts or {}) if recorded is None else None
    states = {}
    def state(proof):
        key = proof['pid'], proof['started']
        if key not in states:
            states[key] = _state(proof)
        return states[key]
    proofs = [(source, proof) for source, proof in (('registry', saved), ('runtime', recorded), ('runner parent', legacy))
              if proof is not None and state(proof) != 'gone']
    if len({(proof['pid'], proof['started']) for _, proof in proofs}) > 1:
        raise _conflict(socket, proofs)
    expected = proofs[0][1] if proofs else None
    if answer is not None:
        output, current = answer
        observed = process_snapshot(current['pid'])
        if expected and (observed is None or not _same({**current, 'started': observed[0]}, expected)):
            raise _conflict(socket, [*proofs, ('answering server', current)])
        _remember(socket, current)
        return output
    if expected is None:
        if saved is not None:
            registry(socket).unlink(missing_ok=True)  # Its original process is proven gone.
        return None
    if state(expected) != 'live':
        raise _problem(socket, expected, 'its process identity or executable could not be verified; no signal sent')
    try:
        socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if socket.parent.stat().st_uid != os.getuid():
            raise OSError('the socket directory belongs to another user')
        # Never change an existing directory's mode: an explicit socket may
        # live directly in the user's home or project folder.
        answer = _probe(backend, socket)
        if answer is None:
            if _state(expected) != 'live':
                raise OSError('the recorded server changed before recovery')
            os.kill(expected['pid'], signal.SIGUSR1)
        deadline = time.monotonic() + 3
        while answer is None and time.monotonic() < deadline:
            time.sleep(.02)
            answer = _probe(backend, socket)
        if answer is None:
            raise OSError('the server did not accept a connection after SIGUSR1')
        output, current = answer
        observed = process_snapshot(current['pid'])
        if observed is None or not _same({**current, 'started': observed[0]}, expected):
            raise _conflict(socket, [*proofs, ('answering server', current)])
        _remember(socket, current)
        return output
    except Unreachable:
        raise
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        raise _problem(socket, expected, str(error)) from None
