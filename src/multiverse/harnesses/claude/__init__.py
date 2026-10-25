"""Claude native terminal lifecycle; steering through the session's peer inbox ("auto" by default).

In the native-backends model (docs/design/harness-adapters.md, "Claude: a terminal-hosted bridge") Claude has no
native service, an exact session (the assigned UUID), a terminal attachment (the `claude` process in the agent's
pane, which does the work) and, optionally, a bridge: the multiverse mod and the inbox it hosts. The bridge is part
of the Claude process and ends with it, so it carries delivery, receipts, `ask` and status without implying an
external service: Claude declares no shared service, attachment recovery, status observer or retained usage.

Like Codex, `inbox_bridge` unset is auto (the bridge unless this Claude Code cannot run it, with the reason
shown), true requires it and false launches plain. With the bridge, each launch binds Claude's peer inbox at the core-assigned
socket and loads the multiverse mod (`mod/`). `send` writes one steering
message to that inbox at `next` priority, its text tagged for the mod. The mod
shows the model the text alone: mid-turn it removes Claude's peer wrapper from
the queued message; otherwise it types the text in as a prompt. The mod
registers the bridge (`BRIDGE`) for its launch and reports the session's
status: working, waiting (a permission prompt or question) or idle.

Usage is measured from the session's own transcript, with or without the bridge.

Tree compaction is the `compaction` plugin (the plugins/compaction submodule):
its launch contribution loads its own Claude plugin, which answers the
session's `/compact` with mv2's compacted conversation, and adds a PreCompact
hook that refuses Claude's own compaction should that ever fail to answer. Its
Claude source (sources/claude) reads this harness's transcripts.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

from ...plugins import harness as plugins
from ..base import ConfiguredAdapter
from ...runtime.process import process_snapshot, process_started, start_matches
from ..types import Capability, NativeEndpoint, NativeResult, ObservedBinding, normalize_session_id
from ..types import Usage
from ...store import read

MOD = Path(__file__).parent / 'mod'
# Typeahead for /multiverse is now the bundled `typeahead` mv2 plugin (multiverse/plugins/builtin/typeahead),
# loaded like any plugin's Claude directory from Claude Code 2.1.292. This copy stays, unchanged, only for
# agents launched before the move: Claude reloads a plugin from the path it was given. Remove it once no
# live launch's request.json names it.
# The mod acts only on text between these tags and removes them. The opening
# tag names the delivery mode, which the mod honors, and the message ID for its
# receipt: `<<MV2 steer id=UUID>>` ... `<</MV2>>`.
CLOSE = '<</MV2>>'
MODES = {'steer': 'steer', 'follow-up': 'follow-up'}
ASK_TIMEOUT = 120
# What the mod reports through `runtime-update --bridge` once it runs in a launch.
BRIDGE = 'claude-mod-v2'  # the mod that reads a tag's send time
# A mod that registered before it could, until it reloads: still connected, and sent tags without a send time,
# since it would miss a tag it can't parse and show the model raw tags with no receipt.
BRIDGES = ('claude-mod-v1', BRIDGE)
FUNCTION_HOOKS = 'CLAUDE_CODE_ENABLE_FUNCTION_HOOKS'  # loads the mod: set for bridge launches only
NOT_REGISTERED = 'The multiverse mod has not registered for this launch'
NO_KEY = 'Claude has not published the inbox key for this launch'
RELAUNCHED = "Claude relaunched itself (/restart) without mv2's inbox; restart the agent"
NOT_RUNNING = "This agent isn't running: resume it first"
TOKEN = re.compile(r'^[0-9a-f]{32}$')
USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


def claude_home(environment=None):
    """Claude's storage root: `environment` as a launch sees it, else this process's."""
    source = environment or os.environ
    if source.get('CLAUDE_CONFIG_DIR'):
        return Path(source['CLAUDE_CONFIG_DIR'])
    return (Path(source['HOME']) if source.get('HOME') else Path.home()) / '.claude'


class Unreadable(Exception):
    """A folder or file under Claude's storage that can't be read, so a lookup can't tell what is there."""

    def __init__(self, path):
        super().__init__(f"{path} can't be read")
        self.path = str(path)


def open_regular(path):
    """`path` opened for reading, through any link, only when it is a regular file, and never waiting: a FIFO,
    device or directory at a transcript's name raises Unreadable. The type is read from the opened descriptor, so a
    file swapped in after a check can't slip past, and a FIFO with no writer can't hold the open (or a lock)."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Unreadable(path)
        os.set_blocking(descriptor, True)
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def read_regular(path):
    """The whole of a regular file (open_regular)."""
    with open_regular(path) as handle:
        return handle.read()


PROJECT_KEY_LENGTH = 200  # past this, Claude shortens a project folder's name and adds a hash only Bun computes


def project_key(path):
    """Claude's project folder name for a launch directory: the NFC path, every UTF-16 code unit that isn't
    an ASCII letter or digit replaced by '-', as Bun does (so a character outside the BMP gives '--')."""
    return ''.join(c if c.isascii() and c.isalnum() else '-' * (2 if ord(c) > 0xFFFF else 1)
                   for c in unicodedata.normalize('NFC', str(path)))


def could_hold(name, workspaces):
    """Whether Claude could file a session launched in one of `workspaces` under the project folder `name`.

    The name is lossy, so this errs towards yes: the folder's name, ignoring case, is a workspace's key or
    starts with it plus '-' (Claude's own worktrees under the workspace, and the hashed form past 200 units).
    With no absolute workspace to go by, any folder could.
    """
    return holds_name(name, project_keys(workspaces))


def project_keys(workspaces):
    """The folder keys of `workspaces` (each absolute one as recorded and as its real path), for holds_name; resolved
    once per scan, since the listing scans a shared store per agent."""
    return {project_key(form)[:PROJECT_KEY_LENGTH].casefold()
            for workspace in workspaces if isinstance(workspace, str) and os.path.isabs(workspace)
            for form in (workspace, os.path.realpath(workspace))}


def holds_name(name, keys):
    """could_hold, from the keys project_keys gave; no keys means any folder could."""
    folded = name.casefold()
    return not keys or any(folded == key or folded.startswith(key + '-') for key in keys)


def session_transcripts(native_id, environment=None, workspaces=(), links=False):
    """Every main transcript of `native_id` (`projects/*/<id>.jsonl`), oldest first.

    Raises Unreadable when a folder can't be listed or searched and could hold the session (a dangling link, or a
    folder gone before it was searched, counts as one that can't be searched): Claude files it
    under its launch directory's name, so with `workspaces` (the launch's own, never re-read) an unreadable
    folder of another workspace is skipped. pathlib's glob skips every unreadable folder silently, so a scan
    that finds nothing would otherwise read as proof the transcript is absent.
    """
    return _scan_transcripts(native_id, environment, workspaces, links)[0]


def _scan_transcripts(native_id, environment=None, workspaces=(), links=False):
    """session_transcripts, and whether this call listed the projects folder itself (False when it is missing)."""
    projects = claude_home(environment) / 'projects'
    try:
        entries = list(os.scandir(projects))
    except FileNotFoundError:
        return [], False
    except OSError as error:
        raise Unreadable(projects) from error
    found, files, keys = [], set(), project_keys(workspaces)
    for entry in entries:
        candidate = Path(entry.path) / f'{native_id}.jsonl'
        holds = holds_name(entry.name, keys)
        try:
            if holds:
                # Resolved now, through any link, never from the listing's type: a dangling or looping link, or a
                # folder gone since the listing, proves nothing; a link to a real folder is searched through it.
                if not stat.S_ISDIR(os.stat(entry.path).st_mode):
                    continue
            elif not entry.is_dir():
                continue
            # `links` finds a link (even a dangling one) where a transcript should be, for readers that refuse it.
            status = candidate.lstat() if links else candidate.stat()
            if (status.st_dev, status.st_ino) in files:
                continue  # the same file through another path (a linked project folder): read it once
            files.add((status.st_dev, status.st_ino))
            found.append((status.st_mtime, candidate))
        except FileNotFoundError as error:
            if holds and not os.path.isdir(entry.path):  # the folder itself is gone: a dangling link, or removed mid-scan
                raise Unreadable(entry.path) from error
            continue
        except OSError as error:
            if holds:  # a project folder that can't be searched could hold it
                raise Unreadable(entry.path) from error
    return [path for _, path in sorted(found)], True


IDENTITY_BYTES = 1 << 20


def transcript_identity(path, native_id):
    """None when `path` is a regular file whose rows name session `native_id` (read within its first MiB), else
    why it proves nothing: a link, a directory or other non-regular file, an unreadable or empty file, or rows of
    no session or another one."""
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            return f'{path} is a link, not a transcript'
        if not stat.S_ISREG(path.lstat().st_mode):
            return f'{path} is not a regular file'
        with open_regular(path) as handle:
            head = handle.read(IDENTITY_BYTES)
    except Unreadable:
        return f'{path} is not a regular file'
    except OSError as error:
        return f"{path} can't be read ({error.strerror or error})"
    named = set()
    for raw in head.split(b'\n')[:-1] if len(head) == IDENTITY_BYTES else head.split(b'\n'):
        try:
            row = json.loads(raw) if raw.strip() else None
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get('sessionId'), str):
            named.add(row['sessionId'])
    if named - {native_id}:
        return f"{path} holds another session's rows"
    if native_id not in named:
        return f'{path} has no row naming this session'
    return None


def subagent_transcripts(main):
    """The subagent transcripts beside a main transcript; Unreadable when part of that folder can't be read."""
    folder = main.parent / main.stem / 'subagents'
    found = []

    def unreadable(error):
        if not isinstance(error, FileNotFoundError):
            raise Unreadable(error.filename or folder) from error

    for directory, _, files in os.walk(folder, onerror=unreadable):
        found.extend(Path(directory) / name for name in files if name.endswith('.jsonl'))
    return sorted(found)


def has_saved_conversation(native_id, environment=None, workspaces=()):
    """Whether Claude has written a transcript for this session.

    Claude writes nothing until the first message, so a session that was never
    used has none, and `--resume` refuses it. When its storage can't be read this
    says yes: resume then tries the conversation, and Claude says if it is gone.
    """
    try:
        return bool(session_transcripts(native_id, environment, workspaces))
    except Unreadable:
        return True


def peer_token(socket_path, home=None):
    """The inbox key Claude publishes for this socket, or None before it binds.

    Claude writes `<pid>.<sha256 of the socket path>.key` under its sessions
    directory (in `home`, the launch's storage, else this process's); the socket
    path is unique to one launch.
    """
    digest = hashlib.sha256(os.path.abspath(socket_path).encode()).hexdigest()
    keys = list(((Path(home) if home else claude_home()) / 'sessions').glob(f'*.{digest}.key'))
    if len(keys) != 1:
        return None
    token = json.loads(keys[0].read_text()).get('peerToken')
    return token if isinstance(token, str) and TOKEN.match(token) else None


# Claude writes procStart in its own format: field 22 of /proc/<pid>/stat (clock ticks since boot) on Linux,
# `ps -o lstart=` in UTC elsewhere (macOS), the same form as the runner's pid_started there.
LINUX = sys.platform.startswith('linux')
PROC = Path('/proc')


def process_file(session):
    """Claude's own file for this running launch's pid (`<home>/sessions/<pid>.json`), or None.

    Reads only that file: a launch that is not running, or has no file, costs no process query.
    Undocumented and Claude's own, like the peer key files beside it.
    """
    pid = session.pid
    if session.state != 'running' or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    home = Path(session.home) if session.home else claude_home()
    try:
        record = json.loads((home / 'sessions' / f'{pid}.json').read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get('procStart'), str) else None


def _proc_start(pid):
    """Linux: (state, start in clock ticks) from /proc/<pid>/stat, or None.

    Fields are counted after the last ')', since the command name can hold spaces and parentheses.
    """
    try:
        stat = (PROC / str(pid) / 'stat').read_text()
    except (OSError, ValueError):
        return None
    fields = stat[stat.rfind(')') + 2:].split(' ') if ')' in stat else []
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return fields[0], fields[19]


def same_process(record, pid):
    """mv2's start of `pid` (as the runner records pid_started) when Claude's file `record` is this live,
    non-zombie process, else None.

    One ps on macOS. On Linux, /proc first and ps only when the file's ticks match it.
    """
    claimed = ' '.join(record['procStart'].split())
    if LINUX:
        proc = _proc_start(pid)
        if proc is None or proc[0] == 'Z' or proc[1] != claimed:
            return None
        return process_started(pid)
    snapshot = process_snapshot(pid)
    if snapshot is None or snapshot[1].startswith('Z') or snapshot[0] != claimed:
        return None
    return snapshot[0]


def process_record(session):
    """Claude's file for this exact launch's process, or None.

    Only when three starts are one: the one the runner recorded when it started this
    launch's child, the pid's start now, and the file's procStart (in Claude's format).
    A pid reused by a later process fails the first; execve (/restart) keeps the pid and its start.
    Core compares the recorded start: it knows its forms (a Linux token, or the ctime older launches
    recorded), and is given the start same_process already read, so this costs no extra query.
    """
    record = process_file(session)
    if record is None or not session.pid_started:
        return None
    observed = same_process(record, session.pid)
    return record if observed is not None and start_matches(session.pid_started, session.pid, observed=observed) else None


def unverified_relaunch(session):
    """For a launch recorded before its start was: whether Claude's file for its pid names another socket.

    Never trusted for delivery or a binding, only to tell the person to restart the agent.
    """
    if session.pid_started:
        return False
    record = process_file(session)
    if record is None or not same_process(record, session.pid):
        return False
    path = record.get('messagingSocketPath')
    return isinstance(path, str) and os.path.abspath(path) != os.path.abspath(session.endpoint.address)


class Transcript:
    """Usage recorded in one transcript file, read once and then as it grows.

    Claude writes one record per content block of a response, each with the
    response's usage, so usage is counted once per response ID. A row with zero
    usage is never a response: a compaction that keeps a row stores it again with
    zero usage, and an installed compaction's rows arrive as new IDs with zero
    usage (Claude's `<synthetic>` model). Neither replaces a real response's usage
    or becomes the latest request, so `last` stays the latest real response.
    """

    def __init__(self, path):
        self.path, self.inode, self.offset, self.responses, self.last = path, None, 0, {}, None

    def read(self):
        status = self.path.stat()
        if status.st_ino != self.inode or status.st_size < self.offset:
            self.inode, self.offset, self.responses, self.last = status.st_ino, 0, {}, None
        with open_regular(self.path) as handle:
            handle.seek(self.offset)
            data = handle.read()
        end = data.rfind(b'\n') + 1  # an unfinished last line waits for its newline
        for line in data[:end].splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get('message') if isinstance(record, dict) and record.get('type') == 'assistant' else None
            if isinstance(message, dict) and isinstance(message.get('usage'), dict) and message.get('id'):
                if not any(counted(message['usage'], key) for key in USAGE_KEYS):
                    continue
                self.responses[message['id']] = message['usage']
                self.last = record.get('timestamp') or self.last
        self.offset += end
        return self


_transcripts = {}
_transcripts_lock = threading.Lock()


def counted(usage, key):
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def context_reading(native_id, workspaces=(), environment=None):
    """(tokens, timestamp) of the latest main-thread request, or None.

    Tokens are input + cache written + cache read. Subagent transcripts are
    excluded: they have their own context windows.
    """
    mains = session_transcripts(native_id, environment, workspaces)
    if not mains:
        return None
    with _transcripts_lock:
        transcript = _transcripts.setdefault(mains[-1], Transcript(mains[-1])).read()
        responses, stamp = transcript.responses, transcript.last
    if not responses:
        return None
    latest = list(responses.values())[-1]
    return sum(counted(latest, key) for key in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')), stamp


def context_usage(native_id):
    """Tokens in the latest main-thread request, or None."""
    reading = context_reading(native_id)
    return reading[0] if reading else None


def moment(text):
    """A timestamp as a datetime, or None."""
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')) if isinstance(text, str) else None
    except ValueError:
        return None


def measured_usage(native_id, workspaces=(), environment=None):
    """Cumulative usage of the session and its subagents, from their transcripts. Raises Unreadable rather
    than total a scan that skipped part of the storage."""
    mains = session_transcripts(native_id, environment, workspaces)
    if not mains:
        return None
    paths = [mains[-1], *subagent_transcripts(mains[-1])]
    with _transcripts_lock:
        transcripts = [_transcripts.setdefault(path, Transcript(path)).read() for path in paths]
    responses = [usage for transcript in transcripts for usage in transcript.responses.values()]
    cached = sum(counted(usage, 'cache_read_input_tokens') for usage in responses)
    written = sum(counted(usage, 'cache_creation_input_tokens') for usage in responses)
    inputs = sum(counted(usage, 'input_tokens') for usage in responses) + cached + written
    outputs = sum(counted(usage, 'output_tokens') for usage in responses)
    last = max((transcript.last for transcript in transcripts if transcript.last), default='')
    return Usage(native_id, inputs, outputs, inputs + outputs, 'claude-transcript', last, cached, written)


# The oldest Claude Code the mod and the peer inbox were built and tested on.
MIN_BRIDGE_VERSION = (2, 1, 280)
# Resume passes `--system-prompt-snapshot off`, which Claude Code has had since this version. With the
# snapshot on (its default) a resumed conversation keeps the system prompt recorded at its first request,
# so a bootstrap changed since then (a plugin's line added or removed, a template update) never applies.
SNAPSHOT_FLAG = '--system-prompt-snapshot'
SNAPSHOT_FLAG_VERSION = (2, 1, 267)
_versions = {}


def claude_version(executable):
    """The (major, minor, patch) of this Claude Code binary, or None when it cannot be told.

    Asked once per binary (an upgrade installs a new binary path), never fatal.
    """
    path = shutil.which(executable)
    if path is None:
        return None
    key = os.path.realpath(path)
    if key not in _versions:
        try:
            output = subprocess.run([path, '--version'], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        found = re.search(r'(\d+)\.(\d+)\.(\d+)', output)
        if not found:
            return None
        _versions[key] = tuple(int(part) for part in found.groups())
    return _versions[key]


def bridge_problem(executable):
    """Why this Claude Code cannot run the bridge, or None. A version that cannot be read is not a reason."""
    version = claude_version(executable)
    if version is not None and version < MIN_BRIDGE_VERSION:
        return (f'Claude Code {".".join(map(str, version))} is older than '
                f'{".".join(map(str, MIN_BRIDGE_VERSION))}, which the multiverse mod needs')
    return None


# A resume whose session Claude has no conversation for starts it fresh under the same ID (PR #3), and says which
# case it is: never used, or a saved conversation that went missing (Claude deletes them after cleanupPeriodDays).
# The notes say what this launch does, which holds even when the launch then fails at startup.
NEVER_USED = ('Claude has no saved conversation for this session: the agent was stopped before its first message, '
              'so this launch starts it fresh under the same session ID')
CONVERSATION_MISSING = ("Claude's saved conversation for this session is missing (Claude deletes conversations older "
                        'than its cleanupPeriodDays setting): this launch starts it fresh under the same session ID, '
                        'without its earlier context')
# Earlier records joined a note like these onto native_mode.fallback_reason; launch-only causes filter them out.
FRESH_NOTES = (NEVER_USED, CONVERSATION_MISSING,
               'No saved Claude conversation for this session; started it fresh under the same session ID',
               'Claude has no saved conversation for this session: the agent was stopped before its first message, '
               'so it started fresh under the same session ID',
               "Claude's saved conversation for this session is missing (Claude deletes conversations older than its "
               'cleanupPeriodDays setting): the agent started fresh under the same session ID, without its earlier context')
FRESH_SESSION = FRESH_NOTES[2]  # the first note's text, kept for callers that name it
LAUNCH_ONLY = 'This agent is launch-only'


def fresh_start(session, environment):
    """The note for resuming `session` fresh, or None when Claude has its conversation and resume continues it."""
    if not session.native_id or has_saved_conversation(session.native_id, environment, (session.workspace,)):
        return None
    return CONVERSATION_MISSING if had_conversation(session, environment) else NEVER_USED


started_fresh = fresh_start  # resume_notice's `fresh_start` keyword shadows the function there


def had_conversation(session, environment):
    """Whether this session ever had a message, from records that outlive Claude's transcript. False means no
    evidence, never proof: Claude prunes its prompt history too, and a person's prompts leave mv2 nothing else."""
    directory = Path(session.session_file).parent if session.session_file else None
    return bool((directory and (_stored_message(directory, session.native_id)
                                or _context_since_move(directory, session.native_id)))
                or _typed_prompt(session.native_id, environment))


def _stored_message(directory, native_id):
    """A message core sent to this session whose row the mod confirmed was stored in the conversation."""
    try:
        state = json.loads((directory / 'inbox-state.json').read_text())
    except (OSError, ValueError):
        return False
    for message_id, record in state.items() if isinstance(state, dict) else ():
        if not isinstance(record, dict):
            continue
        attempts = record.get('attempts') if isinstance(record.get('attempts'), list) else []
        if record.get('target_session') != native_id and not any(
                isinstance(attempt, dict) and attempt.get('native_session_id') == native_id for attempt in attempts):
            continue
        try:
            if (directory / 'claude-receipts' / f'{normalize_session_id(message_id)}.json').is_file():
                return True
        except ValueError:
            continue
    return False


def _context_since_move(directory, native_id):
    """Context the mod reported during the last launch, after the agent last moved to this session (/clear)."""
    try:
        usage = json.loads((directory / 'runtime.json').read_text()).get('usage')
        tokens = usage['context']['tokens']
        reported = moment(usage['reported_at'])
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return False
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0 or reported is None:
        return False
    moved = None
    try:
        for line in (directory / 'sessions.jsonl').read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            at = moment(row.get('at')) if isinstance(row, dict) and row.get('to') == native_id else None
            if at and (moved is None or at > moved):
                moved = at
    except OSError:
        pass
    return moved is None or reported > moved


def _typed_prompt(native_id, environment):
    """A prompt typed into this session, in Claude's own prompt history (each line names its sessionId)."""
    path = claude_home(environment) / 'history.jsonl'
    needle = f'"sessionId":"{native_id}"'.encode()
    try:
        if not path.exists():
            return False
        with open_regular(path) as stream:
            tail = b''
            while chunk := stream.read(1 << 20):
                if needle in tail + chunk:
                    return True
                tail = chunk[-len(needle):]
    except (OSError, Unreadable):
        return False
    return False


def launch_storage(session, environment):
    """Where a launch's Claude keeps its transcripts, as an environment for claude_home, or None if unknown.

    session.home is pinned before the launch starts; failing that, the launch's own environment. Never this
    process's: a recheck run elsewhere, or after HOME changed, would read another storage, find nothing, and
    take that as proof a prompt never arrived.
    """
    home = getattr(session, 'home', None)
    if isinstance(home, str) and os.path.isabs(home):
        return {'CLAUDE_CONFIG_DIR': home}
    source = environment or {}
    if source.get('CLAUDE_CONFIG_DIR'):  # Claude would resolve a relative one against a working directory we don't know
        return {'CLAUDE_CONFIG_DIR': source['CLAUDE_CONFIG_DIR']} if os.path.isabs(source['CLAUDE_CONFIG_DIR']) else None
    return {'HOME': source['HOME']} if source.get('HOME') and os.path.isabs(source['HOME']) else None


def prompt_evidence(native_id, prompt, since, environment, workspaces=()):
    """(answer, why) for whether Claude recorded `prompt`, a launch's command-line first prompt, in session
    `native_id` from `since` on, reading the storage `environment` names.

    The answer is True for a user row with it, False when the transcript is missing or complete and has none,
    and None when that can't be told. Then `why` says what can't be read and what to fix, or is None where
    core's own facts are missing (no session, prompt, storage, or `since` not an aware time).
    """
    start, text = moment(since), (prompt or '').strip()
    if not native_id or not text or start is None or start.tzinfo is None or not environment:
        return None, None
    try:
        paths = session_transcripts(native_id, environment, workspaces)
    except Unreadable as error:
        return None, f"Claude's storage folder {error.path} can't be read: restore read access to it"
    damaged = None
    for path in paths:
        try:
            lines = read_regular(path).split(b'\n')
        except (OSError, Unreadable):
            return None, f"Claude's transcript {path} can't be read: restore read access to it"
        for raw in lines:
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:  # damaged, or the last row cut short while it was being written
                damaged = damaged or path
                continue
            message = row.get('message') if isinstance(row, dict) and row.get('type') == 'user' else None
            content = message.get('content') if isinstance(message, dict) else None
            body = content if isinstance(content, str) else ' '.join(
                str(block.get('text', '')) for block in content if isinstance(block, dict)) if isinstance(content, list) else ''
            if text not in body:
                continue
            when = moment(row.get('timestamp'))
            try:
                if when is None or when >= start:  # a matching row with no usable time still counts as evidence
                    return True, None
            except TypeError:  # a time without a zone can't be compared
                return True, None
    if damaged:
        return None, f"Claude's transcript {damaged} has a damaged or unfinished row: if Claude is still writing it, wait"
    return False, None


def _delivery_row(row):
    """What one transcript row says about mv2's deliveries: ('id', msg_id) for the queued_command a mid-turn steer
    becomes, ('text', text, timestamp) for a prompt, None for any other row, or False when a row of either kind
    is malformed (valid JSON in the wrong shape is damage, never an empty message)."""
    if not isinstance(row, dict):
        return False
    if row.get('type') == 'attachment':
        attachment = row.get('attachment')
        if not isinstance(attachment, dict):
            return False
        if attachment.get('type') != 'queued_command':
            return None
        origin = attachment.get('origin', {})
        if not isinstance(attachment.get('prompt'), str) or not isinstance(origin, dict) \
                or not isinstance(origin.get('msg_id', ''), str):
            return False
        if origin.get('msg_id'):
            return 'id', origin['msg_id']
        return 'text', attachment['prompt'], row.get('timestamp') or attachment.get('timestamp')
    if row.get('type') == 'user':
        message = row.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, str):
            return 'text', content, row.get('timestamp')
        if not isinstance(content, list) or not all(isinstance(block, dict) and isinstance(block.get('type'), str)
                                                    and (block['type'] != 'text' or isinstance(block.get('text'), str))
                                                    for block in content):
            return False
        return 'text', '\n'.join(block['text'] for block in content if block['type'] == 'text'), row.get('timestamp')
    return None


RECEIPT_WINDOW = timedelta(minutes=1)  # the mod writes a prompt receipt milliseconds after its row is stored


def _receipt(directory, receipts, message_id):
    """A message's delivery receipt: (route, time or None) from the mod's file, else the ledger's copy of it; None
    when there is none; False when one exists but can't be read or isn't a receipt."""
    value = None
    if directory is not None:
        path = Path(directory) / 'claude-receipts' / f'{message_id}.json'
        try:
            record = json.loads(path.read_text())
            value = {'route': record.get('route'), 'delivered_at_ms': record.get('at')} if isinstance(record, dict) else False
        except FileNotFoundError:
            value = None
        except (OSError, ValueError):
            return False
    if value is None:
        value = (receipts or {}).get(message_id)
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('route') not in ('prompt', 'mid-turn'):
        return False
    at = value.get('delivered_at_ms')
    when = datetime.fromtimestamp(at / 1000, timezone.utc) if type(at) is int and at > 0 else None
    return value['route'], when


# A late message's age at the end of its sender line ("Message from NAME (sent 25 min ago):", after any
# "(in universe U)"): core ages the frame it prepares and the mod ages it again when the row is stored, so framed
# texts are compared without it, and without anything else changed.
AGED = re.compile(r' \(sent \d+ (?:min|hours?|days?) ago\)(?=:$)')
SENDER = re.compile(r'^Message from .+:$')
SENT = re.compile(r'[0-9][0-9T:.+\-Z]*')  # the send time the mod's tag pattern accepts


def without_age(text):
    """`text` with no age in its sender line, for comparing framed texts."""
    first, newline, rest = text.partition('\n')
    return AGED.sub('', first, count=1) + newline + rest if SENDER.match(first) else text


def message_delivered(native_id, message_id, text, since, environment, workspaces=(), framed=True, cohort=(),
                      directory=None, receipts=None, active=()):
    """(answer, why) for whether a message Claude accepted reached session `native_id`'s conversation.

    Never confirms falsely: when in doubt the answer is None (can't tell). True for our receipt, or the
    queued_command row a mid-turn steer becomes, which carries our message ID. Otherwise a stored copy of the
    framed text (a follow-up or idle steer the mod typed in, equal to it once stripped) counts once, by its row
    uuid across transcript copies. `cohort` holds the session's other acceptances as (message_id, framed text,
    since). Among those with our text, an exact receipt (the mod's file in `directory`, else the ledger's in
    `receipts`) first takes its own copy: a prompt receipt the one unused copy stored between its accept time
    and it, within a minute (none, or more than one, leaves this unknown); a mid-turn receipt none, being a
    queued_command row. The remaining copies go to the others from their accept
    times on, oldest first. False only when every transcript that could hold it was read whole and no copy is
    left from our accept time on. With `framed` False, `text` is only the body (older records): a match is not
    proof, since another message can share it; its absence still is. An identical acceptance in `active` (accepted by
    the current live launch) with no receipt yet may still write one for a stored copy, so it leaves us unknown;
    copies of one row uuid that disagree on its type, text or time make that row unknown, whatever order they're
    read in.
    """
    start, needle = moment(since), (without_age(text) if framed else text or '').strip()
    if not native_id or not needle or start is None or start.tzinfo is None or not environment:
        return None, None
    ours = _receipt(directory, receipts, message_id) if message_id else None
    if ours:
        return True, None
    if ours is False:
        return None, "This message's delivery receipt can't be read"
    try:
        paths = session_transcripts(native_id, environment, workspaces)
    except Unreadable as error:
        return None, f"Claude's storage folder {error.path} can't be read: restore read access to it"
    damaged, ids, versions, prints, conflicting = None, set(), {}, {}, set()
    for path in paths:
        try:
            lines = read_regular(path).split(b'\n')
        except (OSError, Unreadable):
            return None, f"Claude's transcript {path} can't be read: restore read access to it"
        for raw in lines:
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
                seen = _delivery_row(row)
            except ValueError:  # damaged, or the last row cut short while it was being written
                row, seen = None, False
            if seen is False:
                damaged = damaged or path
                continue
            uuid = row.get('uuid') if isinstance(row.get('uuid'), str) and row.get('uuid') else None
            if uuid is not None:  # every copy of a row must agree on what it proves
                # Exactly what allocation reads: the raw ID or text, and a text row's own time selection.
                proof = (row.get('type'), *(seen if seen else (None,)))
                if prints.setdefault(uuid, proof) != proof:
                    conflicting.add(uuid)
            if seen and seen[0] == 'id':
                versions.setdefault(uuid or object(), []).append(('id', seen[1], None, path))
            elif seen and (without_age(seen[1]).strip() == needle if framed else needle in seen[1]):
                versions.setdefault(uuid or object(), []).append(('text', None, seen[2], path))
    copies = []
    for key, found in versions.items():
        uuid = key if isinstance(key, str) else None
        if uuid in conflicting:
            if any(kind == 'text' or value == message_id for kind, value, _, _ in found):
                damaged = damaged or found[0][3]  # copies that disagree prove nothing about this message
            continue  # another message's ID row in doubt: it isn't counted as confirmed either
        kind, value, stamp, path = found[0]
        if kind == 'id':
            ids.add(value)
            continue
        when = moment(stamp)
        if when is None or when.tzinfo is None or uuid is None:
            damaged = damaged or path  # a copy that can't be placed in time, or told from another copy
        else:
            copies.append(when)  # once per uuid, however many copies of the transcript hold it
    if message_id and message_id in ids:
        return True, None
    if damaged:
        return None, f"Claude's transcript {damaged} has a damaged or unfinished row: if Claude is still writing it, wait"
    if not framed:
        if any(when >= start for when in copies):
            return None, ("Only this message's body was recorded, and its conversation holds that body, which another "
                          "message could also carry: delivery can't be told from the text alone")
        return False, None
    rivals, holders = [], []
    for entry in cohort or ():
        if not (isinstance(entry, (tuple, list)) and len(entry) == 3 and all(isinstance(part, str) for part in entry)):
            return None, 'An earlier acceptance on this session has an incomplete record'
        other, other_text, other_since = entry
        if other == message_id or other in ids or without_age(other_text).strip() != needle:
            continue
        accepted = moment(other_since)
        if accepted is None or accepted.tzinfo is None:
            return None, 'An earlier acceptance of the same text has no usable accept time'
        if directory is None and not receipts:
            return None, "Another acceptance of the same text can't be checked for a receipt: no agent folder"
        receipt = _receipt(directory, receipts, other)
        if receipt is False or receipt and receipt[0] == 'prompt' and receipt[1] is None:
            return None, "Another acceptance of the same text has a receipt that can't be placed in time"
        if receipt and receipt[0] == 'prompt':
            holders.append((receipt[1], accepted))
        elif not receipt and other in (active or ()):
            return None, ("An identical message the live launch accepted may still be stored and confirmed: check again "
                          "once it is confirmed or its launch has ended")
        elif not receipt:  # a mid-turn receipt is a queued_command row: it takes no prompt copy
            rivals.append((accepted, other, False))
    free = sorted(copies)
    for written, accepted in sorted(holders):
        mine = [index for index, when in enumerate(free) if accepted <= when <= written and written - when <= RECEIPT_WINDOW]
        if not mine:
            return None, "A receipt for another message of the same text names a copy the transcript doesn't show"
        if len(mine) > 1:  # which copy it was is a guess, and the guess could decide another message
            return None, "A receipt for another message of the same text could match more than one stored copy"
        free.pop(mine[0])
    for accepted, _, is_ours in sorted([*rivals, (start, message_id or '', True)]):
        taken = next((index for index, when in enumerate(free) if when >= accepted), None)
        if is_ours:
            if taken is not None:
                return True, None
            if any(when >= start for when in copies):
                return None, ("This text was stored fewer times than it was accepted, and the copies went to other "
                              "identical messages: delivery can't be told for this one")
            return False, None
        if taken is not None:
            free.pop(taken)
    return None, None


def prompt_recorded(native_id, prompt, since, environment):
    """prompt_evidence's answer alone: True, False or None."""
    return prompt_evidence(native_id, prompt, since, environment)[0]


def ignored_storage(caller, effective, home=None):
    """A caller's CLAUDE_CONFIG_DIR that the resume replaced with the recorded storage, named as Codex names an
    ignored CODEX_HOME; None when it is unset, the same storage, or nothing says where the kept one is.

    The kept storage is the launch's CLAUDE_CONFIG_DIR when it had one; else, for the default storage, its
    recorded home (session.home), or HOME/.claude of the launch environment. Never this process's.
    """
    requested = (caller or {}).get('CLAUDE_CONFIG_DIR')
    if not isinstance(requested, str) or not requested:
        return None
    effective = effective or {}
    saved = effective.get('CLAUDE_CONFIG_DIR')
    if not (isinstance(saved, str) and saved):
        launch_home = effective.get('HOME')
        saved = (home if isinstance(home, str) and os.path.isabs(home)
                 else os.path.join(launch_home, '.claude') if isinstance(launch_home, str) and os.path.isabs(launch_home)
                 else None)
    if saved is None:
        return None
    try:
        kept = Path(saved).expanduser().resolve()
        if Path(requested).expanduser().resolve() == kept:
            return None
    except (OSError, ValueError, RuntimeError):
        return None  # a diagnostic can't claim a storage choice it couldn't check
    return f'Kept its saved Claude storage {json.dumps(str(kept))}; ignored CLAUDE_CONFIG_DIR={json.dumps(requested)}.'


def launch_only_cause(launch):
    """Why a launch has no connection by design: its harness setting, or what kept an auto launch from the mod."""
    if launch.get('policy') == 'plain':
        return 'its harness sets inbox_bridge to false'
    text = str(launch.get('fallback_reason') or '')
    for note in FRESH_NOTES:  # resume joins its fresh-start note on; it is no cause (the older one holds a '; ')
        text = text.replace(note, '')
    return '; '.join(part for part in text.split('; ') if part.strip()) or 'it was launched without the bridge'


@dataclass(frozen=True)
class Bridge:
    """What one launch's terminal-hosted integration, the multiverse mod, can do now.

    `launched`: this launch loaded the mod (not plain, not an auto fallback). `registered`: the mod
    reported itself for this launch, so its inbox exists. `ready`: steering, `ask` and receipts can be used
    now. `reason` says why not. `launch_only`: the launch has no connection, so restarting it as it is can't
    give it one. `final`: that is the harness's own setting (inbox_bridge false), so no later launch will
    either; an auto launch that fell back because this Claude was too old is not final, since it connects
    once Claude is updated and the agent resumed. None of this is a service or a reconnectable terminal.
    """
    launched: bool
    registered: bool
    ready: bool
    reason: str | None
    launch_only: bool = False
    final: bool = False


def bridge_of(enabled, session):
    """The `Bridge` of a launch: the harness setting says whether the mod is wanted, the launch's own record
    whether it ran (an auto launch may have fallen back to plain), and the mod's report whether it is up."""
    launch = session.native_mode or {}
    plain = launch.get('mode') == 'plain'
    launched = bool(enabled) and not plain
    registered = bool(launched and session.native_id and session.endpoint
                      and session.endpoint.transport == 'unix-lines' and session.bridge in BRIDGES)
    ready = registered and session.state == 'running'
    # Launch-only: restarting as configured now can't connect it. The setting is off (final), or an auto launch
    # fell back because this Claude was too old (until it is updated). A launch made while the setting was off,
    # with it on now, is a restart case: a resume connects it.
    launch_only = not enabled or (plain and launch.get('policy') != 'plain')
    if ready:
        reason = None
    elif launch_only:  # first: resuming a launch-only agent would not connect it
        cause = launch_only_cause({'policy': 'plain'} if not enabled else launch)
        reason = f'{LAUNCH_ONLY}: {cause}'
    elif session.state in ('stopped', 'dormant', 'failed'):
        reason = NOT_RUNNING
    else:
        reason = NOT_REGISTERED
    return Bridge(launched, registered, ready, reason, launch_only, final=launch_only and not enabled)


def unavailable(bridge):
    """Why send or ask can't run now. A launch-only one is flagged, so core advises no plain restart, and says
    whether that is final (core may close its messages) or lasts until Claude is updated and the agent resumed."""
    return NativeResult('unavailable', bridge.reason,
                        {'launch_only': True, 'final': bridge.final} if bridge.launch_only else None)


class ClaudeAdapter(ConfiguredAdapter):
    resume_prompt_args = ('--', '{task}')
    package = Path(__file__).parent
    supports_bridge = True
    family = 'claude'
    bridge_policy = 'auto'
    storage_env = ('CLAUDE_CONFIG_DIR',)

    @classmethod
    def configured(cls, name, settings):
        # Like Codex: unset is auto, true requires the bridge, false launches plain. `settings['inbox_bridge']`
        # holds the effective on/off (auto is on) for everything that reads it; the policy is kept apart.
        raw = settings.get('inbox_bridge')
        # "off" (the default) resumes with --system-prompt-snapshot off, so the current bootstrap applies; "on"
        # leaves Claude's own snapshot, so a resumed conversation keeps the prompt of its first request.
        settings = dict(settings)
        snapshot = settings.pop('system_prompt_snapshot', 'off')
        if snapshot not in ('off', 'on'):
            raise ValueError('system_prompt_snapshot must be "off" or "on"')
        adapter = super().configured(name, {**settings, 'inbox_bridge': True if raw is None else raw})
        adapter.bridge_policy = 'auto' if raw is None else 'required' if raw is True else 'plain'
        adapter.system_prompt_snapshot = snapshot
        return adapter

    def spawn(self, context):
        return self._with_bridge(super().spawn(context), context)

    def resume(self, session, context):
        command = self._snapshot_flag(super().resume(session, context))
        note = fresh_start(session, context.environment)
        fresh = self._reserve_session(command, session.native_id) if note else None
        # The note itself is the resume's notice (`resume_notice`), which core shows and keeps with the launch.
        return self._with_bridge(fresh or command, context)

    def resume_notice(self, session, caller_environment, effective_environment, *, fresh_start=True):
        """What a resume result adds: a caller's CLAUDE_CONFIG_DIR it ignored for the storage recorded at spawn,
        and a fresh start (same evidence as `resume`), unless core has already said the launch starts fresh."""
        notes = [ignored_storage(caller_environment, effective_environment, session.home),
                 started_fresh(session, effective_environment) if fresh_start else None]
        return ' '.join(note for note in notes if note) or None

    def startup_prompt_evidence(self, session, prompt, since, environment):
        """For a launch that exited during startup with a wake prompt: whether its Claude recorded the prompt.
        Core makes the message a definite failure only on False; True or None keep it uncertain."""
        return prompt_evidence(session.native_id, prompt, since, launch_storage(session, environment),
                               (session.workspace,))[0]

    def message_id_evidence(self, session, message_id, home):
        """Whether a message accepted before the mod wrote receipts reached this session, from its exact message ID
        alone: ok with {'delivered': True} when a well-formed row of the session's transcript under the Claude storage
        `home` carries it (the queued_command row a mid-turn steer becomes), else unknown with why. Never False: no
        text is matched and no absence is read, so a message typed in as a prompt stays unknown (mv2 update's
        one-time pass for older records).
        """
        if not isinstance(home, str) or not os.path.isabs(home):
            return NativeResult('unknown', f'Claude storage {home!r} is not an absolute path')
        if not session.native_id or not message_id:
            return NativeResult('unknown', "This message's session or ID isn't recorded")
        try:
            paths = session_transcripts(session.native_id, {'CLAUDE_CONFIG_DIR': home}, (session.workspace,))
        except Unreadable as error:
            return NativeResult('unknown', f"Claude's storage folder {error.path} can't be read: restore read access to it")
        proofs, found = {}, False
        for path in paths:
            try:
                lines = read_regular(path).split(b'\n')
            except (OSError, Unreadable):
                return NativeResult('unknown', f"Claude's transcript {path} can't be read: restore read access to it")
            for raw in lines:
                try:
                    row = json.loads(raw)
                    seen = _delivery_row(row)
                except ValueError:
                    continue  # a damaged row elsewhere says nothing about this one
                uuid = row.get('uuid') if isinstance(row, dict) and isinstance(row.get('uuid'), str) and row['uuid'] else None
                if uuid is not None:
                    # Every copy of a row, whatever it is, must agree on its session and what it proves (which
                    # tells its kind apart: only an attachment proves an ID), or the row proves nothing; so each
                    # copy is fingerprinted before anything is filtered out.
                    # A row with no sessionId field is taken as this session's; an explicit null names none.
                    named = ('named', json.dumps(row['sessionId'])) if 'sessionId' in row else ('unnamed',)
                    proofs.setdefault(uuid, set()).add((named, seen))
                elif seen == ('id', message_id) and row.get('sessionId', session.native_id) == session.native_id:
                    found = True
        ours = ('id', message_id)
        this = {('unnamed',), ('named', json.dumps(session.native_id))}
        if found or any(len(copies) == 1 and next(iter(copies))[1] == ours and next(iter(copies))[0] in this
                        for copies in proofs.values()):
            return NativeResult('ok', value={'delivered': True})
        return NativeResult('unknown', 'No row of this session carries the message ID')

    def accepted_message_evidence(self, session, message_id, text, since, home, *, framed=True, cohort=(),
                                  receipts=None, active_acceptances=()):
        """Whether a message Claude accepted reached this session's conversation, read from its transcript under
        the Claude storage `home`: ok with {'delivered': bool}, or unknown with the reason (LC-30).

        Claude keeps accepted prompts only in memory, so one still queued when its process died is gone. Core
        asks once the accepting launch has exited and a later launch on the same session has come up idle, so a
        queue any Claude restored would already have run; False then means the message was lost.
        """
        if not isinstance(home, str) or not os.path.isabs(home):
            return NativeResult('unknown', f'Claude storage {home!r} is not an absolute path')
        directory = Path(session.session_file).parent if session.session_file else None
        answer, why = message_delivered(session.native_id, message_id, text, since, {'CLAUDE_CONFIG_DIR': home},
                                        (session.workspace,), framed, cohort, directory, receipts,
                                        tuple(active_acceptances or ()))
        if answer is None:
            return NativeResult('unknown', why or "This message's session, text or accept time isn't recorded")
        return NativeResult('ok', value={'delivered': answer})

    def startup_prompt_evidence_reason(self, session, prompt, since, environment):
        """Asked after startup_prompt_evidence said None: what can't be read and what to fix, in one line
        without the command (core adds it), or None when Claude has nothing to add."""
        answer, why = prompt_evidence(session.native_id, prompt, since, launch_storage(session, environment),
                                      (session.workspace,))
        return why if answer is None else None

    def _snapshot_flag(self, command):
        """The resume command, with mv2's own `--system-prompt-snapshot off` only where it applies.

        mv2's flag is removed, never a user's, when the harness keeps Claude's snapshot
        (`system_prompt_snapshot: "on"`), when the user passes the flag themselves in `args` or
        `resume_args` (theirs wins), and for a Claude known to be too old for it: only where the
        bridge decision reads the version anyway, since a plain launch never asks which version is
        installed and a version that cannot be read keeps the flag.
        """
        argv = list(command.argv)
        own = self._own_snapshot_flag(argv)
        if own is None:
            return command
        end = argv.index('--') if '--' in argv else len(argv)
        theirs = any(index not in (own, own + 1) and (arg == SNAPSHOT_FLAG or arg.startswith(SNAPSHOT_FLAG + '='))
                     for index, arg in enumerate(argv[:end]))
        keep = not theirs and self.system_prompt_snapshot != 'on'
        if keep and self.bridge_policy != 'plain':
            version = claude_version(argv[0])
            keep = version is None or version >= SNAPSHOT_FLAG_VERSION
        if keep:
            return command
        del argv[own:own + 2]
        return replace(command, argv=tuple(argv))

    @staticmethod
    def _own_snapshot_flag(argv):
        """Where mv2's own `--system-prompt-snapshot off` is, or None: right after the recipe's bootstrap.

        A user's `args` and `resume_args` go in right after the executable, so the recipe's
        `--append-system-prompt` is the last one before any `--`.
        """
        end = argv.index('--') if '--' in argv else len(argv)
        bootstrap = max((index for index in range(end) if argv[index] == '--append-system-prompt'), default=None)
        if bootstrap is not None and argv[bootstrap + 2:bootstrap + 4] == [SNAPSHOT_FLAG, 'off']:
            return bootstrap + 2
        return None

    @staticmethod
    def _reserve_session(command, native_id):
        """The command starting this exact session fresh, or None if it does not resume it.

        Claude never reserved the ID, so `--session-id` takes it over with every
        other flag, and any wake prompt after `--`, unchanged. If a transcript does
        exist, Claude refuses the ID as already in use, so this cannot overwrite one.
        """
        argv = list(command.argv)
        options = argv.index('--') if '--' in argv else len(argv)
        for index in range(options - 1):
            if argv[index] == '--resume' and argv[index + 1] == native_id:
                argv[index] = '--session-id'
                return replace(command, argv=tuple(argv))
        return None

    def _with_bridge(self, command, context):
        # auto uses the bridge unless this Claude cannot run it (and says why), required raises instead of
        # falling back, plain never loads the mod.
        policy = self.bridge_policy
        plain = replace(command, native_mode={'policy': policy, 'mode': 'plain', 'fallback_reason': None})
        if policy == 'plain':
            return self._with_plugins(plain, context)
        problem = bridge_problem(command.argv[0])
        if problem:
            if policy == 'required':
                raise ValueError(problem)
            return self._with_plugins(replace(plain, native_mode={**plain.native_mode, 'fallback_reason': problem}), context)
        socket = context.values['control_socket']
        # mv2 sends from outside the session, which Claude otherwise holds for
        # approval when the session skips permission prompts.
        settings = {'crossSessionInbound': 'accept'}
        environment = {**command.environment, FUNCTION_HOOKS: '1'}
        native_mode = {'policy': policy, 'mode': 'bridge', 'fallback_reason': None}
        # Plugins on in the agent's universe: their Claude directories (by Claude version), settings and environment.
        # The adapter's own settings and environment never change; problems and modes join the launch's own.
        plan = plugins.plan_for(context, 'claude', self.name, version=claude_version(command.argv[0]), bridge=True,
                                base_settings=settings, base_environment=environment)
        if plan.problems:
            native_mode['fallback_reason'] = '; '.join(filter(None, [native_mode['fallback_reason'], *plan.problems]))
        if plan.modes:
            native_mode['mode'] = ' '.join([native_mode['mode'], *plan.modes])
        directories = tuple(argument for directory in plan.claude_plugin_dirs for argument in ('--plugin-dir', directory))
        return replace(command,
            argv=(command.argv[0], '--plugin-dir', str(MOD), '--messaging-socket-path', socket,
                  '--settings', json.dumps(plan.claude_settings), *directories, *command.argv[1:]),
            control_endpoint=NativeEndpoint('unix-lines', socket),
            environment=plan.environment, native_mode=native_mode, plugins=plan.record())

    def _with_plugins(self, command, context):
        """A plain launch: plugins' environment only. It loads no Claude plugin directory (no mod, no function
        hooks) and asks Claude for no version, so each directory is recorded as not fitting."""
        plan = plugins.plan_for(context, 'claude', self.name, version=None, bridge=False,
                                base_environment=command.environment)
        unset = command.unset_env
        if FUNCTION_HOOKS not in plan.environment:
            # A tmux server first started from a Claude session passes its hooks switch on; only the bridge sets it.
            unset = tuple(dict.fromkeys((*unset, FUNCTION_HOOKS)))
        return replace(command, environment=plan.environment, unset_env=unset, plugins=plan.record())

    def session_home(self, environment):
        """Claude's storage for a launch: CLAUDE_CONFIG_DIR, else HOME/.claude, of that launch's environment."""
        return os.path.realpath(claude_home(environment))

    def session_storage_evidence(self, session, home):
        """Whether this session's own transcript is under the Claude storage `home` (an absolute storage root,
        as session_home gives it): ok with {'present': bool}, or unknown with the reason.

        Core adopts a legacy agent's storage only when exactly one candidate home says present and the
        others absent (LC-28). Absent means every project folder that could hold it was read (the folder
        rule): another workspace's unreadable folder doesn't count, but one that could hold it, or an
        unreadable projects folder, is unknown. An absent answer says `store_read`: true when the
        conversation store (projects/) existed and was read, false when it doesn't exist or is a dangling
        link, with a reason naming it. Only core knows whether a home is the caller's own destination or a
        recorded one, where absence counts only if the store was read.
        """
        if not session.native_id:
            return NativeResult('unknown', 'No Claude session ID to look for')
        if not isinstance(home, str) or not os.path.isabs(home):
            return NativeResult('unknown', f'Claude storage {home!r} is not an absolute path')
        try:
            found, store_read = _scan_transcripts(session.native_id, {'CLAUDE_CONFIG_DIR': home},
                                                  (session.workspace,), links=True)
        except Unreadable as error:
            return NativeResult('unknown', f"Claude's storage folder {error.path} can't be read")
        for path in found:  # presence is the session's own rows in a regular file, never a name
            problem = transcript_identity(path, session.native_id)
            if problem:
                return NativeResult('unknown', problem)
        if found:
            return NativeResult('ok', value={'present': True})
        if not store_read:  # this call's own listing found no store; a later look would prove nothing
            return NativeResult('ok', f"Claude's conversation store {Path(home) / 'projects'} doesn't exist",
                                value={'present': False, 'store_read': False})
        return NativeResult('ok', value={'present': False, 'store_read': True})

    def observed_binding(self, session):
        """The session this launch's exact process is on now, from Claude's own process file, or None.

        A /clear moves the same process to a new session; core compares it with the binding.
        """
        record = process_record(session)
        if record is None:
            return None
        try:
            native_id = normalize_session_id(record.get('sessionId'))
        except (TypeError, ValueError):
            return None
        return ObservedBinding(native_id=native_id, home=session.home or os.path.realpath(claude_home()),
                               evidence='claude-process-file')

    def connection_problem(self, session):
        """Why messages cannot reach this live launch when its process relaunched itself, else None."""
        if session.state != 'running' or not session.endpoint or not self._bridge(session).launched:
            return None
        if peer_token(session.endpoint.address, session.home):
            return None
        target, reason = self._inbox(session)
        return reason if target is None and reason == RELAUNCHED else None

    def _inbox(self, session):
        """(session to write to, its inbox key), or (None, why not).

        The launch's own socket while Claude publishes its key. After /restart the same
        process listens on its own socket instead (its process file names it, with the
        same session and start): delivery follows it there. Nothing is written.
        """
        token = peer_token(session.endpoint.address, session.home)
        if token is not None:
            return session, token
        if session.pid_started is None:
            # Its start was not recorded: the file cannot be tied to this launch, so no heal.
            return None, (RELAUNCHED if unverified_relaunch(session) else NO_KEY)
        record = process_record(session)
        path = record.get('messagingSocketPath') if record else None
        try:
            same = record is not None and normalize_session_id(record.get('sessionId')) == normalize_session_id(session.native_id)
        except (TypeError, ValueError):
            same = False
        if not same or not isinstance(path, str) or os.path.abspath(path) == os.path.abspath(session.endpoint.address):
            return None, NO_KEY
        token = peer_token(path, session.home)
        if token is None or not os.path.exists(path):
            return None, RELAUNCHED
        return replace(session, endpoint=NativeEndpoint('unix-lines', path)), token

    def _bridge(self, session):
        return bridge_of(getattr(self, 'settings', {}).get('inbox_bridge'), session)

    def attachment_ready(self, session):
        """A launch with no inbox is ready once its process runs; a bridged one when the mod has registered.

        Core asks this on every readiness poll, so it stays cheap and independent of the other capabilities.
        """
        return not session.endpoint or self._bridge(session).ready

    def capabilities(self, session, executor=None):
        result = super().capabilities(session, executor)
        result['session_name'] = Capability('supported', mechanism='--name')
        if not session.agent_id:  # `mv2 capabilities --harness`: no agent, so no launch to judge
            return {**result, **self._would_support(executor)}
        # The launch's own record decides whether it used the bridge: an auto launch may have fallen back to plain.
        bridge = self._bridge(session)
        for name in ('send_steer', 'send_follow_up'):
            result[name] = (Capability('supported', mechanism='claude-peer-inbox', required=True) if bridge.ready
                            else Capability('unavailable', bridge.reason,
                                mechanism='launch-only' if bridge.final else None, required=True))
        if bridge.launched and session.session_file:
            result['get_delivery'] = Capability('supported', mechanism='claude-mod-receipt')
        result['ask'] = (Capability('supported', mechanism='claude-model-fork') if bridge.ready and session.session_file
                         else Capability('unavailable', bridge.reason or 'No agent directory recorded'))
        if session.native_id:
            result['get_usage'] = Capability('supported', mechanism='claude-transcript')
        return result

    def _would_support(self, executor):
        """With no launch (`mv2 capabilities --harness`): what this harness's launches get, from its setting and,
        with --probe, the installed Claude Code. Reading the version runs claude, so only --probe does."""
        if self.bridge_policy == 'plain':
            found, why = None, 'Agents of this harness are launch-only: it sets inbox_bridge to false'
        elif executor is None:
            found = (f'Through the multiverse mod, on Claude Code {".".join(map(str, MIN_BRIDGE_VERSION))} or newer; '
                     '--probe checks the installed version')
            why = None
        else:
            problem = bridge_problem(self.spec['spawn'][0])
            found = None if problem else 'Through the multiverse mod'
            why = problem and (f'Launches of this harness refuse to start: {problem}' if self.bridge_policy == 'required'
                               else f'Agents of this harness launch without the bridge: {problem}')
        mechanisms = {'send_steer': 'claude-peer-inbox', 'send_follow_up': 'claude-peer-inbox',
                      'ask': 'claude-model-fork', 'get_delivery': 'claude-mod-receipt'}
        result = {name: (Capability('supported', found, mechanism, required=name.startswith('send_')) if found
                         else Capability('unavailable', why, required=name.startswith('send_')))
                  for name, mechanism in mechanisms.items()}
        # Usage is measured from the session's transcript, so every launch has it, connected or not.
        result['get_usage'] = Capability('supported', mechanism='claude-transcript')
        return result

    def get_usage(self, session, executor=None):
        """Transcript totals, plus the mod's context/cost/rate limits while they belong to this launch."""
        if not session.native_id:
            return NativeResult('unavailable', 'No Claude session ID recorded')
        reported = {}
        try:
            # The agent's runtime record; a missing one leaves nothing reported.
            facts = read(Path(session.session_file).parent / 'runtime.json')
            mod = facts.get('usage')
            if isinstance(mod, dict) and session.launch_id and facts.get('launch_id') == session.launch_id:
                reported = {key: mod[key] for key in ('context', 'cost_usd', 'rate_limits') if key in mod}
                reported['context_reported_at'] = mod.get('reported_at')
        except (OSError, ValueError, TypeError):
            pass
        try:
            # The launch's own storage (session.home, pinned before it started), not this process's: a dashboard
            # or CLI with another CLAUDE_CONFIG_DIR would read another storage (LC-26).
            storage = launch_storage(session, None)
            usage = measured_usage(session.native_id, (session.workspace,), storage)
            reading = context_reading(session.native_id, (session.workspace,), storage)
        except Unreadable as error:
            return NativeResult('error', f"Claude's transcript storage can't be read: {error}")
        context = dict(reported['context']) if isinstance(reported.get('context'), dict) else {}
        responded = moment(reading[1]) if reading else None
        reported_at = moment(reported.get('context_reported_at'))
        if reading and (context.get('tokens') is None or (responded and (reported_at is None or reported_at < responded))):
            # No report from the mod yet, or a response newer than it: the transcript has the real request size.
            context = {key: value for key, value in context.items() if key not in ('tokens', 'percent')}
            context['tokens'] = reading[0]
        if reading and reading[1]:
            # When the latest main-thread response came: a plugin's estimate (compaction's) stands only until one does.
            context['responded_at'] = reading[1]
        if context:
            reported['context'] = context
        if usage is None:
            if reported:
                return NativeResult('ok', 'Reported by the multiverse mod', {'native_session_id': session.native_id, **reported})
            return NativeResult('unavailable', 'Claude has not written a transcript for this session yet')
        return NativeResult('ok', 'Measured from the Claude session transcript', {**asdict(usage), **reported})

    def send(self, session, message, executor):
        mode = MODES.get(message.delivery)
        if mode is None:
            return NativeResult('unavailable', f'Unknown delivery mode: {message.delivery}')
        bridge = self._bridge(session)
        if not bridge.ready:
            return unavailable(bridge)
        target, token = self._inbox(session)
        if target is None:
            return NativeResult('unavailable', token)
        # Always `next`: the mod decides from the tag when a follow-up runs.
        # When the sender sent it, for the age the mod adds once the model gets it; only a plain timestamp, so the
        # attribute can never end the tag.
        sent = (f' sent={message.sent_at}' if session.bridge == BRIDGE and isinstance(message.sent_at, str)
                and SENT.fullmatch(message.sent_at) else '')
        try:
            opening = f'<<MV2 {mode} id={normalize_session_id(message.id)}{sent}>>'
        except ValueError:
            opening = f'<<MV2 {mode}>>'  # no receipt without a UUID
        frame = {'type': 'user', 'priority': 'next', 'session_id': session.native_id, 'msg_id': message.id,
                 'message': {'role': 'user', 'content': opening + (message.framed or message.text) + CLOSE}}
        written = executor.write_lines(target, [json.dumps({'type': 'auth', 'token': token}),
                                                json.dumps(frame, ensure_ascii=False)])
        if written.state != 'ok':
            return written
        return NativeResult('native_queued', 'Written to the Claude session inbox; the mod reports delivery')

    def ask(self, session, question, executor):
        """Answer a side question with $.model.fork over the session's transcript.

        The question travels tagged `ask` over the peer inbox; the mod consumes it,
        so nothing enters the conversation, and writes claude-asks/<id>.json.
        """
        bridge = self._bridge(session)
        if not bridge.ready:
            return unavailable(bridge)
        if not session.session_file:
            return NativeResult('unavailable', 'No agent directory recorded')
        target, token = self._inbox(session)
        if target is None:
            return NativeResult('unavailable', token)
        question_id = str(uuid4())
        answer = Path(session.session_file).parent / 'claude-asks' / f'{question_id}.json'
        frame = {'type': 'user', 'priority': 'next', 'session_id': session.native_id, 'msg_id': question_id,
                 'message': {'role': 'user', 'content': f'<<MV2 ask id={question_id}>>{question}{CLOSE}'}}
        written = executor.write_lines(target, [json.dumps({'type': 'auth', 'token': token}),
                                                json.dumps(frame, ensure_ascii=False)])
        if written.state != 'ok':
            return written
        deadline = time.monotonic() + ASK_TIMEOUT
        while time.monotonic() < deadline:
            try:
                record = json.loads(answer.read_text())
            except (OSError, ValueError):  # not written yet, or mid-write
                time.sleep(0.2)
                continue
            answer.unlink(missing_ok=True)
            if record.get('answered'):
                return NativeResult('ok', 'Answered over the session transcript; nothing was added to it',
                                    {'answer': record.get('text', ''), 'usage': record.get('usage')})
            if record.get('reason') == 'nothing-to-fork':
                # Claude forks the main thread's last request in this process; a new or resumed process has none
                # until a turn ends, though the conversation it resumed is all there.
                return NativeResult('unavailable', "This launch hasn't finished a turn yet: Claude answers side "
                                    "questions only after one, so ask again after the agent's next response")
            return NativeResult('error', f'Claude did not answer: {record.get("reason", "unknown reason")}')
        return NativeResult('unknown', f'No answer within {ASK_TIMEOUT} s; the question may still be answered')

    def get_delivery(self, session, message_id, executor=None):
        """The mod's receipt, written when the message's row entered the conversation."""
        if not session.session_file:
            return NativeResult('unavailable', 'No agent directory recorded')
        try:
            receipt = Path(session.session_file).parent / 'claude-receipts' / f'{normalize_session_id(message_id)}.json'
        except ValueError:
            return NativeResult('error', 'Message ID must be a UUID')
        if not receipt.is_file():
            return NativeResult('unknown', 'No delivery receipt from the multiverse mod yet')
        value = json.loads(receipt.read_text())
        return NativeResult('native_queued', 'Delivered into the Claude conversation',
                            {'transcript_confirmed': True, 'route': value.get('route'), 'delivered_at_ms': value.get('at')})
