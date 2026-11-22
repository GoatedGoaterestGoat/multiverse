"""Anchored, non-following private file writes and cleanup strictly inside a launch directory."""
from collections.abc import Collection, Iterable, Mapping, Sequence
import errno
import json
import os
from pathlib import Path
import stat
from uuid import uuid4


# Core-owned entries directly under launches/<launch_id>/ that adapters may never overwrite or unlink.
RESERVED_TOP_LEVEL = frozenset((
    'bootstrap.md',
    'task.md',
    'request.json',
    'ready',
    'ready-event',
    'registration.json',
    'cleanup.json',
    'native-service.log',
    'output-tail.txt',
    'bind-request.json',
    'bind-response.json',
    'native-refusal.json',
    'roles',
    '.connections.lock',
    '.bind.lock',
    '.refusal.lock',
))
_RESERVED_PREFIXES = ('.mv2-', '.write-')
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MAX_PATH_LENGTH = 512


def validate_path(relative: str) -> tuple[str, ...]:
    """Validate one relative path inside launches/<launch_id>/ and return its path components."""
    if (not isinstance(relative, str) or not relative or len(relative) > _MAX_PATH_LENGTH
            or '\0' in relative or '\\' in relative or relative.startswith('/') or relative.endswith('/')):
        raise ValueError(f'Launch file path must be a safe relative path: {relative!r}')
    parts = tuple(relative.split('/'))
    if any(part in ('', '.', '..') or part.startswith(_RESERVED_PREFIXES) for part in parts):
        raise ValueError(f'Launch file path must stay inside the launch folder: {relative!r}')
    if parts[0] in RESERVED_TOP_LEVEL:
        raise ValueError(f'Launch file path collides with a core launch file: {relative!r}')
    return parts


def validate_paths(paths: Iterable[str]) -> list[tuple[str, ...]]:
    """Validate a collection of relative launch paths and reject file/directory prefix collisions."""
    parsed = [validate_path(path) for path in paths]
    seen = set(parsed)
    for parts in parsed:
        for depth in range(1, len(parts)):
            if parts[:depth] in seen:
                raise ValueError(f'Launch file path collides with parent path: {"/".join(parts)!r}')
    return parsed


def validate_component(name: str) -> str:
    """Validate one single directory component name such as a launch ID."""
    if (not isinstance(name, str) or not name or len(name) > _MAX_PATH_LENGTH
            or '/' in name or '\\' in name or '\0' in name or name in ('.', '..')):
        raise ValueError(f'Launch directory name must be a single path component: {name!r}')
    return name


def ensure_launch_dir(agent_dir: Path, launch_id: str) -> Path:
    """Create `agent_dir/launches/<launch_id>` anchored at `agent_dir` without following symlinks."""
    agent = Path(agent_dir)
    name = validate_component(launch_id)
    agent_fd = os.open(agent, _DIR_FLAGS)
    try:
        try:
            os.mkdir('launches', 0o777, dir_fd=agent_fd)
        except FileExistsError:
            pass
        launches_fd = os.open('launches', _DIR_FLAGS, dir_fd=agent_fd)
        try:
            os.mkdir(name, 0o777, dir_fd=launches_fd)
            launch_fd = os.open(name, _DIR_FLAGS, dir_fd=launches_fd)
            os.close(launch_fd)
        finally:
            os.close(launches_fd)
    finally:
        os.close(agent_fd)
    return agent / 'launches' / name


def _open_launch_root(launch: Path, *, agent_dir: Path | None = None) -> int:
    launch = Path(launch)
    if agent_dir is not None:
        agent = Path(agent_dir)
        name = validate_component(launch.name)
        if launch.parent != agent / 'launches':
            raise ValueError(f'Launch folder must live directly under agent launches: {launch!r}')
        agent_fd = os.open(agent, _DIR_FLAGS)
        try:
            launches_fd = os.open('launches', _DIR_FLAGS, dir_fd=agent_fd)
            try:
                return os.open(name, _DIR_FLAGS, dir_fd=launches_fd)
            finally:
                os.close(launches_fd)
        finally:
            os.close(agent_fd)
    if launch.parent.name == 'launches' and launch.parent.parent != launch.parent:
        return _open_launch_root(launch, agent_dir=launch.parent.parent)
    parent_fd = os.open(launch.parent, _DIR_FLAGS)
    try:
        return os.open(validate_component(launch.name), _DIR_FLAGS, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def read_request(agent_dir: Path, launch_id: str) -> dict:
    """Read `launches/<launch_id>/request.json` anchored at `agent_dir` without following symlinks."""
    from ..store import retry_missing
    launch = Path(agent_dir) / 'launches' / validate_component(launch_id)

    def _read():
        root_fd = _open_launch_root(launch, agent_dir=agent_dir)
        try:
            fd = os.open('request.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, 'not a regular file', str(launch / 'request.json'))
                with os.fdopen(fd, 'rb') as handle:
                    fd = -1
                    data = json.loads(handle.read().decode('utf-8'))
            finally:
                if fd >= 0:
                    os.close(fd)
        finally:
            os.close(root_fd)
        if not isinstance(data, dict):
            raise ValueError('Launch request must be a JSON object')
        return data

    return retry_missing(_read, path=launch / 'request.json')


def write_request(agent_dir: Path, launch_id: str, payload: Mapping[str, object]) -> None:
    """Atomically write `launches/<launch_id>/request.json` anchored at `agent_dir` without following symlinks."""
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    launch = Path(agent_dir) / 'launches' / validate_component(launch_id)
    root_fd = _open_launch_root(launch, agent_dir=agent_dir)
    try:
        try:
            existing = os.stat('request.json', dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
                                     or existing.st_nlink > 1):
            raise ValueError('Refusing to overwrite linked or non-regular request.json')
        temp_name = f'.write-{uuid4().hex}'
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
        replaced = False
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                fd = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, 'request.json', src_dir_fd=root_fd, dst_dir_fd=root_fd)
            replaced = True
            _fsync_dir(root_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            if not replaced:
                try:
                    os.unlink(temp_name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(root_fd)


def _walk_parent(root_fd: int, parents: tuple[str, ...], *, create: bool) -> int | None:
    """Walk intermediate directories via O_DIRECTORY | O_NOFOLLOW; caller closes returned fd (unless root_fd)."""
    current = root_fd
    try:
        for part in parents:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    if current != root_fd:
                        os.close(current)
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            if current != root_fd:
                os.close(current)
            current = child
        return current
    except BaseException:
        if current != root_fd:
            os.close(current)
        raise


def _fsync_dir(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.EBADF, errno.EROFS):
            raise


def write_files(launch: Path, files: Mapping[str, str] | None, *, allowed: Collection[str] | None = None,
                agent_dir: Path | None = None) -> None:
    """Atomically write adapter launch files strictly inside `launch` with mode 0600, never following links."""
    if not files:
        if files is not None and not isinstance(files, Mapping):
            raise ValueError('Launch files must be a mapping of relative paths to text content')
        return
    if not isinstance(files, Mapping):
        raise ValueError('Launch files must be a mapping of relative paths to text content')
    allowed_set = None
    if allowed is not None:
        validate_paths(allowed)
        allowed_set = set(allowed)
    # Validate and encode every key and value before creating any directory or file.
    entries = []
    for relative, content in files.items():
        if not isinstance(content, str):
            raise ValueError(f'Launch file content must be text: {relative!r}')
        try:
            encoded = content.encode('utf-8')
        except UnicodeEncodeError as error:
            raise ValueError(f'Launch file content must be valid UTF-8 text: {relative!r}') from error
        parts = validate_path(relative)
        if allowed_set is not None and relative not in allowed_set:
            raise ValueError(f'Launch file is not in this launch\'s declared cleanup_files: {relative!r}')
        entries.append((relative, parts, encoded))
    validate_paths(files.keys())

    root_fd = _open_launch_root(Path(launch), agent_dir=agent_dir)
    try:
        for relative, parts, encoded in entries:
            parent_fd = _walk_parent(root_fd, parts[:-1], create=True)
            try:
                leaf = parts[-1]
                try:
                    existing = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    if (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
                            or existing.st_nlink > 1):
                        raise ValueError(f'Refusing to overwrite linked or non-regular launch file: {relative!r}')
                temp_name = f'.mv2-tmp-{uuid4().hex}'
                fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
                replaced = False
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, 'wb') as handle:
                        fd = -1
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp_name, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    replaced = True
                    _fsync_dir(parent_fd)
                finally:
                    if fd >= 0:
                        os.close(fd)
                    if not replaced:
                        try:
                            os.unlink(temp_name, dir_fd=parent_fd)
                        except FileNotFoundError:
                            pass
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
    finally:
        os.close(root_fd)


def unlink_files(launch: Path, paths: Sequence[str] | None, *, agent_dir: Path | None = None) -> list[str]:
    """Unlink declared regular files strictly inside `launch`, never following links or recursing."""
    if not paths:
        return []
    errors = []
    parsed = []
    for relative in paths:
        try:
            parsed.append((relative, validate_path(relative)))
        except ValueError as error:
            errors.append(str(error))
    if not parsed:
        return errors
    try:
        root_fd = _open_launch_root(Path(launch), agent_dir=agent_dir)
    except FileNotFoundError:
        return errors
    except OSError as error:
        errors.append(f'cannot open launch folder: {error}')
        return errors
    try:
        for relative, parts in parsed:
            parent_fd = None
            try:
                parent_fd = _walk_parent(root_fd, parts[:-1], create=False)
                if parent_fd is None:
                    continue
                leaf = parts[-1]
                try:
                    info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    errors.append(f'refused to unlink non-regular launch file {relative!r}')
                    continue
                try:
                    os.unlink(leaf, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            except OSError as error:
                errors.append(f'{relative}: {error}')
            finally:
                if parent_fd is not None and parent_fd != root_fd:
                    os.close(parent_fd)
    finally:
        os.close(root_fd)
    return errors
