"""A desktop notification when an agent needs you, fails, or reports blocked or done.

It runs in the plugin host, so notifications come whether or not the
dashboard is open. Turn it on with `mv2 plugins enable notifier`; the host
starts with `mv2 plugins host start` (the dashboard starts it too). Settings:
plugin-settings/notifier.json. macOS uses `osascript`, Linux `notify-send`;
anywhere else it only writes state/plugins/notifier/plugin.log.

Two policies a handler should have, shown here: skip events too old to matter
(after the host was stopped for a while, it catches up), and do not repeat
itself (events can arrive more than once).
"""
import shutil
import subprocess
import sys
import time

DEFAULTS = {"notify": {"waiting": True, "failed": True, "blocked": True, "done": True},
            "max_age_seconds": 300, "quiet_seconds": 30}


def message(event):
    """(kind, title, text) for an event worth a notification, else None."""
    name = (event.agent or {}).get("name") or "an agent"
    if event.type == "agent.status" and event.data.get("to") == "waiting":
        return "waiting", f"{name} needs you", "A permission prompt, a question or a plan is waiting."
    if event.type == "agent.exited" and event.data.get("state") == "failed":
        return "failed", f"{name} stopped with an error", event.data.get("reason") or f"exit code {event.data.get('exit_code')}"
    if event.type == "agent.reported" and event.data.get("phase") in ("blocked", "done"):
        phase = event.data["phase"]
        return phase, f"{name} is {phase}", event.data.get("summary") or ""
    return None


def show(title, text):
    """Show a desktop notification; False when this system has no way to."""
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = f"display notification {_quoted(text)} with title {_quoted(title)}"
        return subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10).returncode == 0
    if shutil.which("notify-send"):
        return subprocess.run(["notify-send", title, text], capture_output=True, timeout=10).returncode == 0
    return False


def _quoted(text):
    return '"' + str(text)[:300].replace("\\", "\\\\").replace('"', '\\"') + '"'


def register(mv, show=show, clock=time.monotonic):
    last = {}  # (agent id, kind) -> when it last notified

    def settings(universe):
        values, problem = mv.settings.read(universe)
        return DEFAULTS if problem else {**DEFAULTS, **values, "notify": {**DEFAULTS["notify"], **values.get("notify", {})}}

    def notify(event):
        found = message(event)
        if found is None:
            return
        kind, title, text = found
        current = settings(event.universe)
        if not current["notify"].get(kind) or event.age > current["max_age_seconds"]:
            return
        key = ((event.agent or {}).get("id"), kind)
        if key in last and clock() - last[key] < current["quiet_seconds"]:
            return
        last[key] = clock()
        if not show(title, text):
            mv.log(f"{title}: {text}", event.universe)

    # Exactly the types plugin.json subscribes to: what mv2 records for this plugin.
    for kind in ("agent.status", "agent.exited", "agent.reported"):
        mv.events.on(kind, notify)
