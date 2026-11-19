"""A plugin's secrets (1.20): API keys and tokens it declares, kept outside every universe and shown to no one.

A plugin declares them in its manifest (`"secrets": {"api_key": {"label", "help", "required"}}`), apart from
its settings, so the settings code never shows or saves one. The person sets each with
`mv2 plugins secret NAME KEY`, from a hidden prompt or stdin, never an argument, or on the Plugins screen.

Where the values go (the user's choice, 2026-11-19): the OS keychain, macOS Keychain through `security` or the
Linux Secret Service through `secret-tool`, and where neither is there, a file only the user can read,
`~/.multiverse/plugin-secrets/secrets.json` (0600). Each value is the universe's own: an entry is named by the
universe's id, the plugin and the key. A subverse with no value of its own reads its nearest parent's, live;
nothing is ever copied. Disabling a plugin keeps its values, which only clearing removes.

`index.json` beside the file store says which are set, and where, never a value: whether a secret is set is
read from it, so listing and the screen never open the keychain. A value reaches a keychain tool on its stdin,
never in its argv, where `ps` would show it; and no error, log line or record here ever holds one.

Only the plugin that declared a secret reads it, through `universe.secret(key)` in the plugin API, while it is
on and trusted there; nothing puts one in an agent's environment.
"""
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

DIRECTORY = "plugin-secrets"
INDEX = "index.json"
FILE = "secrets.json"
SERVICE = "mv2 plugin secret"
STORE = "MV2_SECRET_STORE"  # "file" or "keychain": which store to use, else chosen by the system (tests: file)
KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
UNIVERSE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")  # as mv2 writes it
SAFE = re.compile(r"[A-Za-z0-9 _.:-]*")  # what may go between the quotes of a `security -i` command line
TOOL_SECONDS = 30
MAX_BYTES = 64 * 1024


class SecretsError(ValueError):
    """A secret can't be read, set or cleared; the message never holds a value."""


def directory(home=None):
    from .trust import user_directory
    return (Path(home) if home is not None else user_directory()) / DIRECTORY


def _private_write(path, data):
    """Write `data` to `path`, readable only by the user (0600 in a 0700 folder), replacing it in one step."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read(path):
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise SecretsError(f"{path} can't be read ({type(error).__name__}); it holds no other record") from None
    return value if isinstance(value, dict) else {}


@contextmanager
def _lock(home=None):
    """The store's exclusive lock, on one descriptor: opened without waiting on a special file or following a
    link, checked to be a regular file, made the user's alone (an older one is fixed), then locked."""
    folder = directory(home)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / ".lock"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):  # a link, a folder or a special file there
            raise SecretsError(f"{path} isn't a regular file, so it can't be the store's lock; remove it") from None
        # Anything else (out of descriptors, say) says nothing against the lock, which another process may hold.
        raise SecretsError(f"{path}, the store's lock, can't be opened ({error.strerror}); try again") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SecretsError(f"{path} isn't a regular file, so it can't be the store's lock; remove it")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def store_name():
    """Which store new values go to: "keychain" (macOS, or Linux with a Secret Service session) or "file"."""
    chosen = os.environ.get(STORE)
    if chosen in ("file", "keychain"):
        return chosen
    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain"
    if sys.platform.startswith("linux") and shutil.which("secret-tool") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return "keychain"
    return "file"


def _account(universe_id, plugin, key):
    """The keychain entry's name. Each part is checked: the universe id is a UUID, the plugin's name and the key
    are a manifest's, so none can carry a quote or a line into a keychain tool's input."""
    account = f"{universe_id}:{plugin}:{key}"
    if not UNIVERSE_ID.fullmatch(universe_id) or not KEY.fullmatch(key) or not SAFE.fullmatch(account):
        raise SecretsError("the universe's id, the plugin's name or the key isn't one mv2 can name a secret by")
    return account


def _tool(arguments, stdin=None, what="the keychain"):
    """Run a keychain tool, the value only ever on its stdin. Its own output is never shown: a tool may echo what
    it was given."""
    try:  # stdin is always the tool's own pipe, never the terminal mv2 runs in
        with subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as process:
            try:
                out, err = process.communicate(stdin or "", timeout=TOOL_SECONDS)
            except BaseException:  # its time limit, Ctrl+C or anything else: the tool never outlives the call
                process.kill()
                process.wait()
                raise
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SecretsError(f"{what} didn't answer: {type(error).__name__}") from None
    return subprocess.CompletedProcess(arguments, process.returncode, out, err)


def _keychain_set(account, value, label):
    label = re.sub(r"[^A-Za-z0-9 _.:-]", "", label)[:80]  # the manifest's: never anything that could end the quote
    if sys.platform == "darwin":
        # Through `security -i`, so the value is on its stdin, not in any process's arguments. Stored as the hex of
        # its UTF-8, so no quoting can change it and reading it back is exact.
        for part in (account, label):
            if not SAFE.fullmatch(part):
                raise SecretsError("a name for the keychain entry holds characters it can't take")
        command = (f'add-generic-password -U -s "{SERVICE}" -a "{account}" -l "{label}" '
                   f'-w "{value.encode().hex()}"\n')
        result = _tool(["security", "-i"], command, "macOS Keychain")
        if result.returncode != 0 or "error" in (result.stderr or "").lower():
            raise SecretsError(f"macOS Keychain refused to store it ({_reason(result)})")
        return
    result = _tool(["secret-tool", "store", f"--label={label}", "service", SERVICE, "account", account],
                   value.encode().hex(), "the Secret Service")
    if result.returncode != 0:
        raise SecretsError(f"the Secret Service refused to store it ({_reason(result)})")


def _keychain_get(account):
    if sys.platform == "darwin":
        result = _tool(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                       what="macOS Keychain")
    else:
        result = _tool(["secret-tool", "lookup", "service", SERVICE, "account", account], what="the Secret Service")
    if result.returncode != 0:
        return None
    try:
        return bytes.fromhex(result.stdout.strip()).decode()
    except ValueError:
        raise SecretsError("the keychain entry isn't one mv2 wrote; clear it and set it again") from None


def _keychain_clear(account):
    """Remove the entry; SecretsError when the tool refused, so nothing is reported cleared that is still kept."""
    if sys.platform == "darwin":
        result = _tool(["security", "delete-generic-password", "-s", SERVICE, "-a", account], what="macOS Keychain")
        gone = result.returncode in (0, 44)  # 44: there is no such entry, which is what clearing wants
    else:
        result = _tool(["secret-tool", "clear", "service", SERVICE, "account", account], what="the Secret Service")
        gone = result.returncode == 0
    if not gone:
        raise SecretsError(f"the keychain refused to remove it ({_reason(result)}); it is still kept and still "
                           f"recorded as set")


def _reason(result):
    """What may be said of a tool's failure: its exit status only. Its output is never repeated, since a tool may
    echo the value it was given."""
    return f"exit status {result.returncode}"


def _universe_id(root):
    from ..store import read
    try:
        found = read(Path(root) / "universe.json")["id"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SecretsError(f"{root}: universe.json can't be read, so its secrets can't be found: "
                           f"{type(error).__name__}") from None
    if not isinstance(found, str) or not UNIVERSE_ID.fullmatch(found):
        raise SecretsError(f"{root}: universe.json's id isn't one mv2 wrote, so its secrets can't be named")
    return found


def _chain(root):
    """This universe, then each parent up to the top: where a secret is looked for, nearest first."""
    from .. import universe as universes
    found, current = [], Path(root)
    while current is not None:
        found.append(current)
        current = universes.parent(current)
    return found


def set_secret(root, plugin, key, value, label=None, home=None):
    """Keep `value` as this universe's `key` of `plugin`; it replaces one set before. Returns the store's name."""
    if not isinstance(value, str) or not value:
        raise SecretsError("the value is empty; to remove one, clear it")
    if len(value.encode()) > MAX_BYTES or "\0" in value:
        raise SecretsError(f"the value is too long (over {MAX_BYTES // 1024} KiB) or holds a NUL byte")
    universe_id, store = _universe_id(root), store_name()
    account = _account(universe_id, plugin, key)
    with _lock(home):
        index_path, file_path = directory(home) / INDEX, directory(home) / FILE
        index = _read(index_path)
        previous = index.get(universe_id, {}).get(plugin, {}).get(key)
        if store == "keychain":
            _keychain_set(account, value, label or f"mv2: {plugin} {key}")
        else:
            values = _read(file_path)
            values.setdefault(universe_id, {}).setdefault(plugin, {})[key] = value
            _private_write(file_path, values)
        left = None
        if previous and previous.get("store") != store:
            try:
                _remove(previous.get("store"), account, file_path, universe_id, plugin, key)
            except SecretsError as error:
                left = error  # the new value is the one recorded; the old copy is said, below
        from ..store import now
        index.setdefault(universe_id, {}).setdefault(plugin, {})[key] = {"store": store, "at": now(),
                                                                         "universe": str(Path(root).resolve())}
        _private_write(index_path, index)
    if left is not None:
        raise SecretsError(f"it is set, but the old copy in the {previous.get('store')} wasn't removed: {left}")
    return store


def _remove(store, account, file_path, universe_id, plugin, key):
    if store == "keychain":
        _keychain_clear(account)
        return
    values = _read(file_path)
    if values.get(universe_id, {}).get(plugin, {}).pop(key, None) is not None:
        if not values[universe_id][plugin]:
            del values[universe_id][plugin]
        if not values[universe_id]:
            del values[universe_id]
        _private_write(file_path, values)


def clear_secret(root, plugin, key, home=None):
    """Remove this universe's own `key` of `plugin`; a parent's, if it has one, is read again. True if one was set."""
    universe_id = _universe_id(root)
    with _lock(home):
        index_path = directory(home) / INDEX
        index = _read(index_path)
        entry = index.get(universe_id, {}).get(plugin, {}).get(key)
        if entry is None:
            return False
        # Removed from its store first: one the store refused to remove stays recorded as set.
        _remove(entry.get("store"), _account(universe_id, plugin, key), directory(home) / FILE, universe_id,
                plugin, key)
        del index[universe_id][plugin][key]
        if not index[universe_id][plugin]:
            del index[universe_id][plugin]
        if not index[universe_id]:
            del index[universe_id]
        _private_write(index_path, index)
    return True


def where(root, plugin, key, home=None):
    """(the universe whose value is used, from the index alone) or None when none up the chain has one set."""
    index = _read(directory(home) / INDEX)
    for universe in _chain(root):
        try:
            universe_id = _universe_id(universe)
        except SecretsError:
            continue
        if key in index.get(universe_id, {}).get(plugin, {}):
            return universe
    return None


def read(root, plugin, key, home=None):
    """The value of `key` of `plugin` here, or the nearest parent's; None when none is set."""
    index = _read(directory(home) / INDEX)
    for universe in _chain(root):
        try:
            universe_id = _universe_id(universe)
        except SecretsError:
            continue
        entry = index.get(universe_id, {}).get(plugin, {}).get(key)
        if entry is None:
            continue
        if entry.get("store") == "keychain":
            value = _keychain_get(_account(universe_id, plugin, key))
        else:
            value = _read(directory(home) / FILE).get(universe_id, {}).get(plugin, {}).get(key)
        if value is None:
            raise SecretsError(f"{plugin}'s {key} is recorded as set but its value is gone from the "
                               f"{entry.get('store')}; set it again")
        return value
    return None


def orphans(root, declared, home=None):
    """This universe's own secrets whose plugin no longer declares them: [(plugin, key)]. `declared` maps each
    plugin found here to the keys its manifest declares."""
    index = _read(directory(home) / INDEX)
    mine = index.get(_universe_id(root), {})
    return sorted((plugin, key) for plugin, keys in mine.items() for key in keys
                  if key not in declared.get(plugin, ()))
