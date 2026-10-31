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
from uuid import UUID

from ..runtime.process import process_command, process_parent, process_snapshot, start_matches, valid_start
from ..runtime.paths import directory_problem
from ..store import write


class Unreachable(RuntimeError):
    """A terminal server is unreachable, rather than a damaged agent record."""


class ServerConflict(Unreachable):
    """Two proven server identities must never be treated as missing bookkeeping."""


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
    deadline = time.monotonic() + 30
    def timed_out():
        if time.monotonic() >= deadline:
            raise Unreachable(f'Another mv2 process is starting or recovering the tmux server at {socket}; timed out waiting, retry') from None
    while True:
        with _regular(path, create=True) as stream:
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    timed_out()
                    time.sleep(.02)
            try:
                if not _same_lock(stream, path):
                    timed_out()
                    continue  # A previous owner pruned this inode while we waited.
                try:
                    yield
                finally:
                    _prune(socket, stream, path)
                return
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _same_lock(stream, path):
    held = os.fstat(stream.fileno())
    try:
        current = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _prune(socket, stream, path):
    """Prune only a touched, proven-gone entry after its owner has finished."""
    try:
        saved = _saved(socket)
        if saved is not None and _state(saved) != 'gone':
            return
        if not _same_lock(stream, path):
            return
        registry(socket).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        # A waiter holding this old inode must reopen and lock the path before
        # entering. Nothing in this section can publish after we unlink it.
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass  # Unknown or damaged evidence remains; cleanup never masks the operation.


def _tmux(pid):
    command = process_command(pid)
    return isinstance(command, str) and Path(command.removesuffix(': server')).name == 'tmux'


def _state(proof):
    pid, started = proof.get('pid'), proof.get('started')
    if type(pid) is not int or pid <= 0 or not valid_start(started):
        return 'unknown'
    denied = False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'gone'
    except PermissionError:
        denied = True  # A readable start token can still prove this PID was reused.
    except OverflowError:
        return 'unknown'
    observed = process_snapshot(pid)
    if observed is None:
        return 'unknown'
    if observed[1].startswith(('Z', 'X')):
        return 'gone'
    if not start_matches(started, pid, observed=observed[0]):
        return 'gone'  # This PID belongs to a later process. Never signal it.
    return 'live' if not denied and _tmux(pid) else 'unknown'


def runner_server(facts):
    """Legacy launches prove the server through their still-live runner's parent."""
    if facts.get('state') in ('stopped', 'dormant', 'failed'):
        return None  # An ended launch's old PID may now belong to someone else.
    pid, started = facts.get('runner_pid'), facts.get('runner_started')
    if type(pid) is not int or pid <= 0:
        return None
    problem = f'Cannot verify live runner PID {pid}; no signal or replacement server is safe; retry after restoring process access'
    denied = False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        denied = True
    except OverflowError:
        raise Unreachable(problem) from None
    observed = process_snapshot(pid)
    if observed is None:
        raise Unreachable(problem)
    if observed[1].startswith(('Z', 'X')):
        return None
    if not valid_start(started):
        raise Unreachable(problem)
    if not start_matches(started, pid, observed=observed[0]):
        # Converting an old Linux ctime token may itself need an unavailable
        # process query. Only comparable, differing tokens prove PID reuse.
        if started.startswith('linux:') != observed[0].startswith('linux:'):
            raise Unreachable(problem)
        return None
    if denied:
        raise Unreachable(problem)
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
    return ServerConflict(f'Tmux socket {socket} has conflicting server identities ({names}). No signal sent. '
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
              or 'Connection refused' in reason and 'connecting to' in reason
              or 'Not a directory' in reason and 'connecting to' in reason):
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
        return None  # A responsive channel is usable without authority to signal its process.
    proof = {**current, 'started': observed[0], 'socket': str(socket)}
    write(registry(socket), proof)
    return proof


def _recorded(socket, facts):
    terminal = (facts or {}).get('tmux') or {}
    recorded = terminal.get('server') if isinstance(terminal, dict) else None
    if recorded is not None and (not isinstance(recorded, dict) or recorded.get('socket') != str(socket)
                                or type(recorded.get('pid')) is not int or recorded['pid'] <= 0
                                or not valid_start(recorded.get('started'))):
        raise Unreachable(f'Tmux server identity does not name its recorded socket {socket}; repair runtime.json')
    return recorded


def _owns_pane(answer, facts):
    """An exact saved pane/session/launch tag proves which server holds this launch."""
    if answer is None or not facts:
        return False
    launch = facts.get('launch_id')
    try:
        if not isinstance(launch, str) or str(UUID(launch)) != launch:
            return False
    except ValueError:
        return False
    terminal = facts.get('tmux') or {}
    if not isinstance(terminal, dict) or not terminal.get('pane') or not terminal.get('session'):
        return False
    for line in answer[0].splitlines():
        row = line.split('\t')
        if (len(row) == 7 and row[0] == terminal['pane'] and row[1] == terminal['session']
                and row[5] == launch):
            return True  # A remain-on-exit pane still identifies its original server.
    return False


def _matches_answer(answer, saved, recorded, facts):
    if answer is None or saved is None:
        return False
    current = answer[1]
    return (current['epoch'] is not None
            and (current['pid'], current['epoch']) == (saved['pid'], saved.get('epoch'))
            and (not facts or facts.get('state') in ('stopped', 'dormant', 'failed')
                 or recorded is not None and _same(recorded, saved)
                 or _owns_pane(answer, facts)))


def healthy(backend, *, facts=None):
    """A matching answering server needs no creation/recovery lock or OS query."""
    socket = address(backend)
    recorded = _recorded(socket, facts)
    try:
        saved = _saved(socket)
    except Unreachable:
        return None  # The locked path can repair this from a fresh process proof.
    answer = _probe(backend, socket)
    return (answer[0], saved) if _matches_answer(answer, saved, recorded, facts) else None


def observe(backend, *, facts=None):
    answer = healthy(backend, facts=facts)
    if answer is not None:
        return answer
    with serialized(address(backend)):
        return connected(backend, facts=facts)  # Re-probe after any preceding creator/recovery.


def connected(backend, *, facts=None):
    """Under serialized(), return (panes, signal proof), or None for a gone server.

    The proof may be unavailable even when the server answers. Never substitute
    a previous registry entry for the identity of this response.
    """
    socket = address(backend)
    saved_error = None
    try:
        saved = _saved(socket)
    except Unreachable as error:
        saved, saved_error = None, error
    recorded = _recorded(socket, facts)
    answer = _probe(backend, socket)
    if answer is None and saved_error is not None:
        raise saved_error  # A missing socket cannot repair an unreadable process identity.
    # Healthy observation stays one tmux query, with no process queries. PID
    # alone is insufficient: a later server may have reused it. Old tmux without
    # start_time and older registry entries take the full proof path.
    if _matches_answer(answer, saved, recorded, facts):
        return answer[0], saved
    legacy = runner_server(facts or {}) if recorded is None and not _owns_pane(answer, facts) else None
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
        if expected:
            observed = process_snapshot(current['pid'])
            if (current['pid'] != expected['pid']
                    or observed is not None and not _same({**current, 'started': observed[0]}, expected)):
                raise _conflict(socket, [*proofs, ('answering server', current)])
        proof = _remember(socket, current)
        if proof is not None:
            return output, proof
        # A resume may have closed the server's last pane just after its
        # answer. Re-probe before either joining it or considering recovery.
        answer = _probe(backend, socket)
        if answer is not None:
            if expected:
                observed = process_snapshot(answer[1]['pid'])
                if (answer[1]['pid'] != expected['pid']
                        or observed is not None and not _same({**answer[1], 'started': observed[0]}, expected)):
                    raise _conflict(socket, [*proofs, ('answering server', answer[1])])
            return answer[0], None
        if expected is None:
            observed = process_snapshot(current['pid'])
            try:
                os.kill(current['pid'], 0)
                gone = observed is not None and observed[1].startswith(('Z', 'X'))
            except ProcessLookupError:
                gone = True
            except PermissionError:
                gone = False
            if not gone:
                raise Unreachable(f'Tmux server PID {current["pid"]} stopped answering at {socket}; '
                                  'its process identity is unavailable, so no signal or replacement server is safe; retry')
    if expected is not None and state(expected) != 'live':
        raise Unreachable(f'Tmux server PID {expected["pid"]} is unreachable at {socket}: '
                          'its process identity, executable or signal permission could not be verified; '
                          'no signal sent; retry after restoring process access')
    if problem := directory_problem(socket.parent):
        if expected is not None:
            raise _problem(socket, expected, problem + '; the next mv2 command will retry recovery of this server')
        raise Unreachable(problem + '; then retry the mv2 command')
    if expected is None:
        if saved is not None:
            registry(socket).unlink(missing_ok=True)  # Its original process is proven gone.
        return None
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
        return output, _remember(socket, current)
    except Unreachable:
        raise
    except PermissionError:
        raise Unreachable(f'Permission denied recovering tmux server PID {expected["pid"]} at {socket}; '
                          'another user may own the process or a sandbox may block access. '
                          'Restore access as the server owner before retrying') from None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        raise _problem(socket, expected, str(error)) from None
