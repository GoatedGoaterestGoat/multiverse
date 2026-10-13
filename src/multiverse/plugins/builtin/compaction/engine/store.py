"""The agent's compaction directory: trees, locks, log, status and installs.

    state/agents/<id>/compaction/
      config.json              the agent's own scheduler (config.py)
      trees/<turn>.json        one summary tree per turn, leaves by reference
      trees/<turn>.lock        held while a process builds that turn
      worker.lock, install.lock  the background builder and a running compaction
      turn-end.json            where the newest finished turn ended (the harness's marker)
      build.log                every build event, errors and retries included
      status.json              current activity, recent errors, failed turns
      installs/<id>.json       what a compaction rendered, for its install record
      installed.jsonl          the rows each install added, as the harness recorded them
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from uuid import uuid4

from multiverse.plugins.api import files
from .model import Turn, tree_from_record, tree_record

lock, now, read, write = files.lock, files.now, files.read_json, files.write_json

RECENT_ERRORS = 20


def _filename(name: str) -> str:
    if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
        return name
    return 'turn-' + hashlib.sha256(name.encode()).hexdigest()[:32]


def _alive(holder: dict) -> bool:
    """Whether the process that wrote a lock still runs it.

    A bare PID check is not enough: an old lock outlives a killed builder and its
    PID can be reused, so the process must also still be a Multiverse one.
    """
    try:
        pid = int(holder['pid'])
    except (KeyError, TypeError, ValueError):
        return False
    if holder.get('host') != socket.gethostname():
        return True  # cannot check another host's process; assume it is working
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: a false "alive" only delays; a false "dead" would race
    return 'multiverse' in command


class Held:
    """A lock file this process created; `release()` removes it if still ours."""

    def __init__(self, path: Path, token: str):
        self.path, self.token = path, token

    def release(self):
        try:
            if json.loads(self.path.read_text()).get('token') == self.token:
                self.path.unlink()
        except (OSError, ValueError):
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


class Store:
    def __init__(self, agent_dir):
        self.agent_dir = Path(agent_dir)
        self.directory = self.agent_dir / 'compaction'
        self.trees = self.directory / 'trees'

    # Locks -----------------------------------------------------------------

    def _acquire(self, path: Path, purpose: str) -> Held | None:
        """Create the lock file, clearing one left by a process that is gone; None if held."""
        path.parent.mkdir(parents=True, exist_ok=True)
        token = str(uuid4())
        holder = {'pid': os.getpid(), 'host': socket.gethostname(), 'token': token, 'purpose': purpose, 'at': now()}
        for _ in range(2):
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                if self._holder(path) is not None:
                    return None
                continue  # a stale lock was just removed
            with os.fdopen(descriptor, 'w') as handle:
                json.dump(holder, handle)
            return Held(path, token)
        return None

    def _holder(self, path: Path) -> dict | None:
        """Who holds a lock; removes it and returns None when its process is gone."""
        try:
            holder = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # Written but not yet flushed by its creator, or torn by a crash: trust a fresh one.
            try:
                if time.time() - path.stat().st_mtime < 10:
                    return {'pid': None, 'purpose': 'starting'}
                path.unlink()
            except OSError:
                pass
            return None
        if _alive(holder):
            return holder
        try:
            path.unlink()
        except OSError:
            pass
        return None

    def turn_lock(self, turn_id: str, purpose: str) -> Held | None:
        return self._acquire(self.trees / f'{_filename(turn_id)}.lock', purpose)

    def turn_holder(self, turn_id: str) -> dict | None:
        return self._holder(self.trees / f'{_filename(turn_id)}.lock')

    def worker_lock(self) -> Held | None:
        return self._acquire(self.directory / 'worker.lock', 'background')

    def worker_holder(self) -> dict | None:
        return self._holder(self.directory / 'worker.lock')

    def install_lock(self) -> Held | None:
        return self._acquire(self.directory / 'install.lock', 'install')

    # Trees -----------------------------------------------------------------

    def tree_path(self, turn_id: str) -> Path:
        return self.trees / f'{_filename(turn_id)}.json'

    def load_tree(self, turn: Turn) -> bool:
        """Attach the turn's cached tree; False when there is none that fits."""
        if len(turn.leaves) <= 1:
            turn.root = turn.leaves[0] if turn.leaves else None
            return bool(turn.leaves)
        try:
            record = read(self.tree_path(turn.id))
        except (OSError, ValueError):
            return False
        root = tree_from_record(turn, record)
        if root is None:
            return False
        turn.root = root
        return True

    def save_tree(self, turn: Turn, root, **meta):
        write(self.tree_path(turn.id), tree_record(turn, root, built_at=now(), **meta))
        turn.root = root

    def tree_count(self) -> int:
        return len(list(self.trees.glob('*.json'))) if self.trees.is_dir() else 0

    # Log and status --------------------------------------------------------

    def log(self, message: str):
        self.directory.mkdir(parents=True, exist_ok=True)
        line = f'{now()} [{os.getpid()}] {message}\n'
        with (self.directory / 'build.log').open('a') as handle:
            handle.write(line)

    @contextmanager
    def _status(self):
        path = self.directory / 'status.json'
        with lock(self.directory / '.status.lock'):
            try:
                status = read(path)
            except (OSError, ValueError):
                status = {}
            yield status
            write(path, status)

    def status(self) -> dict:
        try:
            return read(self.directory / 'status.json')
        except (OSError, ValueError):
            return {}

    def set_activity(self, activity: dict | None):
        with self._status() as status:
            status['activity'] = {**activity, 'pid': os.getpid(), 'at': now()} if activity else None

    def add_error(self, entry: dict):
        with self._status() as status:
            status['errors'] = [*status.get('errors', []), {**entry, 'at': now()}][-RECENT_ERRORS:]

    def set_compact_failed(self, failed: bool):
        """Whether the session's last compaction failed (the status line says so until one succeeds)."""
        with self._status() as status:
            status['compact_failed'] = bool(failed)

    def set_failure(self, turn_id: str, reason: str | None):
        with self._status() as status:
            failures = status.setdefault('failures', {})
            if reason is None:
                failures.pop(turn_id, None)
            else:
                previous = failures.get(turn_id, {})
                failures[turn_id] = {'reason': reason, 'at': now(), 'count': previous.get('count', 0) + 1,
                                     'epoch': time.time()}

    def recent_failure(self, turn_id: str, within: float) -> dict | None:
        failure = self.status().get('failures', {}).get(turn_id)
        return failure if failure and time.time() - failure.get('epoch', 0) < within else None

    # Turn ends -------------------------------------------------------------

    def mark_turn_end(self, marker: str):
        """Remember the harness's marker for the newest finished turn."""
        write(self.directory / 'turn-end.json', {'marker': marker, 'at': now()})

    def turn_end(self) -> str | None:
        try:
            marker = read(self.directory / 'turn-end.json').get('marker')
        except (OSError, ValueError, AttributeError):
            return None
        return marker if isinstance(marker, str) and marker else None

    # Installs --------------------------------------------------------------

    def save_install(self, payload: dict) -> tuple[str, Path]:
        install_id = str(uuid4())
        path = self.directory / 'installs' / f'{install_id}.json'
        write(path, {**payload, 'install_id': install_id, 'created_at': now()})
        return install_id, path

    def load_install(self, install_id: str) -> dict:
        if not re.fullmatch(r'[0-9a-f-]{36}', install_id):
            raise ValueError('Invalid install ID')
        return read(self.directory / 'installs' / f'{install_id}.json')

    def add_installed(self, record: dict):
        self.directory.mkdir(parents=True, exist_ok=True)
        with lock(self.directory / '.installed.lock'):
            with (self.directory / 'installed.jsonl').open('a') as handle:
                handle.write(json.dumps({**record, 'at': now()}, ensure_ascii=False) + '\n')
                handle.flush()
                os.fsync(handle.fileno())

    def installed(self) -> list[dict]:
        path = self.directory / 'installed.jsonl'
        records = []
        try:
            for line in path.read_text().splitlines():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue  # a torn last line from a crash
        except FileNotFoundError:
            pass
        return records
