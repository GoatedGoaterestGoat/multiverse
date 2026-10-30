"""unittest helpers for plugin developers, and for mv2's own plugin tests.

    from multiverse.plugins.testing import PluginTestCase

    class MyPluginTest(PluginTestCase):
        plugin = Path(__file__).parents[1]      # the directory with plugin.json

        def test_column(self):
            column = self.loaded().registrations.columns["uptime"]
            self.assertEqual(column.value({"name": "worker"}), "—")

Each test gets a temporary universe made from the base template, a temporary
home (so ~/.multiverse is never the developer's), a temporary Claude config
directory (CLAUDE_CONFIG_DIR), a bin/ directory first on PATH for fake
executables, no inherited agent identity, and `plugin` found through
MV2_PLUGIN_PATH and turned on. Nothing here starts an agent, tmux or a native
harness: `agent()` writes an agent's records and `executable()` a fake program.
"""
import atexit
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

from . import config, loader
from .. import plugins
from ..universe import templates

def plugin_module(directory, name):
    """A module of the plugin at `directory` (`name` such as "engine.store"), imported as mv2 imports it:
    one copy per process, with no universe and whether or not the plugin is on anywhere (API 1.9).

    For test modules that import a plugin's own modules at module level, before
    any test's universe exists. Its relative imports work as they do in mv2."""
    directory = Path(directory).resolve()
    # Exactly this directory, as a developer's (MV2_PLUGIN_PATH) plugin: no plugin discovery, which would scan
    # installed packages and cache that in the home of whatever runs it (the real one, at a test module's import).
    from .manifest import load as load_manifest
    if not (directory / "plugin.json").is_file():
        raise FileNotFoundError(f"{directory / 'plugin.json'}: no such plugin")
    manifest = load_manifest(directory)
    found = loader._entry(None, manifest.name, False, "path", directory, manifest, [])
    return importlib.import_module(f"{loader.load(found, off=True).package}.{name}")


# The guard: no test writes under the real ~/.multiverse, ~/.claude or ~/.codex, to ~/.claude.json, or under the
# .multiverse of the mv2 checkout it runs from. An audit hook (in native_safety's style) refuses
# writes, renames, removals, directories and mode changes there with PermissionError, so the real home is never
# changed, and records each one, so the test fails (check(), which PluginTestCase calls when it ends) or, for a
# thread that outlived its test, the process exits 1 at its end.

_protected = set()  # absolute directories no test may change
_protected_files = set()  # absolute files no test may change, nor the copies named after them (FILE.tmp…)
_harness = set()  # those of them that are Claude's or Codex's home, not mv2's: no bytecode exemption there
_exempt = set()  # directories inside them that are not mv2's state: the plugin under test (PluginTestCase.plugin)
_violations = []  # {"path", "event", "test", "thread", "unreported"}
_current = None  # the test running now
_installed = False
_strict = False  # whether writes no check() reported make the process exit 1 (the default once installed)
_TEST = "_mv2_test"  # on a Thread: the test it was started in (threads it starts carry it on)
_open_context = threading.local()  # the dir_fd of the os.open call running in this thread: its audit event lacks it
_WRITES = {"os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.chmod", "os.utime", "os.link", "os.symlink",
           "os.truncate", "shutil.rmtree", "shutil.copyfile", "shutil.move"}
_REAL_HOME = os.path.join(os.path.expanduser("~"), ".multiverse")  # captured at import, before a test moves HOME
# The user's real Claude Code and Codex homes, captured the same way: tests run the harnesses only in homes of their
# own, so a write here would change the person's real Claude or Codex state.
_HARNESS_HOMES = [os.path.join(os.path.expanduser("~"), name) for name in (".claude", ".codex")]
# Claude Code's account and per-folder trust state, a file beside ~/.claude, which it writes by way of temporary and
# backup copies beside it.
_HARNESS_FILES = [os.path.join(os.path.expanduser("~"), ".claude.json")]


def _real_run_directory():
    """The person's run folder (MV2_RUN_DIR, else ~/.multiverse/run), where the live tmux server and the agents'
    native sockets are, as mv2 finds it; None for a relative MV2_RUN_DIR, which mv2 itself refuses."""
    from ..runtime import paths
    try:
        return str(paths.run_directory())
    except ValueError:
        return None


_RUN_DIR = _real_run_directory()  # captured at import, as the homes above

# The universe of the mv2 checkout this module runs from (src/multiverse/plugins/testing.py), when it runs from
# one: there, it is the universe agents run in, which a test finding a universe from its directory would change.
_SOURCE = Path(__file__).resolve().parents[3]
_CHECKOUT_STATE = str(_SOURCE / ".multiverse") if (_SOURCE / "src" / "multiverse").is_dir() else None


def install(home=None, strict=True):
    """Guard this process (once; later calls add directories): no test writes under `home`, by default the real
    ~/.multiverse, ~/.claude, ~/.claude.json (and its ~/.claude.json.* copies) and ~/.codex as they were when this
    module was imported, and the .multiverse of the mv2 checkout this module runs from. Returns the directory now
    protected (the real ~/.multiverse by default).

    A refused write counts against the test it came from: the test running then, or for a thread the test that
    started it. Strict (the default): writes that no check() reported (no test named itself, or a thread outlived
    its test) make the process exit 1 at its end, after they are printed. strict=False only prints them; it never
    makes a process that another caller installed strict lenient again."""
    global _installed, _strict
    _strict = _strict or strict
    homes = [home] if home is not None else [_REAL_HOME, *_HARNESS_HOMES] + [each for each in (_CHECKOUT_STATE,
                                                                                               _RUN_DIR) if each]
    for each in homes:
        given = os.path.normpath(os.path.abspath(each))
        _protected.update({given, os.path.realpath(given)})  # as written and as resolved (/tmp, /var are links)
        if home is None and each in _HARNESS_HOMES:
            _harness.update({given, os.path.realpath(given)})
    for each in _HARNESS_FILES if home is None else []:
        given = os.path.normpath(os.path.abspath(each))
        _protected_files.update({given, os.path.realpath(given)})
    directory = os.path.realpath(os.path.normpath(os.path.abspath(homes[0])))
    if not _installed:
        _installed = True
        start = threading.Thread.start

        def tagged_start(thread, *args, **kwargs):
            setattr(thread, _TEST, getattr(threading.current_thread(), _TEST, None) or _current)
            return start(thread, *args, **kwargs)
        threading.Thread.start = tagged_start
        os.open = _open_with_context(os.open)
        sys.addaudithook(_hook)
        atexit.register(_at_exit)
    return Path(directory)


def running(name):
    """Name the test running now (None between tests): violations say which test they happened in."""
    global _current
    _current = name


def violations():
    """Every write the guard refused since install(), as copies."""
    return [dict(item) for item in _violations]


def check(test=None):
    """Raise AssertionError for refused writes not reported yet, and mark them reported: those of `test` when given
    (what a test's own cleanup asks: never another test's), else all."""
    found = [item for item in _violations if item["unreported"] and (test is None or item["test"] == test)]
    for item in found:
        item["unreported"] = False
    if found:
        raise AssertionError("a test wrote mv2's real state, or the user's Claude or Codex home: under ~/.multiverse, "
                             "~/.claude, ~/.claude.json, ~/.codex or this checkout's .multiverse (refused): "
                             + "; ".join(
            f"{item['event']} {item['path']} in {item['test'] or 'no test'} ({item['thread']})" for item in found))


def _record(event, path):
    thread = threading.current_thread()
    _violations.append({"path": path, "event": event, "test": getattr(thread, _TEST, None) or _current,
                        "thread": thread.name, "unreported": True})


# For each event the guard watches: (index of a path the call changes, index of the directory fd it is relative
# to, or None), from the audit events table. A path read but not changed (a link's target) is not listed.
_TARGETS = {"os.mkdir": [(0, 2)], "os.rmdir": [(0, 1)], "os.remove": [(0, 1)], "os.chmod": [(0, 2)],
            "os.utime": [(0, 3)], "os.rename": [(0, 2), (1, 3)], "os.link": [(1, 3)], "os.symlink": [(1, 2)],
            "os.truncate": [(0, None)], "shutil.rmtree": [(0, 1)], "shutil.copyfile": [(1, None)],
            "shutil.move": [(0, None), (1, None)]}


def _hook(event, args):
    if event == "open":
        if len(args) < 3 or not _writes(args[1], args[2]):
            return
        targets = [(args[0], getattr(_open_context, "dir_fd", None))]
    elif event in ("socket.bind", "socket.connect"):
        # A Unix socket by path: under mv2's own protected state, one the live tmux server or an agent serves on.
        # Claude's and Codex's homes are left to their own guards (native_safety refuses the Codex daemon's
        # socket), so a test proving that one refuses is not counted here as well.
        if len(args) < 2 or getattr(args[0], "family", None) != socket.AF_UNIX or not isinstance(args[1], (str, bytes)):
            return
        if not args[1] or args[1][:1] in ("\0", b"\0"):  # Linux's abstract names are no file
            return
        targets = [(args[1], None)]
    elif event in _TARGETS:
        targets = [(args[at] if at < len(args) else None, args[fd] if fd is not None and fd < len(args) else None)
                   for at, fd in _TARGETS[event]]
    else:
        return
    for path, directory in targets:
        where = _absolute(path, directory)
        if where is None:
            continue
        # As written and as resolved: a link elsewhere may point into the home. realpath resolves the links that
        # exist and keeps a new file's own name, so a file about to be made through one is judged by where it goes.
        for candidate in dict.fromkeys((where, os.path.realpath(where))):
            if event.startswith("socket.") and _in_harness_home(candidate):
                continue
            if _guarded(candidate):
                _record(event, candidate)
                raise PermissionError(f"a test may not change mv2's real state or the user's Claude or Codex home: "
                                      f"{candidate}")


def _in_harness_home(where):
    return any(where == top or where.startswith(top + os.sep) for top in _harness)


def _guarded(where):
    return (any(where == top or where.startswith(top + os.sep) for top in _protected)
            or any(where == name or where.startswith(name + ".") for name in _protected_files)) and not _allowed(where)


def _open_with_context(real):
    """os.open, noting its dir_fd for the length of the call: the "open" audit event carries the name only, so
    the hook would otherwise judge a name relative to a directory descriptor by the working directory."""
    def open(path, flags, mode=0o777, *, dir_fd=None):
        previous = getattr(_open_context, "dir_fd", None)
        _open_context.dir_fd = dir_fd
        try:
            return real(path, flags, mode, dir_fd=dir_fd)
        finally:
            _open_context.dir_fd = previous
    open.__doc__, open.__wrapped__ = real.__doc__, real
    return open


def _allowed(where):
    """Not mv2's state although inside the home: the plugin under test's own directory, and Python's bytecode in
    a personal plugin (~/.multiverse/plugins/NAME), which importing its test modules writes before any test runs;
    never in Claude's or Codex's home, whose plugins folders are theirs."""
    if any(where == top or where.startswith(top + os.sep) for top in _exempt):
        return True
    parts = where.split(os.sep)
    return ("__pycache__" in parts or where.endswith((".pyc", ".pyo"))) and any(
        where.startswith(os.path.join(top, "plugins") + os.sep) for top in _protected - _harness)


def _writes(mode, flags):
    if isinstance(mode, str):
        return any(letter in mode for letter in "wax+")
    return isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))


def _absolute(path, directory=None):
    """The absolute path a call changes: relative to `directory` (a dir_fd, as shutil.rmtree passes) when given,
    else to the working directory; None when it cannot be told (a file descriptor, or a dir_fd whose path this
    system cannot give), which is then not counted."""
    if isinstance(path, int) or path is None:
        return None
    try:
        path = os.fsdecode(os.fspath(path))
    except TypeError:
        return None
    if os.path.isabs(path):
        return os.path.normpath(path)
    if isinstance(directory, int) and not isinstance(directory, bool) and directory >= 0:  # AT_FDCWD is negative
        base = _fd_path(directory)
        return os.path.normpath(os.path.join(base, path)) if base else None
    return os.path.normpath(os.path.join(os.getcwd(), path))


def _fd_path(fd):
    """The path of an open directory: /proc/self/fd on Linux, F_GETPATH on macOS; None elsewhere or if closed."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        pass
    try:
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0]) or None
    except (OSError, ValueError):
        pass
    return None


def _exit_status(strict=None):
    """1 when a strict process has refused writes no check() reported, else 0."""
    strict = _strict if strict is None else strict
    return 1 if strict and any(item["unreported"] for item in _violations) else 0


def _at_exit():
    found = [item for item in _violations if item["unreported"]]
    if not found:
        return
    print("mv2 tests: writes under the real ~/.multiverse were refused but never reported:", file=sys.stderr)
    for item in found:
        print(f"  {item['event']} {item['path']} in {item['test'] or 'no test'} ({item['thread']})",
              file=sys.stderr)
    if _exit_status():
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


CLEARED = ("MV2_UNIVERSE", "MV2_AGENT_ID", "MV2_AGENT_DIR", "MV2_LAUNCH_ID", "MV2_CONTROL_SOCKET",
           "MV2_TMUX_SOCKET", "MV2_PLUGIN_PATH", "CLAUDE_CONFIG_DIR", "CODEX_HOME", loader.SAFE_MODE)


class ClaudeRows:
    """Claude transcript rows for a fake transcript (1.13): each joined to the one before by parentUuid, with rising
    timestamps, in the shapes Claude Code stores. `prompt` and `command` start a turn; the rest belong to the
    latest one. `rewind(row)` makes the next row a second child of an earlier one, as a rewind does: without a
    tip, the history follows the newest leaf."""

    def __init__(self):
        from datetime import datetime, timezone
        self.rows, self._parent, self._prompt = [], None, None
        self._clock = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def add(self, kind, **fields):
        """A row of `kind` with these fields; uuid, parentUuid, timestamp and the turn's promptId are filled in."""
        from datetime import timedelta
        from uuid import uuid4
        self._clock += timedelta(seconds=1)
        row = {"type": kind, "uuid": str(uuid4()), "parentUuid": self._parent, "timestamp": self._clock.isoformat(),
               "sessionId": "fake", **({"promptId": self._prompt} if self._prompt and kind == "user" else {}),
               **fields}
        self.rows.append(row)
        self._parent = row["uuid"]
        return row

    def prompt(self, text, source="human"):
        from uuid import uuid4
        self._prompt = str(uuid4())
        return self.add("user", message={"role": "user", "content": text}, origin={"kind": source})

    def response(self, text=None, *, tool=None, stop="end_turn"):
        """The model's reply: `text`, or a tool call (`tool` = (call_id, name, input)), with its stop reason."""
        from uuid import uuid4
        block = ({"type": "tool_use", "id": tool[0], "name": tool[1], "input": tool[2]} if tool
                 else {"type": "text", "text": text or ""})
        return self.add("assistant", message={"id": "msg_" + uuid4().hex[:12], "role": "assistant", "model": "claude-test",
                                              "content": [block], "stop_reason": "tool_use" if tool else stop,
                                              "usage": {"input_tokens": 10, "output_tokens": 5}})

    def tool_result(self, call_id, text):
        return self.add("user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": text}]}, toolUseResult={"stdout": text})

    def turn_end(self, milliseconds=1000):
        return self.add("system", subtype="turn_duration", durationMs=milliseconds)

    def interrupted(self, tool=False):
        """An Esc: Claude usually writes no turn_duration then, and this row ends the turn."""
        text = "[Request interrupted by user" + (" for tool use]" if tool else "]")
        return self.add("user", message={"role": "user", "content": [{"type": "text", "text": text}]})

    def steer(self, text, source="peer"):
        """A message the model sees before the running turn ends (a mid-turn steer)."""
        return self.add("attachment", attachment={"type": "queued_command", "commandMode": "prompt", "prompt": text,
                                                  "origin": {"kind": source}})

    def command(self, name, args="", output=""):
        """A typed slash command (name with its /, as "/model"), and its output."""
        from uuid import uuid4
        self._prompt = str(uuid4())
        row = self.add("user", message={"role": "user", "content": f"{name} {args}".strip()})
        self.add("user", message={"role": "user", "content": f"<local-command-stdout>{output}</local-command-stdout>"})
        return row

    def rewind(self, row):
        """The next row hangs off `row`, an earlier one: a second branch."""
        self._parent = row["uuid"]

    def compaction(self, summary="Summary of the conversation so far", stored=True):
        """Claude's own compaction (1.14): its boundary, linked to the row before it, then its summary. With
        `stored=False` the rows before it were never stored here, as when a session continues past a compaction
        in a file of its own, and the history starts at the summary (`page.summary`)."""
        from uuid import uuid4
        before = self._parent if stored else str(uuid4())
        self.add("system", subtype="compact_boundary", parentUuid=None, logicalParentUuid=before,
                 compactMetadata={"trigger": "auto", "preservedMessages": {"uuids": []}})
        return self.add("user", message={"role": "user", "content": summary}, isCompactSummary=True)


class FakeTranscript:
    """A Claude transcript an agent reads, in a PluginTestCase (1.13): stored where the agent's records say its
    Claude storage is, so a test reads it through the real API, core and Claude adapter."""

    def __init__(self, case, agent_dir, rows):
        from uuid import uuid4
        self._case, self.agent_dir = case, Path(agent_dir)
        self.native_id = str(uuid4())
        self._bind(None, self.native_id, None)
        self.rewrite(rows)

    @property
    def path(self):
        from ..harnesses.claude.transcripts import project_key
        from ..store import read
        workspace = read(self.agent_dir / "properties.json")["workspace"]
        return self._case.claude_config / "projects" / project_key(workspace) / f"{self.native_id}.jsonl"

    def _bind(self, previous, native_id, reason):
        from ..store import read, write
        from ..universe import agents
        home = os.path.realpath(self._case.claude_config)
        if previous is None:
            data = read(self.agent_dir / "properties.json")
            data.update(native_session_id=native_id, native_session_home=home)
            write(self.agent_dir / "properties.json", data)
            return
        launch = read(self.agent_dir / "runtime.json")["launch_id"]
        agents.rebind_session(self.agent_dir.parent.parent.parent, self.agent_dir.name, previous, native_id, reason,
                              launch, home=home)

    def append(self, *rows):
        """Rows Claude writes next."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as stream:
            stream.write("".join(json.dumps(row) + "\n" for row in rows))

    def rewrite(self, rows):
        """The same file, its bytes changed (as Claude does when it removes a row): same inode, new content."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w") as stream:
            stream.write("".join(json.dumps(row) + "\n" for row in rows))

    def clear(self, rows=()):
        """A /clear: the agent moves to a new session, the old one stays on disk and in its lineage."""
        from uuid import uuid4
        previous, self.native_id = self.native_id, str(uuid4())
        self._bind(previous, self.native_id, "clear")
        self.rewrite(rows)

    def unreadable(self):
        """The folder that holds the session can't be read: a read must say it can't tell, never 'empty'."""
        folder = self.path.parent
        folder.chmod(0)
        self._case.addCleanup(folder.chmod, 0o755)


class FakeRuns:
    """One-shot runs answered from a script (1.14), for PluginTestCase.fake_runs(). The real run_once runs: the
    API's checks, access and permission, the harness's run options, model check and plan (its message checks
    included) and core's own checks. Only the run is replaced, so nothing is written or spawned. Each run takes
    the next answer: an api.Reply it returns, or an api.RunError it raises. `requests` holds each run's request
    in full, oldest first: a dict of messages (an api.Message, or a row's data as passed), prompt, system, model,
    effort, transport_retries, agent (its directory, or None) and timeout."""

    def __init__(self, test, answers, version="2.1.312"):
        from ..harnesses import types
        from ..runtime import runs
        from . import api
        self.answers, self.requests, calling = list(answers), [], []
        if not (test.bin / "claude").exists():  # what the run options ask the harness's executable; never a session
            test.executable("claude", f"echo '{version} (Claude Code)'")
        original = runs.run_once

        def run_once(adapter, request, **keywords):
            calling.append((request, keywords))
            try:
                return original(adapter, request, **keywords)
            finally:
                calling.pop()

        def execute(executor, plan, payload):
            request, keywords = calling[-1]
            self.requests.append({
                "messages": [item.data if isinstance(item, types.RunRow) else api.Message(item.role, [
                    api.TextBlock(block.text) if isinstance(block, types.RunText)
                    else api.ImageBlock(block.media_type, block.data) for block in item.blocks])
                    for item in request.messages],
                "prompt": request.prompt, "system": request.system, "model": request.model,
                "effort": request.effort, "transport_retries": request.transport_retries,
                "agent": keywords.get("agent_directory"), "timeout": keywords.get("timeout")})
            if not self.answers:
                raise types.RunFailure("failed", "the test's script has no answer left for this run")
            answer = self.answers.pop(0)
            if isinstance(answer, api.RunError):
                raise types.RunFailure(answer.kind, str(answer), retry_after=answer.retry_after,
                                       confirmed=answer.confirmed)
            if not isinstance(answer, api.Reply):
                raise TypeError("fake_runs answers are api.Reply and api.RunError")
            return types.RunReply(text=answer.text, model=answer.model, usage=dict(answer.usage),
                                  cost_usd=answer.cost_usd, stop=answer.stop, lost=tuple(answer.lost))

        for patcher in (mock.patch.object(runs, "run_once", run_once), mock.patch.object(runs, "_execute", execute)):
            patcher.start()
            test.addCleanup(patcher.stop)


class PluginTestCase(unittest.TestCase):
    # The plugin under test: a directory with plugin.json, or None to write plugins in the test.
    plugin = None
    # Whether mv2's bundled plugins (such as typeahead, on by default), the checkout's submodule plugins and
    # pip-installed plugins are found too. Off, a test sees only its own plugins, whatever is installed.
    bundled = False

    def setUp(self):
        super().setUp()
        install()  # before anything here moves HOME; checked last, once everything else is cleaned up
        running(self.id())
        self.addCleanup(check, self.id())
        if self.plugin is not None:  # its own directory is its developer's code, not mv2's state
            own = {os.path.normpath(os.path.abspath(self.plugin)), os.path.realpath(self.plugin)}
            _exempt.update(own)
            self.addCleanup(_exempt.difference_update, own)
        self.addCleanup(running, None)
        temp = tempfile.TemporaryDirectory(prefix="mv2-plugin-test-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name).resolve()
        self.home = self.work / "home"
        self.home.mkdir()
        self.plugins_dir = self.work / "plugins"
        self.plugins_dir.mkdir()
        environment = {key: value for key, value in os.environ.items() if key not in CLEARED}
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.claude_config = self.work / "claude-config"
        self.claude_config.mkdir()
        # TMUX_TMPDIR: where the default tmux server, the live agents', keeps its socket; the test's is its own.
        # MV2_RUN_DIR: the live agents' run folder; the test's own, short enough for its native sockets' paths.
        (self.work / "run").mkdir(mode=0o700)
        environment.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.claude_config), TMUX_TMPDIR=str(self.work),
                           MV2_RUN_DIR=str(self.work / "run"),
                           PATH=os.pathsep.join([str(self.bin), environment.get("PATH", "")]),
                           MV2_PLUGIN_PATH=os.pathsep.join(
                               str(path) for path in (self.plugin, self.plugins_dir) if path is not None))
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        loader.reset()
        self.addCleanup(loader.reset)
        if not self.bundled:
            empty = self.work / "no-bundled-plugins"
            empty.mkdir()
            for patcher in (mock.patch.object(loader, "BUNDLED", empty), mock.patch.object(loader, "CHECKOUT", None),
                            mock.patch.object(loader, "_installed", [])):
                patcher.start()
                self.addCleanup(patcher.stop)
        previous = loader.process
        self.addCleanup(setattr, loader, "process", previous)
        loader.process = "cli"
        self.root = templates.create(self.work / "project" / ".multiverse", "test", workdir=self.work / "project")
        if self.plugin is not None:
            name = json.loads((Path(self.plugin) / "plugin.json").read_text())["name"]
            self.enable(name)

    def write_plugin(self, name, manifest=None, files=None, where=None):
        """A plugin directory NAME with plugin.json and `files` ({relative path: text}); returns it."""
        directory = Path(where or self.plugins_dir) / name
        directory.mkdir(parents=True, exist_ok=True)
        data = {"schema": 1, "name": name, "version": "0.1.0", "api": "1.0", **(manifest or {})}
        (directory / "plugin.json").write_text(json.dumps(data, indent=2) + "\n")
        for relative, text in (files or {}).items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return directory

    def enable(self, name, on=True, root=None):
        return config.set_enabled(root or self.root, name, on)

    def subverse(self, name):
        return templates.create(self.root / "subverses" / name, name, workdir=self.work / "project")

    def entry(self, name, root=None):
        return plugins.entry(root or self.root, name)

    def loaded(self, name=None, root=None):
        """The plugin loaded and registered, as a process that needs it would load it."""
        if name is None:
            name = json.loads((Path(self.plugin) / "plugin.json").read_text())["name"]
        return loader.load(self.entry(name, root))

    def module(self, name):
        """A module of the plugin under test (`plugin`), as mv2 loaded it for this test (API 1.9)."""
        return importlib.import_module(f"{self.loaded().package}.{name}")

    def dashboard(self, size=(120, 40), root=None):
        """The real dashboard on this universe with the plugins really loaded, driven by verbs (API 1.9).

        Use it with `async with` in a test that is also an IsolatedAsyncioTestCase; see
        multiverse.plugins.dashboard_testing for what a test may rely on."""
        from .dashboard_testing import Dashboard
        return Dashboard(self, size=size, root=root)

    def agent(self, name, harness="claude", root=None, native_id=None, runtime=None, **properties):
        """Write an agent's records and return its directory; nothing is started.

        They describe a well-formed running launch on a private tmux socket that
        does not exist. Looking it up asks only that socket (a tmux client finds
        no server). Explicit runtime values override every default.
        """
        from uuid import uuid4
        from ..store import read, write
        root = Path(root or self.root)
        identity = str(uuid4())
        directory = root / "state" / "agents" / identity
        write(directory / "properties.json", {
            "id": identity, "name": name, "harness": harness, "task": "", "phase": "working", "summary": "",
            "universe_id": read(root / "universe.json")["id"], "workspace": str(self.work / "project"),
            **({"native_session_id": native_id} if native_id else {}), **properties})
        write(directory / "runtime.json", {"launch_id": str(uuid4()), "state": "running", "status": "idle",
            "tmux": {"socket": str(self.work / ("tmux-" + identity[:8] + ".sock")), "session": "$1", "pane": "%1"},
            **(runtime or {})})
        return directory

    def fake_runs(self, *answers):
        """One-shot runs answered from this script for the rest of the test (1.14): a FakeRuns, whose `requests`
        records what each run was asked. Everything but the run itself is real."""
        return FakeRuns(self, answers)

    def transcript(self, agent, rows=()):
        """A Claude transcript for `agent` (its directory, from agent(), or an api.Agent) that the API reads (1.13):
        a FakeTranscript, with append, rewrite, clear and unreadable. Build rows with ClaudeRows."""
        directory = agent.directory if hasattr(agent, "directory") and not isinstance(agent, Path) else agent
        return FakeTranscript(self, directory, list(rows))

    def claude_transcript(self, native_id, records, project="project"):
        """Write a Claude session transcript (JSON lines) where Claude keeps it, under CLAUDE_CONFIG_DIR."""
        path = self.claude_config / "projects" / project / f"{native_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return path

    def executable(self, name, script):
        """A fake program on PATH: `script` is its shell body (#!/bin/sh is added)."""
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(0o755)
        return path
