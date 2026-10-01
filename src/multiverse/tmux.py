"""tmux identifies terminals, not agents. Tags bind a pane to one launch."""
import os
import subprocess

FIELDS = ("pane_id", "session_id", "pane_dead", "@mv2_agent_id", "@mv2_universe_id", "@mv2_launch_id")


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
            if "no server running" in reason or "No such file or directory" in reason and "connecting to" in reason:
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


def matches(pane, properties, runtime):
    return bool(pane and all(pane.get(field) == value for field, value in (
        ("@mv2_agent_id", properties["id"]),
        ("@mv2_universe_id", properties["universe_id"]),
        ("@mv2_launch_id", runtime.get("launch_id")),
    )))


def observe(properties, runtime, snapshots=None):
    terminal = runtime.get("tmux")
    if not terminal:
        return {"live": "stopped" if runtime.get("state") in ("stopped", "failed") else "unknown",
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
        return {"live": "stopped", "reason": "Recorded pane is absent"}
    if not matches(pane, properties, runtime):
        return {"live": "stopped", "reason": "Pane belongs to a different launch"}
    return {"live": "stopped" if pane["pane_dead"] == "1" else "alive", "reason": ""}


def owned(properties, runtime):
    result = observe(properties, runtime)
    if result["live"] != "alive":
        raise ValueError(f'Cannot operate on agent: {result["live"]}: {result["reason"]}')
    return Tmux(runtime["tmux"]["socket"]), runtime["tmux"]["pane"]
