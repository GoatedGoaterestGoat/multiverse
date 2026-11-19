"""A plugin's Python packages (1.20): what its manifest's "python" names, installed only with the person's ok.

A plugin can need packages mv2 doesn't have. Its manifest names a requirements file, as
`uv pip compile --generate-hashes` writes one: every package pinned to one version (`==`) with the
SHA-256 of each file it may install. Installing one runs nothing (only wheels are taken), but importing it
runs its code inside mv2 with the plugin's own trust, so mv2 installs them only when the person says yes:
`mv2 plugins enable NAME` or `mv2 plugins packages NAME` lists each one and asks. Loading never installs.

They go in a folder of the plugin's own, `~/.multiverse/plugin-packages/<key>/`, never into mv2's
environment, which `mv2 update` (`uv sync --locked`) would empty, and where a plugin's package could replace
one mv2 uses. The key is a digest of the plugin's name, its requirements file and this Python, so a changed
list, or another Python, is a folder of its own that needs the person's ok again. When the plugin loads, its
folder goes on the import path after mv2's own packages, so mv2's always win:
- a package mv2 has at the same version is left out of the folder, and at another version refused;
- one process holds every loaded plugin, so a package another loaded plugin has at another version refuses
  the second plugin's load, naming the first.
A folder nothing used for UNUSED_DAYS is removed; disabling a plugin keeps its folder for when it is enabled
again.
"""
from dataclasses import dataclass
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

DIRECTORY = "plugin-packages"
COMPLETE = ".mv2-packages.json"  # written last: a folder without it is a staging folder or a failed install
UNUSED_DAYS = 14
STAGING_HOURS = 1  # a staging folder older than this was left by an install that died
INSTALL_SECONDS = 600
# Where the installer looks for packages: its own configuration (PyPI unless the person set another index).
# Tests point it at a folder of local wheels, so they never reach the network.
INDEX = ()
# One exact version: no wildcard (1.*), no second clause (1.0,!=1.0.1), no extras ([extra] needs dependencies,
# which --no-deps leaves out anyway).
_LINE = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*==\s*([0-9][0-9A-Za-z.!+_-]*)\s*(?:;(.*))?")
# An environment marker, token by token (PEP 508): nothing else, so no installer option can hide after the `;`.
_MARKER_VARIABLES = {"python_version", "python_full_version", "os_name", "sys_platform", "platform_release",
                     "platform_system", "platform_version", "platform_machine", "platform_python_implementation",
                     "implementation_name", "implementation_version"}
_MARKER_TOKEN = re.compile(r"\s*(?:(===|==|!=|<=|>=|~=|<|>)|(\(|\))|'([A-Za-z0-9 ._*+!-]*)'|\"([A-Za-z0-9 ._*+!-]*)\"|"
                           r"([A-Za-z_]+))")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")
_active = {}  # normalized package name -> (plugin, version), for the folders on this process's import path
_active_lock = threading.RLock()  # held across a load's check and its publishing


class PackagesError(ValueError):
    """A plugin's packages can't be used or installed; the message says why and what fixes it."""


@dataclass(frozen=True)
class Requirement:
    name: str
    version: str
    hashes: tuple
    marker: str | None = None

    @property
    def text(self):
        return f"{self.name}=={self.version}" + (f" ; {self.marker}" if self.marker else "")


def normal(name):
    """A package name as Python packaging compares them: case, runs of -, _ and . don't matter."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(text, where="the requirements file"):
    """The requirements in `text`, each pinned with == and hashed; PackagesError naming the line otherwise."""
    logical, pending = [], ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        pending += line
        if pending.strip():
            logical.append(pending.strip())
        pending = ""
    if pending.strip():
        logical.append(pending.strip())
    found, seen = [], set()
    for line in logical:
        if line.startswith("-"):
            raise PackagesError(f"{where}: {line.split()[0]} isn't allowed: list each package as NAME==VERSION "
                                f"with its --hash=sha256:... lines")
        head = line.split("--hash", 1)[0].strip()
        match = _LINE.fullmatch(head)
        if match is None:
            raise PackagesError(f"{where}: {head!r} must pin one exact version, as NAME==VERSION")
        name, version, marker = match.groups()
        if marker is not None:
            marker = marker.strip()
            if not _marker(marker):
                raise PackagesError(f"{where}: {name}: {marker!r} isn't an environment marker such as "
                                    f"python_version < \"3.12\"")
        hashes = tuple(_HASH.findall(line))
        rest = _HASH.sub("", line[len(head):]).strip()
        if rest:
            raise PackagesError(f"{where}: {name}: only --hash=sha256:HEX may follow the version, not {rest!r}")
        if not hashes:
            raise PackagesError(f"{where}: {name}=={version} has no --hash=sha256:...; generate the file with "
                                f"uv pip compile --generate-hashes")
        if normal(name) in seen:
            raise PackagesError(f"{where}: {name} is listed twice")
        seen.add(normal(name))
        found.append(Requirement(name, version, hashes, marker or None))
    return tuple(found)


def _marker(text):
    """Whether `text` is made only of an environment marker's tokens: variables, comparisons, quoted values,
    and, or, not, in and parentheses, with a value or variable on each side of each comparison."""
    position, tokens = 0, []
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _MARKER_TOKEN.match(text, position)
        if match is None or match.end() == position:
            return False
        operator, paren, single, double, word = match.groups()
        if word is not None and word not in _MARKER_VARIABLES | {"and", "or", "not", "in"}:
            return False
        tokens.append("value" if single is not None or double is not None or word in _MARKER_VARIABLES
                      else operator and "compare" or paren or word)
        position = match.end()
    # value (compare|in|not in) value, joined by and/or, in balanced parentheses
    depth, expect = 0, "value"
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if expect == "value":
            if token == "(":
                depth += 1
            elif token == "value":
                following = tokens[index + 1:index + 3]
                if following[:1] == ["compare"] or following[:1] == ["in"]:
                    step = 2
                elif following == ["not", "in"]:
                    step = 3
                else:
                    return False
                if index + step >= len(tokens) or tokens[index + step] != "value":
                    return False
                index += step
                expect = "joiner"
            else:
                return False
        else:
            if token == ")" and depth > 0:
                depth -= 1
            elif token in ("and", "or"):
                expect = "value"
            else:
                return False
        index += 1
    return bool(tokens) and depth == 0 and expect == "joiner"


def requirements_path(manifest):
    """The plugin's requirements file, or None when it declares no packages."""
    return Path(manifest.directory) / manifest.python if getattr(manifest, "python", None) else None


def declared(manifest):
    """(requirements, the file's bytes): () and b"" when the plugin declares no packages."""
    path = requirements_path(manifest)
    if path is None:
        return (), b""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PackagesError(f"{manifest.name}: {path}: {error.strerror or error}") from None
    return parse(data.decode("utf-8", "replace"), str(path)), data


def root(home=None):
    from .trust import user_directory
    return (Path(home) if home is not None else user_directory()) / DIRECTORY


def key(manifest, data):
    """The folder's name: a digest of the plugin's name, its requirements file and this Python."""
    python = f"{sys.implementation.cache_tag}-{sys.platform}-{os.uname().machine if hasattr(os, 'uname') else ''}"
    return hashlib.sha256(b"\0".join((manifest.name.encode(), data, python.encode()))).hexdigest()[:32]


def folder(manifest, home=None):
    """Where the plugin's packages are installed, or None when it declares none."""
    requirements, data = declared(manifest)
    return root(home) / key(manifest, data) if requirements else None


def mv2_packages():
    """{normalized name: version} of the packages mv2's own environment has (plugin folders left out)."""
    base = root()
    paths = [entry for entry in sys.path if entry and DIRECTORY not in Path(entry).parts and Path(entry) != base]
    found = {}
    for distribution in importlib.metadata.distributions(path=paths):
        name = distribution.metadata.get("Name")
        if name and normal(name) not in found:
            found[normal(name)] = distribution.version
    return found


def state(manifest, home=None):
    """What the plugin declares and whether it is installed here: {"packages": [text], "installed", "folder",
    "problem"}; None when it declares no packages."""
    try:
        requirements, data = declared(manifest)
    except PackagesError as error:
        return {"packages": [], "installed": False, "folder": None, "problem": str(error)}
    if not requirements:
        return None
    where = root(home) / key(manifest, data)
    keep, conflict = _conflicts(manifest, requirements)
    if not keep and conflict is None:
        # Every one is mv2's own, at that version: nothing would be installed, so there is nothing to agree to.
        return {"packages": [requirement.text for requirement in requirements], "installed": True,
                "folder": str(where), "from_mv2": True, "problem": None}
    installed = (where / COMPLETE).is_file() and not _stale(manifest, where / COMPLETE)
    return {"packages": [requirement.text for requirement in requirements], "installed": installed,
            "folder": str(where),
            "problem": None if installed else (f"{manifest.name} needs Python packages that aren't installed here: "
                                               f"run mv2 plugins packages {manifest.name} to review and install them")}


def _conflicts(manifest, requirements):
    """(the requirements to install, the problem) against mv2's own: one mv2 has at the same version is left out
    (mv2's copy is used), one at another version refuses the install."""
    mine, keep = mv2_packages(), []
    for requirement in requirements:
        theirs = mine.get(normal(requirement.name))
        if theirs is None:
            keep.append(requirement)
        elif theirs != requirement.version:
            return None, (f"{manifest.name} needs {requirement.name}=={requirement.version}, but mv2 itself uses "
                          f"{requirement.name} {theirs}: a plugin can't replace a package mv2 uses")
    return keep, None


def installer():
    """The install command's start: uv when it is on PATH, else this Python's pip; None when there is neither."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable]
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
    return None


def install(manifest, home=None, approved=None):
    """Install the plugin's packages into its folder, only ever called once the person said yes. `approved` is the
    folder name of the list the person was shown: when the file changed since, nothing installs.

    Into a staging folder first, renamed into place when the installer finished and the folder is complete, so a
    failure (the network, a hash that doesn't match, a package with no wheel) leaves nothing half done. Returns
    {"folder", "installed": [text], "skipped": [text mv2 already has]}."""
    requirements, data = declared(manifest)
    if not requirements:
        return {"folder": None, "installed": [], "skipped": []}
    target = root(home) / key(manifest, data)
    if approved is not None and target.name != approved:
        raise PackagesError(f"{manifest.name}'s requirements changed after you were shown them, so nothing was "
                            f"installed: run mv2 plugins packages {manifest.name} to see the new list")
    if (target / COMPLETE).is_file():
        if not _stale(manifest, target / COMPLETE):
            return {"folder": str(target), "installed": [], "skipped": [], "already": True}
        shutil.rmtree(target, ignore_errors=True)  # what it left out as mv2's has changed: install it again
    keep, problem = _conflicts(manifest, requirements)
    if problem:
        raise PackagesError(problem)
    command = installer()
    if command is None:
        raise PackagesError(f"{manifest.name}'s packages can't be installed: neither uv nor pip is available to "
                            f"this Python ({sys.executable})")
    base = root(home)
    base.mkdir(parents=True, exist_ok=True)
    prune(home)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{target.name}-", dir=base))
    try:
        if keep:
            listed = staging / ".requirements.txt"
            listed.write_text("".join(f"{r.name}=={r.version}" + (f" ; {r.marker}" if r.marker else "")
                                      + "".join(f" --hash=sha256:{digest}" for digest in r.hashes) + "\n"
                                      for r in keep))
            packages = staging / "site"
            # Wheels only, each matching one of its hashes, nothing the list doesn't name: what installs is exactly
            # what the person approved, and installing runs no package's code.
            result = subprocess.run([*command, "--target", str(packages), "--require-hashes", "--no-deps",
                                     "--only-binary", ":all:", *INDEX, "-r", str(listed)],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=INSTALL_SECONDS)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip().splitlines()
                raise PackagesError(f"{manifest.name}'s packages could not be installed: "
                                    + ("\n".join(detail[-8:]) or f"the installer exited {result.returncode}"))
            listed.unlink()
        else:
            (staging / "site").mkdir()
        record = {"plugin": manifest.name, "packages": [r.text for r in requirements],
                  "installed": [r.text for r in keep],
                  "skipped": [r.text for r in requirements if r not in keep], "at": time.time()}
        (staging / COMPLETE).write_text(json.dumps(record, indent=2) + "\n")
        try:
            os.rename(staging, target)
        except OSError:
            if not (target / COMPLETE).is_file():  # another install of the same list finished first: keep it
                raise
            shutil.rmtree(staging, ignore_errors=True)
        return {"folder": str(target), "installed": record["installed"], "skipped": record["skipped"]}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def problem(manifest, home=None):
    """Why the plugin's packages can't be used in this process now, without loading it: None when they can, or when
    it declares none. What listings, info and doctor say, and what activate refuses with."""
    return _check(manifest, home)[0]


def _check(manifest, home=None):
    """(problem or None, its site folder or None, {normalized: (name, version)} installed there)."""
    try:
        requirements, data = declared(manifest)
    except PackagesError as error:
        return str(error), None, {}
    if not requirements:
        return None, None, {}
    target = root(home) / key(manifest, data)
    marker = target / COMPLETE
    if not marker.is_file():
        keep, conflict = _conflicts(manifest, requirements)
        if not keep and conflict is None:
            return None, None, {}  # every one is mv2's own, at that version: it uses mv2's
        return conflict or state(manifest, home)["problem"], None, {}
    site = target / "site"
    found = {}
    for distribution in importlib.metadata.distributions(path=[str(site)]):
        name = distribution.metadata.get("Name")
        if name:
            found[normal(name)] = (name, distribution.version)
    mine = mv2_packages()  # as mv2's environment is now: an update since the install may have changed it
    for name, (shown, version) in found.items():
        if mine.get(name) not in (None, version):
            return (f"{manifest.name} needs {shown} {version}, but mv2 itself now uses {shown} {mine[name]}: a "
                    f"plugin can't replace a package mv2 uses, so it needs a release that pins "
                    f"{shown}=={mine[name]}"), site, found
    stale = _stale(manifest, marker, mine, requirements)
    if stale:
        return stale, site, found
    with _active_lock:
        for name, (shown, version) in found.items():
            other = _active.get(name)
            if other is not None and other[1] != version:
                if other[0] == manifest.name:
                    return (f"{manifest.name} now needs {shown} {version}, but this process already loaded {shown} "
                            f"{other[1]} for it: restart it (the dashboard, or mv2 plugins host stop and start) to "
                            f"use the new one"), site, found
                return (f"{manifest.name} needs {shown} {version}, but {other[0]}, loaded first, has {shown} "
                        f"{other[1]}: plugins share one Python, so turn one of them off"), site, found
    return None, site, found


def _stale(manifest, marker, mine=None, requirements=None):
    """Why the packages left out of the folder because mv2 had them no longer hold, or None. What was left out is
    the plugin's own list less what the install record says it installed; the record's own list of what it left
    out is never trusted. If mv2 now has another version of one, or none, the plugin would get a package it wasn't
    pinned to. A record that can't say what it installed means installing again."""
    try:
        record = json.loads(marker.read_text())
    except (OSError, ValueError):
        record = None
    installed = record.get("installed") if isinstance(record, dict) else None
    if not isinstance(installed, list) or not all(isinstance(line, str) for line in installed):
        return (f"{manifest.name}'s install record in {marker.parent} can't be read: run mv2 plugins packages "
                f"{manifest.name} to install its packages again")
    if requirements is None:
        requirements = declared(manifest)[0]
    mine = mv2_packages() if mine is None else mine
    for requirement in requirements:
        if requirement.text in installed:
            continue
        now = mine.get(normal(requirement.name))
        if now != requirement.version:
            return (f"{manifest.name}'s {requirement.name}=={requirement.version} was left out as mv2's own, but mv2 "
                    f"now has {requirement.name + ' ' + now if now else 'none'}: run mv2 plugins packages "
                    f"{manifest.name} to install its packages again")
    return None


def activate(manifest, home=None):
    """Put the plugin's installed packages on this process's import path, after mv2's own, before it loads.
    PackagesError with problem()'s reason when they can't be used here now. The check and the publishing are one
    step under the lock, so two plugins loading at once can't both pass with two versions of one package."""
    with _active_lock:
        why, site, found = _check(manifest, home)
        if why:
            raise PackagesError(why)
        if site is None:
            return None
        if str(site) not in sys.path:
            sys.path.append(str(site))
            importlib.invalidate_caches()
        for name, (_, version) in found.items():
            _active.setdefault(name, (manifest.name, version))
    try:
        os.utime(site.parent / COMPLETE)  # used now: kept past UNUSED_DAYS
    except OSError:
        pass
    return str(site)


def clashes(manifests, home=None):
    """{plugin: why} for plugins whose installed folders hold one package at two versions: they can't both load in
    one process, whichever loads first, so each says so, wherever it is listed."""
    found = {}  # normalized package -> {version: [plugin]}
    for manifest in manifests:
        try:
            requirements, data = declared(manifest)
        except PackagesError:
            continue
        if not requirements:
            continue
        site = root(home) / key(manifest, data) / "site"
        if not (site.parent / COMPLETE).is_file():
            continue
        for distribution in importlib.metadata.distributions(path=[str(site)]):
            name = distribution.metadata.get("Name")
            if name:
                found.setdefault(normal(name), {}).setdefault(distribution.version, []).append((manifest.name, name))
    problems = {}
    for versions in found.values():
        if len(versions) < 2:
            continue
        sides = sorted((version, plugin, shown) for version, plugins in versions.items() for plugin, shown in plugins)
        for version, plugin, shown in sides:
            others = ", ".join(f"{other} needs {shown} {theirs}" for theirs, other, _ in sides if other != plugin)
            problems.setdefault(plugin, f"{plugin} needs {shown} {version}, but {others}: plugins share one Python, "
                                        f"so they can't both load; turn one of them off")
    return problems


def prune(home=None):
    """Remove folders nothing used for UNUSED_DAYS, and staging folders an install that died left behind."""
    base, now = root(home), time.time()
    try:
        children = list(base.iterdir())
    except OSError:
        return []
    removed = []
    for child in children:
        try:
            if child.name.startswith(".staging-"):
                old = now - child.stat().st_mtime > STAGING_HOURS * 3600
            else:
                marker = child / COMPLETE
                old = now - (marker.stat().st_mtime if marker.is_file() else child.stat().st_mtime) > UNUSED_DAYS * 86400
        except OSError:
            continue
        if old and str(child / "site") not in sys.path:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    return removed
