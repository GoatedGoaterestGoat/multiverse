"""tmux identifies terminals, not agents. Tags bind a pane to one launch."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

from . import statusline

FIELDS = ("pane_id", "session_id", "pane_dead", "@mv2_agent_id", "@mv2_universe_id", "@mv2_launch_id", "pane_pid")


class Tmux:
    def __init__(self, socket_path=None):
        self.prefix = ["tmux", "-S", socket_path] if socket_path else ["tmux", "-L", "multiverse-v2"]

    def run(self, *args):
        result = subprocess.run([*self.prefix, *args], text=True, capture_output=True, timeout=10)
        if result.returncode or result.stderr.startswith("error "):
            raise RuntimeError(result.stderr.strip() or "tmux command failed")
        return result.stdout.rstrip("\n")

    def snapshot(self):
        try:
            output = self.run("list-panes", "-a", "-F", "\t".join("#{" + field + "}" for field in FIELDS))
            return {row[0]: dict(zip(FIELDS, row)) for line in output.splitlines()
                    if len(row := line.split("\t")) == len(FIELDS)}, None
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            reason = str(error)
            # An absent server is different from one we lack permission to inspect.
            # tmux before 3.3 fails `list-panes -a` on a running server with no
            # sessions ("no current target"); that server has no panes either.
            if ("no server running" in reason or "No such file or directory" in reason and "connecting to" in reason
                    or reason == "no current target"):
                return {}, None
            return {}, reason

    def create(self, name, cwd, argv):
        options = []
        for key in ("PATH", "PYTHONPATH", "MV2_CLI", "MV2_PYTHON"):
            if key in os.environ:
                options.extend(["-e", f"{key}={os.environ[key]}"])
        result = self.run("new-session", "-d", "-P", "-F", "#{socket_path}\t#{session_id}\t#{pane_id}",
                          "-s", name, "-c", str(cwd), *options, *argv)
        if len(result.split("\t")) != 3:
            # Some tmux versions return success even if server startup failed.
            raise RuntimeError(f"tmux did not return a pane identity: {result!r}")
        socket_path, session_id, pane_id = result.split("\t")
        return {"socket": socket_path, "session": session_id, "pane": pane_id}

    def tag(self, pane, properties, launch_id):
        for key, value in (("agent_id", properties["id"]), ("universe_id", properties["universe_id"]), ("launch_id", launch_id)):
            self.run("set-option", "-p", "-t", pane, f"@mv2_{key}", value)
        # Preserve the terminal after native exit, independent of the user's
        # tmux defaults. Core removes this wrapper explicitly on kill/resume.
        self.run("set-option", "-p", "-t", pane, "remain-on-exit", "on")

    def configure_status(self, terminal, root, properties, launch_id, family):
        """Configure only a newly created, tagged agent wrapper before native startup."""
        session, pane = terminal['session'], terminal['pane']
        self.run('set-option', '-w', '-t', pane, 'automatic-rename', 'off')
        self.run('set-option', '-w', '-t', pane, 'allow-rename', 'off')
        self.run('rename-window', '-t', pane, statusline.clean(properties['name']) or 'agent')
        _, interval = statusline.config(statusline.read_object(root / 'statusline.json'), family)
        argv = [sys.executable, '-S', str(Path(statusline.__file__).resolve()),
                str(root), properties['id'], launch_id, family]
        # Indirection keeps path text out of tmux format parsing: option values
        # are inserted literally (without recursive expansion). Shell quoting
        # still applies; the server's environment may be another agent's.
        command = shlex.join(argv)
        for name, value in (('@mv2_status_command', command), ('status', 'on'),
                            ('status-position', 'bottom'), ('status-interval', str(interval)),
                            ('status-left', ''), ('status-right', ''),
                            ('status-format[0]', ' #(#{@mv2_status_command})')):
            self.run('set-option', '-t', session, name, value)

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
                                capture_output=True, text=True, timeout=2)
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


def observe(properties, runtime, snapshots=None):
    terminal = runtime.get("tmux")
    if not terminal:
        return {"live": "dormant" if runtime.get("state") in ("stopped", "failed", "dormant") or not runtime else "unknown",
                "reason": runtime.get("error", "No recorded pane")}
    backend = Tmux(terminal["socket"])
    if snapshots is not None:
        if terminal["socket"] not in snapshots:
            snapshots[terminal["socket"]] = backend.snapshot()
        panes, error = snapshots[terminal["socket"]]
    else:
        panes, error = backend.snapshot()
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
        raise ValueError(f'Cannot operate on agent: {result["live"]}: {result["reason"]}')
    return Tmux(runtime["tmux"]["socket"]), runtime["tmux"]["pane"]
