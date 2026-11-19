"""One-time copies of a parent's public definitions, retaining its refresh provenance."""
import json
import errno
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from ..store import now, retry_missing, uid, write
from . import templates

# Definition namespaces may contain locally added files as well as shipped ones. Nothing traverses state,
# nested universes, unmanaged plugin code/trust, native homes or generated artifacts to discover extra files.
DIRECTORIES = ('harnesses', 'views', 'skills', 'workflows', 'commands', 'prompts', 'plugin-settings')
ROOT_FILES = ('bootstrap.md', 'statusline.json', 'plugins.json', '.gitignore')
PRIVATE = templates.UNMANAGED | {'auth.json', 'credentials.json', 'token', 'secrets', 'plugin-secrets',
                                 'trusted-plugins.json', 'installed-plugins.json', 'plugin-fingerprints.json'}
FINGERPRINT = re.compile(r'sha256:[0-9a-f]{64}')


def _public(name):
    if not isinstance(name, str):
        return False
    if name == '.gitignore':
        return True
    path = Path(name)
    return (bool(path.parts) and not path.is_absolute()
            and path.parts[0] not in PRIVATE
            and not any(part in ('..', '__pycache__') or part.startswith('.') for part in path.parts))


def _read(root, name):
    """Read a fixed regular file through no-follow directory descriptors; never follow a replaced link."""
    parts = Path(name).parts
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
        with os.fdopen(child, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f'{root / name}: definition is not a regular file')
            raw = stream.read(before.st_size)
            after = os.fstat(stream.fileno())
            if (len(raw) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError(f'{root / name}: definition changed while inheriting; retry')
            return raw, stat.S_IMODE(before.st_mode) & 0o777
    finally:
        os.close(descriptor)


def _snapshot(root, name):
    try:
        return retry_missing(lambda: _read(root, name), path=root / name)
    except OSError as error:
        if error.errno == errno.ENOENT:
            raise
        reason = ('is a link or has a non-directory path component' if error.errno in (errno.ELOOP, errno.ENOTDIR)
                  else f'cannot be read ({error.strerror})')
        raise _problem(root, f'{name} {reason}') from error


def _problem(root, reason):
    return ValueError(f'Cannot inherit {root}: {reason}. Repair the parent definitions or install record, '
                      'or pass --template NAME for a clean template copy')


def _manifest(root):
    try:
        raw, _ = _snapshot(root, templates.MANIFEST)
        value = json.loads(raw.decode('utf-8'))
        if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1
                or not isinstance(value.get('template'), str) or not value['template']
                or not isinstance(value.get('files'), dict) or not isinstance(value.get('bases', {}), dict)):
            raise _problem(root, 'its install record is missing required fields')
        for name, fingerprint in value['files'].items():
            if not isinstance(name, str) or not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
                raise _problem(root, 'its install record has an invalid file fingerprint')
        for name, text in value.get('bases', {}).items():
            if (name not in value['files'] or not isinstance(text, str)
                    or templates._text_fingerprint(text) != value['files'][name]):
                raise _problem(root, 'its saved base does not match its recorded version')
        source = value.get('source', '')
        if not isinstance(source, str) or Path(source).is_absolute() or '..' in Path(source).parts:
            raise _problem(root, 'its recorded template scope is invalid')
        try:
            shipped = templates.template_source(value['template']) / source
        except ValueError as error:
            raise _problem(root, 'its recorded template is not available') from error
        marker, _ = _snapshot(shipped, 'universe.json')
        if not isinstance(json.loads(marker.decode('utf-8')), dict):
            raise _problem(root, 'its template scope is not a universe')
        return value, raw, shipped
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _problem(root, 'its install record or template cannot be read') from error


def _files(root, recorded):
    names = {name for name in recorded if _public(name)}
    names.update(ROOT_FILES)
    for name in DIRECTORIES:
        base = root / name
        try:
            info = base.lstat()
        except FileNotFoundError:
            continue  # Definition directories themselves are not atomically replaced records.
        if not stat.S_ISDIR(info.st_mode):
            raise _problem(root, f'{name} is not an ordinary definition directory')
        def failed(error):
            raise error
        for directory, folders, files in os.walk(base, followlinks=False, onerror=failed):
            folders[:] = [part for part in folders if not part.startswith('.') and part != '__pycache__']
            for part in folders:
                if (Path(directory) / part).is_symlink():
                    raise _problem(root, 'a definition directory is a link')
            names.update((Path(directory) / part).relative_to(root).as_posix() for part in files
                         if not part.startswith('.') and part != '__pycache__')
    return sorted(name for name in names if _public(name))


def create(parent, destination, name, workdir):
    """Inherit public configuration only; unknown or damaged install provenance refuses before publication."""
    parent, destination = Path(parent).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError(f'Already exists: {destination}')
    manifest, manifest_raw, shipped = _manifest(parent)
    meta_raw, _ = _snapshot(parent, 'universe.json')
    meta = json.loads(meta_raw.decode('utf-8'))
    if not isinstance(meta, dict):
        raise _problem(parent, 'its universe record is not an object')
    files = {path: fingerprint for path, fingerprint in manifest['files'].items() if _public(path)}
    bases = {}
    for path, fingerprint in files.items():
        if not templates.mergeable(path):
            continue
        text = templates._base_for(path, fingerprint, manifest.get('bases', {}))
        if text is None:
            try:
                candidate, _ = _snapshot(shipped, path)
                candidate = candidate.decode('utf-8')
                if templates._text_fingerprint(candidate) == fingerprint:
                    text = candidate
            except (FileNotFoundError, UnicodeDecodeError):
                pass  # Unknown historical custom versions stay unknown, exactly as in the parent's refresh.
        if text is not None:
            bases[path] = text
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.mv2-subverse-', dir=destination.parent))
    try:
        for path in _files(parent, manifest['files']):
            try:
                raw, mode = _snapshot(parent, path)
            except FileNotFoundError:
                continue  # Preserve a parent's deleted definition rather than reinstalling the template's.
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(mode)
        meta.update(id=uid(), name=name, created_at=now(), template=manifest['template'],
                    workspace=os.path.relpath(Path(workdir).resolve(), destination))
        write(staging / 'universe.json', meta)
        write(staging / templates.MANIFEST, {'schema': 1, 'template': manifest['template'],
                                           'source': manifest.get('source', ''), 'files': files, 'bases': bases})
        (staging / 'state/agents').mkdir(parents=True)
        (staging / 'subverses').mkdir()
        if _snapshot(parent, templates.MANIFEST)[0] != manifest_raw or _snapshot(parent, 'universe.json')[0] != meta_raw:
            raise _problem(parent, 'its configuration identity changed during the copy; retry')
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    for scope in (destination, parent):
        templates._event(scope, 'universe.created', {'name': name, 'path': str(destination),
                                                   'template': manifest['template']})
    return destination
