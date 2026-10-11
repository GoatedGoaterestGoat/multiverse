"""An Uptime column and an inspector line: how long each running agent's current launch has run.

Turn it on with `mv2 plugins enable hello-column` (with MV2_PLUGIN_PATH pointing
at this directory's parent, or this directory copied to ~/.multiverse/plugins/),
then add "uptime" (or "hello-column:uptime") to a view's `columns`.
"""
from datetime import datetime, timezone


def uptime(row, now=None):
    """`2h 05m` for a running agent, `—` otherwise. Reads only the row, so it is quick on every refresh."""
    runtime = row.get("runtime") or {}
    if row.get("live") != "alive" or not runtime.get("started_at"):
        return "—"
    started = datetime.fromisoformat(runtime["started_at"])
    seconds = int(((now or datetime.now(timezone.utc)) - started).total_seconds())
    if seconds < 0:
        return "—"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def register(mv):
    mv.dashboard.column("uptime", "Uptime", 8, uptime, compact=False)

    @mv.dashboard.inspector
    def launched(row):
        started = (row.get("runtime") or {}).get("started_at")
        return f"Launched {started[:16].replace('T', ' ')} UTC" if started else None
