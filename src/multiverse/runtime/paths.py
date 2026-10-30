"""User-owned runtime paths; select the override afresh for every caller."""
import os
from pathlib import Path
import sys
import stat
from uuid import UUID


def run_directory(environment=None):
    """The durable run root, without creating it or caching a caller's environment."""
    env = os.environ if environment is None else environment
    path = Path(env.get('MV2_RUN_DIR') or Path(env.get('HOME') or Path.home()) / '.multiverse' / 'run')
    if not path.is_absolute():
        raise ValueError('MV2_RUN_DIR must be an absolute path')
    return path.resolve()


def native_socket(launch_id, *, directory=None):
    """A launch's local bridge address; refuse length before a native child starts."""
    if not isinstance(launch_id, str) or str(UUID(launch_id)) != launch_id:
        raise ValueError('A native socket requires a launch UUID')
    root = recorded_directory(directory) if directory is not None else run_directory()
    path = root / 'native' / launch_id / 'rpc.sock'
    return socket_path(path, variable='MV2_RUN_DIR', kind='Native')


def socket_path(path, *, variable, kind='Tmux'):
    """Validate a complete Unix address before any provider or terminal work."""
    path = Path(path)
    limit = 103 if sys.platform == 'darwin' else 107
    if len(os.fsencode(path)) > limit:
        raise ValueError(f'{kind} socket path is longer than {limit} bytes: {path}; set {variable} to a shorter absolute path')
    return path


def recorded_directory(value):
    """Validate the saved spelling without re-resolving a running launch's path."""
    if not isinstance(value, (str, Path)) or not str(value) or '\0' in str(value):
        raise ValueError('Recorded run directory must be an absolute path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Recorded run directory must be an absolute path')
    return path


def directory_problem(directory):
    """Name an obstruction without changing a caller's file or directory."""
    path = Path(directory)
    for part in (*reversed(path.parents), path):
        try:
            info = part.stat()
        except FileNotFoundError:
            if not part.is_symlink():
                continue
            kind = 'broken symbolic link'
        else:
            if stat.S_ISDIR(info.st_mode):
                continue
            kind = ('regular file' if stat.S_ISREG(info.st_mode) else
                    'FIFO' if stat.S_ISFIFO(info.st_mode) else
                    'socket' if stat.S_ISSOCK(info.st_mode) else 'non-directory entry')
        return f'Socket directory {path} is blocked by a {kind} at {part}. Move it aside without deleting its contents'
    return None


def prepare(directory=None):
    """Create a private run root; never chmod an existing user directory."""
    root = recorded_directory(directory) if directory is not None else run_directory()
    if problem := directory_problem(root):
        raise ValueError(problem)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700:
        raise ValueError(f'Run directory {root} must belong to this user with mode 0700; set MV2_RUN_DIR to a private directory')
    return root


def launch_socket(launch_id, environment):
    """Saved requests retain their original location across upgrades and resumes."""
    if 'MV2_RUN_DIR' in environment:
        return native_socket(launch_id, directory=recorded_directory(environment['MV2_RUN_DIR']))
    if not isinstance(launch_id, str) or str(UUID(launch_id)) != launch_id:
        raise ValueError('A native socket requires a launch UUID')
    return Path('/tmp') / ('mv2-native-' + launch_id) / 'rpc.sock'
