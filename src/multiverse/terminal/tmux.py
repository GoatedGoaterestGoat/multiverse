"""tmux identifies terminals, not agents. Tags bind a pane to one launch."""
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys

from . import recovery, statusline

FIELDS = ("pane_id", "session_id", "pane_dead", "@mv2_agent_id", "@mv2_universe_id", "@mv2_launch_id", "pane_pid")


def _no_join_only_option(error):
    # Older tmux has no -N. Its ordinary new-session is allowed only while
    # holding our creation/recovery lock, after the normal proof checks.
    return re.search(r"\b(?:unknown|invalid|illegal) option(?:\s+--|\s*:)?\s+['\"]?-?N\b", str(error)) is not None


def status_setup(root, properties, launch_id, family):
    """Prepare pane-independent status configuration before creating the waiting runner."""
    _, interval = statusline.config(statusline.read_object(root / 'statusline.json'), family)
    argv = [sys.executable, '-S', str(Path(statusline.__file__).resolve()),
            str(root), properties['id'], launch_id, family]
    # Indirection inserts the option literally, without recursive tmux format expansion.
    # The renderer command still needs shell quoting for unusual paths and text.
    options = (('@mv2_status_command', shlex.join(argv)), ('status', 'on'),
               ('status-position', 'bottom'), ('status-interval', str(interval)),
               ('status-left', ''), ('status-right', ''),
               ('status-format[0]', ' #(#{@mv2_status_command})'))
    return statusline.clean(properties['name']) or 'agent', options


class Tmux:
    pane_format = "\t".join("#{" + field + "}" for field in (*FIELDS, "pid", "start_time"))

    def __init__(self, socket_path=None, *, legacy=False):
        from ..runtime.paths import run_directory
        self.default = not socket_path and not legacy
        self.prefix = (["tmux", "-L", "multiverse-v2"] if legacy else
                       ["tmux", "-S", str(socket_path or run_directory() / 'tmux.sock')])

    def run(self, *args):
        try:
            return self._run(*args)
        except RuntimeError as error:
            reason = str(error)
            if not ('no server running' in reason or 'connecting to' in reason):
                raise
            with recovery.serialized(recovery.address(self)):
                if recovery.connected(self) is None:
                    raise error
            return self._run(*args)

    def _run(self, *args):
        # A tmux client resets stdin's O_NONBLOCK flag, even for capture/list commands.
        # Never share the native TUI's terminal file description with these helpers.
        result = subprocess.run([*self.prefix, *args], stdin=subprocess.DEVNULL,
                                text=True, capture_output=True, timeout=10)
        if result.returncode or result.stderr.startswith("error "):
            raise RuntimeError(result.stderr.strip() or "tmux command failed")
        return result.stdout.rstrip("\n")

    def environment(self, name):
        """One global value, preserving embedded/trailing newlines and explicit empty."""
        try:
            return self._environment(name)
        except RuntimeError as error:
            if not ('no server running' in str(error) or 'connecting to' in str(error)):
                raise
            with recovery.serialized(recovery.address(self)):
                if recovery.connected(self) is None:
                    raise error
            return self._environment(name)

    def _environment(self, name):
        result = subprocess.run([*self.prefix, 'show-environment', '-g', name], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=10)
        if (result.returncode == 1 and not result.stdout
                and result.stderr == ('unknown variable: ' + name + '\n').encode()):
            return None
        if result.returncode or result.stderr.startswith(b'error '):
            raise RuntimeError(result.stderr.decode(errors='replace').strip() or 'tmux command failed')
        output = result.stdout.decode()  # No universal-newline conversion or trailing-value trimming.
        if output == '-' + name + '\n':
            return None
        if not output.startswith(name + '=') or not output.endswith('\n') or '\0' in output:
            raise ValueError(f'tmux returned an unreadable environment value for {name}')
        value = output[len(name) + 1:-1]  # Remove exactly the producer's single final newline.
        if '\\' in value:
            # Some tmux versions escape controls, others emit them literally.
            # Do not confuse an escape with a real path that has no transcript.
            raise ValueError(f'tmux returned ambiguous escaped output for {name}')
        return value

    def snapshot(self, *, facts=None):
        try:
            answer = recovery.observe(self, facts=facts)
            if answer is None:
                return {}, None
            output, _ = answer
            return {row[0]: dict(zip(FIELDS, row)) for line in output.splitlines()
                    if len(row := line.split("\t")) == len(FIELDS)}, None
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            reason = recovery.UnreachableReason(str(error)) if isinstance(error, recovery.Unreachable) else str(error)
            return {}, reason

    def create(self, name, cwd, argv):
        if self.default:
            from ..runtime.paths import prepare
            prepare(Path(self.prefix[2]).parent)
        options = []
        for key in ("PATH", "PYTHONPATH", "MV2_CLI", "MV2_PYTHON"):
            if key in os.environ:
                options.extend(["-e", f"{key}={os.environ[key]}"])
        command = ("new-session", "-d", "-P", "-F", "#{socket_path}\t#{session_id}\t#{pane_id}",
                   "-s", name, "-c", str(cwd), *options, *argv)
        result = None
        unsupported = False
        if recovery.healthy(self) is not None:
            try:
                # -N joins only: a cleaner after the probe must not make tmux
                # create a second server while the first still lives.
                result = self._run('-N', *command)
            except RuntimeError as error:
                unsupported = _no_join_only_option(error)
                if not (unsupported or 'no server running' in str(error) or 'connecting to' in str(error)):
                    raise
            else:
                server = self._created_server()
        if result is None:
            with recovery.serialized(recovery.address(self)):
                answer = recovery.connected(self)
                if answer is not None and not unsupported:
                    try:
                        result = self._run('-N', *command)
                    except RuntimeError as error:
                        if not _no_join_only_option(error):
                            raise
                if result is None:
                    result = self._run(*command)
                server = self._created_server(locked=True)
        if len(result.split("\t")) != 3:
            # Some tmux versions return success even if server startup failed.
            raise RuntimeError(f"tmux did not return a pane identity: {result!r}")
        socket_path, session_id, pane_id = result.split("\t")
        return {"socket": socket_path, "session": session_id, "pane": pane_id, "server": server}

    def _created_server(self, *, locked=False):
        # Once new-session returned its pane identity, hand it to lifecycle even
        # if optional signal-proof bookkeeping fails. Otherwise that live pane
        # would escape tagging and launch-failure cleanup.
        try:
            answer = recovery.connected(self) if locked else recovery.observe(self)
            return answer[1] if answer is not None else None
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return None

    def ensure(self, root):
        """Seed old launch proofs before a spawn can create a server on this path."""
        from .. import universe
        from ..universe import records
        socket = recovery.address(self)
        while parent := universe.parent(root):
            root = parent
        paths = [path for scope in universe.tree(root)
                 for path in (scope / 'state' / 'agents').glob('*/runtime.json')]
        # calling_socket already verified this caller before lifecycle selects
        # the backend. A child may be created in a different universe.
        caller = os.environ.get('MV2_AGENT_DIR')
        if caller:
            from ..runtime.agent_identity import sender_actor
            try:
                actor = sender_actor()
            except (OSError, ValueError):
                actor = None
            if actor is not None:
                paths.append(actor.directory / 'runtime.json')
        for path in paths:
            try:
                facts = records.runtime(path)
                terminal = facts.get('tmux') or {}
                named = terminal.get('socket')
                if not named or Path(named).resolve() != socket:
                    continue
                server = terminal.get('server')
                if isinstance(server, dict) and server.get('socket') != str(socket):
                    continue
            except (OSError, ValueError, TypeError, AttributeError):
                continue  # An invalid record is never authority for a signal.
            if recovery.observe(self, facts=facts) is not None:
                return
        recovery.observe(self)

    def tag(self, pane, properties, launch_id):
        for key, value in (("agent_id", properties["id"]), ("universe_id", properties["universe_id"]), ("launch_id", launch_id)):
            self.run("set-option", "-p", "-t", pane, f"@mv2_{key}", value)
        # Preserve the terminal after native exit, independent of the user's
        # tmux defaults. Core removes this wrapper explicitly on kill/resume.
        self.run("set-option", "-p", "-t", pane, "remain-on-exit", "on")

    def respawn(self, properties, runtime, cwd, argv):
        """Replace only a dead owned runner, preserving its attached clients."""
        panes, error = self.snapshot()
        pane = panes.get(runtime['tmux']['pane'])
        if error or not matches(pane, properties, runtime) or pane['pane_dead'] != '1':
            raise ValueError(error or 'Automatic reconnect requires the dead owned pane')
        self.run('respawn-pane', '-t', pane['pane_id'], '-c', str(cwd), *argv)
        return runtime['tmux']

    def configure_status(self, terminal, root, properties, launch_id, family, *, prepared=None):
        """Configure only a newly created, tagged agent wrapper before native startup."""
        session, pane = terminal['session'], terminal['pane']
        name, options = prepared if prepared is not None else status_setup(root, properties, launch_id, family)
        commands = [('set-option', '-w', '-t', pane, 'automatic-rename', 'off'),
                    ('set-option', '-w', '-t', pane, 'allow-rename', 'off'),
                    ('rename-window', '-t', pane, name),
                    *(('set-option', '-t', session, key, value) for key, value in options)]
        # Literal argv separators, never a shell command. Every command retains its explicit target.
        arguments = []
        for command in commands:
            if arguments:
                arguments.append(';')
            arguments.extend(command)
        self.run(*arguments)

    def signal_child(self, properties, runtime, signum):
        """Signal only the recorded child of this launch's currently owned runner."""
        panes, error = self.snapshot()
        if error:
            raise RuntimeError(error)
        pane = panes.get(runtime['tmux']['pane'])
        if not matches(pane, properties, runtime):
            raise ValueError('Pane ownership changed; refusing to signal a process')
        if pane['pane_dead'] == '1' or 'exit_code' in runtime:
            return
        runner_pid = int(pane['pane_pid'])
        if runtime.get('runner_pid', runner_pid) != runner_pid:
            raise ValueError('Pane runner changed; refusing to signal a process')
        child_pid = runtime.get('pid')
        if not isinstance(child_pid, int) or child_pid <= 0 or runner_pid <= 0:
            raise ValueError('Native process has not registered yet; retry after startup')
        parent = subprocess.run(['ps', '-p', str(child_pid), '-o', 'ppid='],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2)
        if parent.returncode == 1 and not parent.stdout.strip():
            return  # The native child exited; its runner is finishing cleanup.
        if parent.returncode or parent.stdout.strip() != str(runner_pid):
            raise ValueError('Native process no longer belongs to the owned runner; refusing to signal it')
        try:
            os.kill(child_pid, signum)
        except ProcessLookupError:
            pass  # Exited between identity validation and the signal.


def matches(pane, properties, runtime):
    return bool(pane and all(pane.get(field) == value for field, value in (
        ("@mv2_agent_id", properties["id"]),
        ("@mv2_universe_id", properties["universe_id"]),
        ("@mv2_launch_id", runtime.get("launch_id")),
    )))


def observation_key(properties, runtime):
    """Fields used by observe(), copied so reconciliation cannot mutate a cache key."""
    terminal = runtime.get('tmux')
    fields = {key: runtime[key] for key in ('launch_id', 'state', 'exit_code', 'ending_at', 'error') if key in runtime}
    return (properties.get('id'), properties.get('universe_id'), fields,
            dict(terminal) if isinstance(terminal, dict) else terminal)


def observe(properties, runtime, snapshots=None):
    terminal = runtime.get("tmux")
    if not terminal:
        return {"live": "dormant" if runtime.get("state") in ("stopped", "failed", "dormant") or not runtime else "unknown",
                "reason": runtime.get("error", "No recorded pane")}
    backend = Tmux(terminal["socket"])
    if snapshots is not None:
        if terminal["socket"] not in snapshots:
            snapshots[terminal["socket"]] = backend.snapshot(facts=runtime)
        panes, error = snapshots[terminal["socket"]]
    else:
        panes, error = backend.snapshot(facts=runtime)
    # Observe the recorded local terminal; a saved hostname is provenance,
    # not proof that a missing pane is running somewhere else.
    if error:
        return {"live": "unknown", "reason": error}
    pane = panes.get(terminal["pane"])
    if not pane:
        return {"live": "dormant", "reason": "Recorded pane is absent"}
    if not matches(pane, properties, runtime):
        return {"live": "dormant", "reason": "Pane belongs to a different launch"}
    live = ('stopped' if pane['pane_dead'] == '1' or 'exit_code' in runtime else
            'ending' if runtime.get('state') == 'ending' or runtime.get('ending_at') else 'alive')
    return {"live": live, "reason": ""}


def owned(properties, runtime, *, states=('alive',)):
    result = observe(properties, runtime)
    if result["live"] not in states:
        if isinstance(result['reason'], recovery.UnreachableReason):
            raise recovery.Unreachable(result['reason'])
        reason = f': {result["reason"]}' if result["reason"] else ''
        raise ValueError(f'Cannot operate on agent: {result["live"]}{reason}')
    return Tmux(runtime["tmux"]["socket"]), runtime["tmux"]["pane"]
