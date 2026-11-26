"""Installing plugins from a git repository or a folder, and updating and removing what was installed.

A source is `URL[@REF][#PATH]` (a git URL over https, ssh, git or file, a tag, branch or commit, a folder inside
the repository) or a folder on this machine. Nothing runs before the person's yes: the plugin is fetched into a
staging folder, summarized (preview), and moved into place only by confirm(), which checks that the staged files
are still exactly what was shown and that nothing was installed or updated meanwhile.

Where it goes: ~/.multiverse/plugins/NAME (personal: any universe can turn it on), or a universe's plugins/NAME
(`here`). The installer records what it placed (source, ref, commit, folder, content fingerprint, powers), puts a
marker file with the install's id inside the plugin, and pins the fingerprint in trusted-plugins.json before the
files arrive. A personal plugin with any of these three is verified and run like a trusted universe plugin, so
losing one never leaves it running unpinned. Update and remove act only on a folder whose marker matches the
record. Only the person installs, updates or removes a plugin: never an agent.

From git, nothing is checked out: the plugin's folder is read from git's tree objects, each folder on the way to
it must be a folder, and only plain files are written, byte for byte, so no link, submodule, attribute or filter
in the repository acts before the person's yes.
"""
import json
import os
import re
import secrets as tokens
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import manifest as manifests, trust
from ..store import lock, now, read, retry_missing, write

RECORD = "plugin-installs.json"
STAGING = "plugin-staging"
MARKER = ".mv2-installed.json"  # inside an installed plugin: which install placed it (part of its fingerprint)
STALE_HOURS = 24  # a staged preview nobody confirmed or cancelled, and a removed-* folder older versions left
FETCH_SECONDS = 600
SCHEMES = ("https://", "ssh://", "git://", "file://")
# git URL protocols the installer lets git use: never ext:: (runs a command) or others a URL could name.
PROTOCOLS = ("https", "ssh", "git", "file")
KEY = re.compile(r"[0-9a-f]{16}")
SCRATCH = re.compile(r"(?:look-|removed-)?[0-9a-f]{16}")  # what the installer itself makes in staging, and sweeps
BUSY = re.compile(r"busy-[0-9a-f]{16}-[0-9a-f]{8}")  # a preview a confirm or cancel claimed
RECOVERY = "plugin-recovery"  # an old version a failed rollback, or a removal, couldn't put back: never swept
KEPT = "kept-"  # a folder remove set aside until it commits: matches neither SCRATCH nor BUSY, so never swept
MARKER_BYTES = 4096
BLOBS = {"100644": 0o644, "100755": 0o755}


class InstallError(ValueError):
    """A plugin can't be installed, updated or removed; the message says why and what to do."""


def visible(text):
    """`text` with control and formatting characters written out (\\x1b, \\u202e), so text from a source can't
    redraw or hide what the person reads before saying yes."""
    text = str(text)
    return "".join(character if character.isprintable() else character.encode("unicode_escape").decode("ascii")
                   for character in text)


class Source:
    """What `install` was given: kind 'git' (location is the URL) or 'folder' (an absolute path), the ref asked
    for and the folder inside the repository, each or None."""

    def __init__(self, kind, location, ref=None, path=None):
        self.kind, self.location, self.ref, self.path = kind, location, ref, path

    def text(self):
        return self.location + (f"@{self.ref}" if self.ref else "") + (f"#{self.path}" if self.path else "")


def parse(text):
    """A Source from what the person typed: a folder on this machine, else `URL[@REF][#PATH]`."""
    if not isinstance(text, str) or not text.strip():
        raise InstallError("mv2 plugins install takes a git URL or a plugin's folder")
    text = text.strip()
    if text.startswith("-") or not text.isprintable():
        raise InstallError(f"{visible(text)} is not a git URL or a folder")
    folder = Path(text).expanduser()
    if "://" not in text and folder.is_dir():
        return Source("folder", str(folder.resolve()))
    location, _, path = text.partition("#")
    if "://" in location:
        scheme = location.split("://", 1)[0].lower()
        if scheme + "://" not in SCHEMES:
            raise InstallError(f"{scheme}:// is not a URL the installer fetches: use https://, ssh://, git:// or "
                               f"file://")
        rest = location.index("://") + 3
        start = location.index("/", rest) if "/" in location[rest:] else len(location)
    elif re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:", location):  # git@host:owner/repo.git
        start = location.index(":") + 1
    else:
        raise InstallError(f"{text} is neither a folder here nor a git URL (https://, ssh://, git@host:path, "
                           f"git:// or file://)")
    ref = None
    if "@" in location[start:]:  # the ref follows the first @ inside the URL's path
        at = location.index("@", start)
        location, ref = location[:at], location[at + 1:]
        if not ref or ref.startswith("-") or any(character.isspace() for character in ref):
            raise InstallError(f"@{ref} is not a tag, branch or commit")
    return Source("git", location, ref, _subfolder(path) if path else None)


def _subfolder(path):
    parts = path.split("/")
    if path.startswith("/") or any(part in ("", ".", "..") or part.lower() == ".git" for part in parts):
        raise InstallError(f"#{path} must name a folder inside the repository")
    return "/".join(parts)


def _git(*args, cwd=None, binary=False):
    """Run git with only the URL protocols the installer allows, never a terminal prompt, never a hook."""
    allowed = [option for protocol in PROTOCOLS for option in ("-c", f"protocol.{protocol}.allow=always")]
    command = ["git", "-c", "protocol.allow=never", *allowed, "-c", f"core.hooksPath={os.devnull}", *args]
    try:
        return subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=not binary,
                              timeout=FETCH_SECONDS, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except FileNotFoundError:
        raise InstallError("git is needed to install a plugin from a URL, and it isn't on PATH") from None
    except subprocess.TimeoutExpired:
        raise InstallError(f"git took longer than {FETCH_SECONDS} s; nothing was installed") from None


def _first_line(result):
    output = result.stderr or result.stdout or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    lines = [line for line in output.splitlines() if line.strip()]
    return visible(lines[-1].strip()) if lines else f"exit status {result.returncode}"


def _resolve(repository, ref):
    """(commit, follows, branch) for `ref` in a fresh clone: follows is 'tag', 'branch', 'commit' or 'default'."""
    def commit_of(name):
        found = _git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{name}^{{commit}}", cwd=repository)
        return found.stdout.strip() if found.returncode == 0 else None
    if ref is None:
        branch = _git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=repository).stdout.strip() or None
        commit = commit_of("HEAD")
        if commit is None:
            raise InstallError("the repository has no commits on its default branch")
        return commit, "default", branch
    for prefix, follows in (("refs/tags/", "tag"), ("refs/remotes/origin/", "branch")):
        commit = commit_of(prefix + ref)
        if commit:
            return commit, follows, ref if follows == "branch" else None
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", ref):
        commit = commit_of(ref)
        if commit:
            return commit, "commit", None
    raise InstallError(f"there is no tag, branch or commit named {visible(ref)} in the repository")


def newest_tag(repository):
    tags = _git("-c", "versionsort.suffix=-", "tag", "--list", "--sort=-v:refname", cwd=repository)
    names = [line.strip() for line in tags.stdout.splitlines() if line.strip()]
    return names[0] if names else None


def _clone(url, into):
    result = _git("clone", "--quiet", "--no-checkout", "--no-recurse-submodules", "--", url, str(into))
    if result.returncode != 0:
        raise InstallError(f"git could not fetch {visible(url)}: {_first_line(result)}")
    return into


def _tree(repository, tree):
    """{name: (mode, object)} of one tree object, read literally (no pathspec, no working tree)."""
    listed = _git("ls-tree", "-z", "--end-of-options", tree, cwd=repository)
    if listed.returncode != 0:
        raise InstallError(f"git could not read the repository: {_first_line(listed)}")
    found = {}
    for item in filter(None, listed.stdout.split("\0")):
        details, _, name = item.partition("\t")  # MODE TYPE OBJECT<TAB>NAME
        mode, _, rest = details.partition(" ")
        found[name] = (mode, rest.split(" ")[-1])
    return found


def _materialize(repository, commit, path, into):
    """Write the plugin folder `path` of `commit` to `into` from git's objects: every folder on the way must be a
    folder; only plain files, byte for byte; a link or a submodule anywhere in it refuses the install."""
    found = _git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}^{{tree}}", cwd=repository)
    tree = found.stdout.strip()
    if found.returncode != 0 or not tree:
        raise InstallError(f"git could not read the commit {commit[:12]}")
    walked = []
    for part in (path.split("/") if path else ()):
        mode, tree = _tree(repository, tree).get(part, (None, None))
        walked.append(part)
        if mode != "040000":
            raise InstallError(f"there is no folder {visible('/'.join(walked))} in the repository at {commit[:12]}"
                               + (" (it is a link)" if mode == "120000" else ""))
    if _tree(repository, tree).get(manifests.FILE, ("",))[0] not in BLOBS:
        where = f"#{visible(path)}" if path else "the repository's top"
        raise InstallError(f"{where} has no {manifests.FILE} at {commit[:12]}: it isn't a plugin")
    listed = _git("ls-tree", "-r", "-t", "-z", "--end-of-options", tree, cwd=repository)
    if listed.returncode != 0:
        raise InstallError(f"git could not read the repository: {_first_line(listed)}")
    into.mkdir()
    root = into.resolve()
    for item in filter(None, listed.stdout.split("\0")):
        details, _, name = item.partition("\t")
        mode, _, rest = details.partition(" ")
        parts = name.split("/")
        if any(part in ("", ".", "..") or part.lower() == ".git" for part in parts):
            raise InstallError(f"{visible(name)} is not a file name a plugin can have")
        if trust._skipped(name):
            continue
        if mode == "120000":
            raise InstallError(f"{visible(name)} is a symbolic link: a plugin can't contain links")
        if mode == "160000":
            raise InstallError(f"{visible(name)} is a git submodule, which the installer doesn't fetch")
        target = into / name
        if not str(target.resolve()).startswith(str(root) + os.sep):
            raise InstallError(f"{visible(name)} would land outside the plugin")
        if mode == "040000":
            target.mkdir(parents=True, exist_ok=True)
            continue
        if mode not in BLOBS:
            raise InstallError(f"{visible(name)} is not a plain file (git mode {visible(mode)})")
        blob = _git("cat-file", "blob", rest.split(" ")[-1], cwd=repository, binary=True)
        if blob.returncode != 0:
            raise InstallError(f"git could not read {visible(name)}: {_first_line(blob)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, BLOBS[mode])
        with os.fdopen(descriptor, "wb") as out:
            out.write(blob.stdout)


def _ignore(directory, names):
    return [name for name in names if name in trust.SKIPPED or name in trust.SKIPPED_FILES
            or name.endswith(trust.SKIPPED_SUFFIXES)]


def _copy(source, target):
    """Copy a plugin's files (not .git or caches) to `target`; a link anywhere is refused."""
    for top, directories, files in os.walk(source):
        directories[:] = [name for name in directories if name not in trust.SKIPPED]
        for name in directories + files:
            if os.path.islink(os.path.join(top, name)):
                raise InstallError(f"{visible(os.path.relpath(os.path.join(top, name), source))} is a symbolic "
                                   f"link: a plugin can't contain links")
    shutil.copytree(source, target, symlinks=True, ignore=_ignore)


class Place:
    """Where an install goes: personal (~/.multiverse) or a universe's own (`root`)."""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None
        base = trust.user_directory() if root is None else self.root / "state"
        self.plugins = (trust.user_directory() if root is None else self.root) / "plugins"
        self.record, self.staging, self.lock = base / RECORD, base / STAGING, base / ".plugin-installs.lock"
        self.recovery = base / RECOVERY
        # Taken only to write the record, so its existence shows the record was written before (read_map).
        self.writes = base / ".plugin-installs.write.lock"

    @property
    def kind(self):
        return "personal" if self.root is None else "universe"

    def target(self, name):
        manifests.check_name(name)
        return self.plugins / name

    def records(self, sure=False):
        """{name: record}; InstallError when the record can't be read, so nothing is decided without it. A record
        missing though written before reads as none (gone for good: the installer then refuses any folder it
        didn't place), unless `sure`, for deciding what may run: then it is Uncertain."""
        try:
            data = read_map(self.record, self.writes)
        except FileNotFoundError:
            return {}
        except Uncertain:
            if sure:
                raise
            return {}
        except (OSError, ValueError) as error:
            raise InstallError(f"{self.record} can't be read ({error}): restore it, or remove it and reinstall "
                               f"what it named") from None
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(plugins, dict):
            raise InstallError(f"{self.record} can't be read: restore it, or remove it and reinstall what it named")
        good = {}
        for name, record in plugins.items():
            try:
                manifests.check_name(name)
            except ValueError:
                if sure:
                    raise Uncertain(f"{self.record} names a plugin no installer could: restore it") from None
                continue
            if isinstance(record, dict):
                good[name] = record
            elif sure:  # a damaged entry may be the only sign left of an install
                raise Uncertain(f"{self.record}: the entry for {name} is damaged: restore it")
        return good

    def save(self, records):
        with lock(self.writes):
            write(self.record, {"schema": 1, "plugins": records})

    def check_folders(self):
        """The plugins and staging folders must be real folders: a link would put files somewhere else."""
        for folder in (self.plugins, self.staging):
            if folder.is_symlink():
                raise InstallError(f"{folder} is a symbolic link: the installer only works in a real folder")
            folder.mkdir(parents=True, exist_ok=True)


def place(root=None, here=False):
    if here and root is None:
        raise InstallError("--here needs a universe: run it inside one, or name it with -u")
    return Place(root if here else None)


def pinned(root=None):
    """{resolved plugin directory: record} for what the installer recorded, personal and in `root`."""
    found = {}
    for where in ([Place()] + ([Place(root)] if root is not None else [])):
        try:
            records = where.records()
        except InstallError:
            continue
        for name, record in records.items():
            found[str(where.target(name).resolve())] = {**record, "name": name, "placement": where.kind}
    return found


class Uncertain(InstallError):
    """A map that was written before reads as missing even after its replacement retries."""


def read_map(path, written):
    """A JSON map that is normally absent (nothing installed or trusted yet), replaced whole when written:
    FileNotFoundError when it was never written. Retried through a replacement only when `written`, the lock its
    writers take, shows it has been written before, or the file is seen there right after the miss; still
    missing then is Uncertain, never plain absence. Otherwise absence is the plain answer, read once and never
    waited for."""
    try:
        found = read(path)
    except FileNotFoundError:
        # Absent unless something says otherwise: its writers' lock, or the file itself seen there now.
        if not (written.exists() or path.exists()):
            raise
        try:
            found = retry_missing(lambda: read(path), path=path)
        except FileNotFoundError:
            raise Uncertain(f"{path} is missing though it was written before: restore it") from None
    if not written.exists():  # written before its writers' lock existed (this branch's early installs): now it does
        try:
            written.touch()
        except OSError:
            pass
    return found


def _pins():
    """The paths trusted-plugins.json pins, read as trust._records reads. A map that isn't there pins nothing, even
    one written before (every trust writer reads it so too); a gap while it's replaced is retried only when its
    writers' lock is there. One that's there but can't be read: InstallError."""
    try:  # a map never written is never waited for
        return trust._records(strict=True, wait=trust.written_before())
    except trust.MapUnreadable as error:
        raise InstallError(str(error)) from None


def managed_personal(candidates=None):
    """(directories, certain): the personal plugin directories the installer placed, by any of its three marks
    (its record, its pin in trusted-plugins.json, the marker inside the plugin), so losing the others never
    leaves an installed plugin running unpinned. The maps are read here, directly. When either can't be read for
    sure (damaged, or the record missing though written before), every personal folder counts as installed, so each is
    verified rather than run as placed by hand, and `certain` is False: nothing may keep that answer. With
    `candidates` (the directories a listing found), nothing is read unless one is in the personal plugins
    folder: then nothing there can be an install."""
    place = Place()
    base = place.plugins.resolve()
    personal = [directory for directory in (candidates or ()) if in_personal(directory)]
    if candidates is not None and not personal:
        return set(), True
    certain = True
    try:
        found = {str(base / name) for name in place.records(sure=True)}
        prefix = str(base) + os.sep
        found.update(path for path in _pins() if path.startswith(prefix))
    except InstallError:
        found, certain = {str(entry_path(directory)) for directory in personal}, False
    try:
        children = list(place.plugins.iterdir())
    except OSError:
        children = []
    found.update(str(entry_path(child)) for child in children if marked(child))
    # And where each resolves: a linked install found by another spelling (MV2_PLUGIN_PATH naming its target) is
    # the same folder, verified the same way.
    for entry in list(found):
        try:
            found.add(str(Path(entry).resolve()))
        except (OSError, RuntimeError):
            pass
    return found, certain


def entry_path(directory):
    """A folder as its own entry in its parent (the parent resolved, its name kept): a link moved elsewhere keeps
    the place, and the identity, it has in the personal plugins."""
    directory = Path(directory)
    return directory.parent.resolve() / directory.name


def in_personal(directory):
    """Whether a folder sits in the personal plugins, as an entry there or once resolved."""
    base = Place().plugins.resolve()
    return entry_path(directory).parent == base or Path(directory).resolve().parent == base


def marker(directory):
    """The install marker inside `directory`, read only as a small regular file (never through a link, never
    waiting on a special file), or None."""
    import stat
    try:
        descriptor = os.open(Path(directory) / MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        data = os.read(descriptor, MARKER_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(data) > MARKER_BYTES:
        return None
    try:
        found = json.loads(data)
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def marked(directory):
    """Whether `directory` carries an install marker: read when it is discovered, so an install placed after
    the records were read is still known as one."""
    return marker(directory) is not None


def _owned(target, record):
    """Whether `target` is the folder the install `record` placed: a real folder whose marker names it."""
    if target.is_symlink() or not target.is_dir():
        return False
    found = marker(target)
    return found is not None and record.get("install") is not None and found.get("install") == record["install"]


def _sweep(where):
    """Remove what the installer itself left in staging longer than STALE_HOURS: nothing else. Under the
    placement lock, as every disposal is, so it never meets a confirm using its stage."""
    if where.staging.is_symlink():
        return
    with lock(where.lock):
        try:
            children = list(where.staging.iterdir())
        except OSError:
            return
        for child in children:
            try:
                ours = SCRATCH.fullmatch(child.name) or (
                    # a confirm that died: taken unless it holds an old version a rollback kept
                    BUSY.fullmatch(child.name) and not (child / "replaced").exists())
                if ours and not child.is_symlink() and child.is_dir() \
                        and time.time() - child.lstat().st_mtime > STALE_HOURS * 3600:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass


def _powers(found):
    """What a plugin can do, as a change in it would need the person's yes: access, permissions, extension
    points, what it adds to harness launches, Python packages."""
    from . import packages
    try:
        requirements = [line.text for line in packages.declared(found)[0]]
    except packages.PackagesError as error:
        raise InstallError(str(error)) from None
    return {"access": {"request": found.access.request, "minimum": found.access.minimum},
            "permissions": sorted(found.permissions), "extends": sorted(found.extends),
            "harnesses": _launches(found), "packages": requirements}


def _launches(found):
    """Everything a plugin adds to harness launches, as JSON, paths relative to the plugin: a change in any of
    it (an entry, its folder, its version bounds, needs_bridge, Codex fields) is a change in what it can do."""
    result = {}
    for family, entries in sorted(found.harnesses.items()):
        result[family] = [{"min_version": list(entry.min_version) if entry.min_version else None,
                           "max_version": list(entry.max_version) if entry.max_version else None,
                           "path": Path(entry.path).as_posix() if entry.path else None,  # plugin-relative
                           "needs_bridge": entry.needs_bridge, "fields": entry.fields} for entry in entries]
    return json.loads(json.dumps(result, sort_keys=True, default=str))


def _runs(directory):
    """What would run: its Python files, Claude hooks (claude/), and compiled files a reviewer can't read."""
    directory = Path(directory)
    python = sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*.py")
                    if not trust._skipped(path.relative_to(directory)))
    return {"python": python, "claude": (directory / "claude").is_dir(), "compiled": trust.compiled(directory)}


def _clash(root, name, where, own):
    """Why `name` can't go into `where`: another plugin of that name, or a folder already there not installed."""
    from . import loader
    target = where.target(name)
    if own is None and (target.exists() or target.is_symlink()):
        return f"{target} is already there and wasn't installed by mv2 plugins install; move it away first"
    for source, directory in loader._source_directories(root):
        try:
            found = manifests.load(directory)
        except manifests.ManifestError:
            continue
        if found.name == name and Path(directory).resolve() != target.resolve():
            return f"a {source} plugin named {name} is already at {directory}"
    from . import submodules
    if name in submodules():
        return f"{name} is a plugin this mv2 checkout provides: install it with mv2 plugins install {name}"
    return None


def preview(text, root=None, here=False, *, _update=None):
    """Fetch and stage what `text` names, and say what it is; nothing is installed. The returned `key` confirms
    exactly these files (confirm) or drops them (cancel)."""
    source = parse(text) if _update is None else _update["source"]
    where = place(root, here) if _update is None else _update["place"]
    where.check_folders()
    _sweep(where)
    key = tokens.token_hex(8)
    stage = where.staging / key
    stage.mkdir()
    try:
        plugin = stage / "plugin"
        commit = follows = branch = changes = None
        if source.kind == "folder":
            if not (Path(source.location) / manifests.FILE).is_file():
                raise InstallError(f"{visible(source.location)} has no {manifests.FILE}: it isn't a plugin")
            _copy(source.location, plugin)
        else:
            repository = _clone(source.location, stage / "repository")
            commit, follows, branch = _resolve(repository, source.ref)
            _materialize(repository, commit, source.path, plugin)
            if _update is not None and _update["record"].get("commit"):
                log = _git("log", "--oneline", "--no-decorate", "--end-of-options",
                           f"{_update['record']['commit']}..{commit}", cwd=repository)
                changes = [visible(line) for line in log.stdout.splitlines()] if log.returncode == 0 else None
            shutil.rmtree(repository, ignore_errors=True)
        try:
            found = manifests.load(plugin)
        except manifests.ManifestError as error:
            raise InstallError(f"{visible(source.text())} isn't a usable plugin: {visible(error)}") from None
        if _update is not None and found.name != _update["name"]:
            raise InstallError(f"the source now holds a plugin named {found.name}, not {_update['name']}")
        if _update is None:
            own = where.records().get(found.name)
            if own is not None:
                raise InstallError(f"{found.name} is already installed from {visible(own.get('source'))}: update "
                                   f"it with mv2 plugins update {found.name}, or remove it first")
            problem = _clash(where.root, found.name, where, own)
            if problem:
                raise InstallError(problem)
        install = tokens.token_hex(8)
        write(plugin / MARKER, {"install": install, "name": found.name, "source": source.text(), "commit": commit})
        value = trust.fingerprint(plugin)
        summary = {
            "key": key, "install": install, "name": found.name, "version": found.version,
            "description": found.description, "source": source.text(), "kind": source.kind,
            "location": source.location, "ref": source.ref, "path": source.path, "commit": commit,
            "follows": follows, "branch": branch, "fingerprint": value, "placement": where.kind,
            "target": str(where.target(found.name)), "universe": str(where.root) if where.root is not None else None,
            "powers": _powers(found), "runs": _runs(plugin), "update": None, "prior": None,
            "on_here": _on_here(root, found.name)}
        if _update is not None:
            old = _update["record"]
            installed = where.target(found.name)
            try:
                edited = trust.current(installed) != old.get("fingerprint")
            except (OSError, ValueError):
                edited = True
            mine = summary["powers"]
            summary["prior"] = old
            summary["update"] = {
                "from": {"commit": old.get("commit"), "version": old.get("version"), "source": old.get("source")},
                "changes": changes, "same": _same(plugin, installed) and not edited, "edited": edited,
                "powers_changed": sorted(name for name in mine if mine[name] != (old.get("powers") or {}).get(name))}
        write(stage / "preview.json", summary)
        return summary
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _on_here(root, name):
    """Whether universe `root`'s plugins.json already turns `name` on (left from before): it then runs there as
    soon as it's placed."""
    if root is None:
        return False
    from . import config
    try:
        return bool(config.load(root, observed=True).enabled.get(name))
    except (OSError, ValueError):
        return False


def _same(staged, installed):
    """Whether the staged plugin has the installed one's files, the marker aside (each install has its own)."""
    def files(directory):
        try:
            return {relative.as_posix(): path.read_bytes() for relative, path in trust._files(directory)
                    if relative.as_posix() != MARKER}
        except (OSError, ValueError):
            return None
    mine = files(staged)
    return mine is not None and mine == files(installed)


def _places(root):
    return [Place()] + ([Place(root)] if root is not None else [])


def _where(key, root):
    if not isinstance(key, str) or not KEY.fullmatch(key):
        return None
    return next((where for where in _places(root) if not where.staging.is_symlink()
                 and (where.staging / key / "preview.json").is_file()), None)


def _claim(where, key):
    """Take preview `key` for one use, under the placement lock: renamed out of reach of any other confirm,
    cancel or sweep. The claimed folder, or None when it's gone."""
    with lock(where.lock):
        stage = where.staging / key
        if stage.is_symlink() or not stage.is_dir():
            return None
        claimed = where.staging / f"busy-{key}-{tokens.token_hex(4)}"
        os.replace(stage, claimed)
        os.utime(claimed)  # claimed now: a day's grace from here before any sweep may take it
        return claimed


def cancel(key, root=None):
    """Drop a preview; True when there was one."""
    where = _where(key, root)
    claimed = _claim(where, key) if where is not None else None
    if claimed is None:
        return False
    shutil.rmtree(claimed, ignore_errors=True)
    return True


def confirm(key, root=None):
    """Install (or update to) exactly the staged files of preview `key`; the person's yes."""
    where = _where(key, root)
    stage = _claim(where, key) if where is not None else None
    if stage is None:
        raise InstallError("that preview is gone (cancelled, used, or left over a day): fetch it again")
    staged, keep = stage / "plugin", False
    try:
        summary = read(stage / "preview.json")
        value, raw = trust.fingerprint_with(staged)  # its event types from exactly the bytes checked here
        if value != summary["fingerprint"]:
            raise InstallError("the fetched files changed after they were shown, so nothing was installed: "
                               "fetch them again")
        name, target = summary["name"], where.target(summary["name"])
        where.check_folders()
        with lock(where.lock):
            records = where.records()
            own = records.get(name)
            if summary["update"] is None:
                if own is not None:
                    raise InstallError(f"{name} was installed meanwhile: update it with mv2 plugins update {name}")
                problem = _clash(where.root, name, where, None)
                if problem:
                    raise InstallError(problem)
            else:
                if own != summary["prior"]:
                    raise InstallError(f"{name} changed since this update was shown (updated or removed): look at "
                                       f"it again")
                if not _owned(target, own):
                    raise InstallError(f"{target} isn't the folder mv2 plugins install placed (moved or replaced "
                                       f"since): nothing was updated")
            previous = trust._records().get(str(target.resolve()))
            # Pinned before the files arrive: in between, the old files read as changed, never as unpinned. With
            # the event types of the staged bytes the fingerprint was just checked on.
            trust.pin(target, summary["fingerprint"], trust.events_of(staged, raw))
            old = stage / "replaced"
            placed = replaced = False
            try:
                if target.exists():
                    os.replace(target, old)
                    replaced = True  # there was a previous version, whatever any pin says
                os.replace(staged, target)
                placed = True
                stamp = now()
                records[name] = {
                    "install": summary["install"], "source": summary["source"], "kind": summary["kind"],
                    "location": summary["location"], "ref": summary["ref"], "path": summary["path"],
                    "commit": summary["commit"], "follows": summary["follows"], "branch": summary["branch"],
                    "version": summary["version"], "fingerprint": summary["fingerprint"], "powers": summary["powers"],
                    "installed_at": own.get("installed_at", stamp) if own else stamp, "updated_at": stamp}
                where.save(records)
            except BaseException as error:
                try:  # put back exactly what was there: the old folder, its pin, and no record of the new one
                    if placed:
                        os.replace(target, stage / "failed")
                    if old.exists():
                        os.replace(old, target)
                except OSError as rollback:
                    kept = old
                    try:  # out of staging, where nothing removes it
                        where.recovery.mkdir(parents=True, exist_ok=True)
                        moved = where.recovery / f"{name}-{tokens.token_hex(4)}"
                        os.replace(old, moved)
                        kept = moved  # named only once it is there
                    except OSError:
                        keep = True
                    raise InstallError(f"{name} could not be placed ({visible(error)}) nor put back as it was "
                                       f"({rollback}): the previous version is in {kept}") from error
                try:
                    if isinstance(previous, dict):
                        trust.pin(target, previous["fingerprint"], previous.get("events"))
                    else:
                        trust.forget(target)
                except OSError as pinning:
                    # The files are back; the pin still names the new ones, so the old ones read as changed and
                    # don't run until someone reviews them: never the other way round.
                    raise InstallError(
                        f"{name} could not be placed ({visible(error)}) "
                        + (f"and its pin couldn't be put back ({pinning}): it reads as changed until you review it "
                           f"and run mv2 plugins trust {name}; the previous version is in {target}"
                           if replaced else
                           f"and its pin for {target} couldn't be cleared ({pinning}); nothing was installed")
                    ) from error
                raise
        from . import loader
        loader._changed()
        return {"name": name, "version": summary["version"], "commit": summary["commit"], "target": str(target),
                "placement": where.kind, "updated": summary["update"] is not None,
                "packages": summary["powers"]["packages"]}
    finally:
        if not keep:
            shutil.rmtree(stage, ignore_errors=True)


def _installed(name, root, here, placement=None):
    """(Place, record) of what the installer placed for `name`: exactly `placement` ('personal' or 'universe')
    when given, refusing when that one has none; else the universe's with `here`, else personal, else the
    universe's."""
    if placement is not None:
        if placement not in ("personal", "universe"):
            raise InstallError(f"placement is personal or universe, not {visible(placement)}")
        where = Place() if placement == "personal" else place(root, True)
        record = where.records().get(name)
        if not isinstance(record, dict):
            raise InstallError(f"there is no {placement} install of {name} any more: look at it again")
        return where, record
    for where in ([place(root, True)] if here else _places(root)):
        record = where.records().get(name)
        if isinstance(record, dict):
            return where, record
    return None, None


def _as_shown(name, record, expected):
    """Refuse when the record differs from what the person was shown (`expected`: fields of a listing row's
    install), so a remove never acts on an install that changed since."""
    if expected is not None and any(record.get(key) != value for key, value in expected.items()):
        raise InstallError(f"{name} changed since it was shown (updated or reinstalled): look at it again")


def update_preview(name, ref=None, root=None, here=False, placement=None):
    """Fetch what `name` would update to and say what changes: to `ref`, else its newest tag (a tag install), its
    branch's tip (a branch or default-branch install). `ask` says whether the person must say yes: when its
    access, permissions, extension points, harness launches or Python packages change."""
    where, record = _installed(name, root, here, placement)
    if record is None:
        raise InstallError(_not_installed(name, root))
    if not _owned(where.target(name), record):
        raise InstallError(f"{where.target(name)} isn't the folder mv2 plugins install placed (moved or replaced "
                           f"since): reinstall it, or remove the record with mv2 plugins remove {name}")
    location, path = record.get("location"), record.get("path")
    if record.get("kind") == "folder":
        if ref is not None:
            raise InstallError(f"{name} was installed from the folder {visible(location)}, which has no refs")
        source = Source("folder", location)
    else:
        target_ref = ref
        if target_ref is None:
            follows = record.get("follows")
            if follows == "commit":
                raise InstallError(f"{name} is pinned to commit {str(record.get('commit'))[:12]}: name what to "
                                   f"update to, as mv2 plugins update {name}@REF")
            if follows == "tag":
                with _scratch(where) as scratch:
                    target_ref = newest_tag(_clone(location, scratch / "repository"))
                if target_ref is None:
                    raise InstallError(f"{visible(location)} has no tags any more")
            elif follows == "branch":
                target_ref = record.get("branch")
        source = Source("git", location, target_ref, path)
    summary = preview(None, root, here, _update={"source": source, "place": where, "record": record, "name": name})
    # Installed files changed since the install: what runs now isn't what the record describes, so there's no
    # verified baseline to tell a code-only update by. Ask.
    summary["ask"] = bool(summary["update"]["powers_changed"]) or summary["update"]["edited"]
    write(where.staging / summary["key"] / "preview.json", summary)
    return summary


class _scratch:
    def __init__(self, where):
        where.check_folders()
        self.path = where.staging / ("look-" + tokens.token_hex(8))

    def __enter__(self):
        self.path.mkdir()
        return self.path

    def __exit__(self, *_):
        shutil.rmtree(self.path, ignore_errors=True)


def _not_installed(name, root):
    from . import loader
    for source, directory in loader._source_directories(root):
        try:
            if manifests.load(directory).name == name:
                return (f"{name} at {directory} wasn't installed by mv2 plugins install ({source}): remove it by "
                        f"hand if you mean to")
        except manifests.ManifestError:
            continue
    return f"no plugin named {name} was installed by mv2 plugins install"


def remove(name, root=None, here=False, placement=None, expected=None):
    """Remove what the installer placed for `name`, and its record; return what's left elsewhere. A folder at the
    recorded place that isn't the one it placed (moved or replaced since) is left alone, and with `expected` (a
    listing row's install), an install that changed since it was shown is too."""
    where, record = _installed(name, root, here, placement)
    if record is None:
        raise InstallError(_not_installed(name, root))
    _as_shown(name, {**record, "placement": where.kind}, expected)
    target = where.target(name)
    folder = None
    try:  # its package folder, found from its requirements while they are still there
        from . import packages
        folder = packages.folder(manifests.load(target))
    except (manifests.ManifestError, ValueError, OSError):
        pass
    where.check_folders()
    gone = False
    with lock(where.lock):
        records = where.records()
        if records.get(name) != record:
            raise InstallError(f"{name} changed meanwhile: look at it again")
        aside = None
        if target.exists() or target.is_symlink():
            if not _owned(target, record):
                raise InstallError(f"{target} isn't the folder mv2 plugins install placed (moved or replaced "
                                   f"since): nothing was removed")
            # Out of the way under a name no sweep matches, whole until the removal commits (an interrupted
            # remove, or one that can't drop the pin nor put it back, never loses it to a later sweep).
            aside = where.staging / f"{KEPT}{name}-{tokens.token_hex(4)}"
            try:
                os.replace(target, aside)
            except OSError as error:
                raise InstallError(f"its folder couldn't be moved out of the way ({visible(str(error))})") from None
        else:
            gone = True
        try:
            trust.forget(target)
        except OSError as error:  # its pin can't be dropped (trusted-plugins.json can't be read): remove nothing
            if aside is not None:
                try:
                    os.replace(aside, target)
                except OSError:
                    kept = _keep_unswept(where, name, aside)
                    raise InstallError(f"its pin couldn't be dropped ({visible(str(error))}), and its folder "
                                       f"couldn't be put back: it is in {kept}") from None
            raise InstallError(f"its pin couldn't be dropped: {visible(str(error))}") from None
        del records[name]
        try:
            where.save(records)
        except OSError as error:  # its pin is gone but its record stays: put the folder back for that record
            # The same removal, wherever it is run from: a bare one takes a personal install first.
            again = (f"mv2 -u {shlex.quote(str(where.root))} plugins remove {name} --here" if where.kind == "universe"
                     else f"mv2 plugins remove {name}")
            cause = f"its record couldn't be saved ({visible(str(error))})"
            if aside is None:
                raise InstallError(f"{cause}; its folder was already gone: run {again} again to drop the "
                                   f"record") from None
            try:
                os.replace(aside, target)
            except OSError:
                kept = _keep_unswept(where, name, aside)
                raise InstallError(f"{cause}; its files are kept in {kept}: to keep {name}, move them back to "
                                   f"{target} (unpinned until you approve it again); to finish removing it, run "
                                   f"{again} (the files stay)") from None
            raise InstallError(f"{cause}; its folder is back in {target}, unpinned, so it doesn't run: run {again} "
                               f"again") from None
        if aside is not None:
            shutil.rmtree(aside, ignore_errors=True)  # committed: only now is the folder gone
    from . import loader
    loader._changed()
    return {"name": name, "removed": str(target), "placement": where.kind, "already_gone": gone,
            "left": leftovers(name, root, folder)}


def _keep_unswept(where, name, folder):
    """Move `folder` (already under a staging name no sweep matches) to plugin-recovery, where a failed update's
    old version goes too; where it ends up, `folder` itself when that move can't be made."""
    try:
        where.recovery.mkdir(parents=True, exist_ok=True)
        moved = where.recovery / f"{name}-{tokens.token_hex(4)}"
        os.replace(folder, moved)
        return moved
    except OSError:
        return folder


def leftovers(name, root, folder):
    """What a removed plugin leaves, each with what clears it: never removed by `remove`."""
    from . import config, packages, secrets, settings as settings_files
    left = []
    if root is not None:
        settings = config.load(root, observed=True)
        named = [what for what, there in (("turns it on", settings.enabled.get(name)),
                                          (f"grants it {settings.access.get(name)}", settings.access.get(name)))
                 if there]
        if named:
            left.append({"what": f"this universe's plugins.json still {' and '.join(named)}",
                         "clear": f"mv2 plugins disable {name}"})
        file = settings_files.path(root, name)
        if file.is_file():
            left.append({"what": f"its settings in {file}", "clear": f"rm {file}"})
        state = Path(root) / "state" / "plugins" / name
        if state.is_dir():
            left.append({"what": f"its state in {state}", "clear": f"rm -r {state}"})
    try:
        index = secrets._read(secrets.directory() / secrets.INDEX)
    except (OSError, ValueError):
        index = {}
    universes = sorted(universe for universe, plugins in index.items()
                       if isinstance(plugins, dict) and plugins.get(name))
    here = secrets._universe_id(root) if root is not None else None
    for key in sorted(index.get(here, {}).get(name, ())) if here in universes else ():
        left.append({"what": f"its secret {key} in this universe", "clear": f"mv2 plugins secret {name} {key} --clear"})
    others = [universe for universe in universes if universe != here]
    if others:
        keys = sorted({key for universe in others for key in index[universe][name]})
        left.append({"what": f"its secrets ({', '.join(keys)}) in {len(others)} other "
                             f"{'universe' if len(others) == 1 else 'universes'}",
                     "clear": f"mv2 -u UNIVERSE plugins secret {name} KEY --clear, in each"})
    if folder is not None and folder.is_dir():
        left.append({"what": f"its Python packages in {folder} (removed after {packages.UNUSED_DAYS} days unused)",
                     "clear": f"rm -r {folder}"})
    return left
