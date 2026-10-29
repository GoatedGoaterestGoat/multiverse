"""The record of a plugin's one-shot runs (1.14): one line per run in state/plugins/NAME/runs.jsonl of the run's
universe, metadata only (never messages, prompt, system or reply text), so `mv2 plugins info NAME` can say what the
plugin ran and spent. Written by mv2 after each run, whatever its outcome; rotated with the plugin's other logs."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from . import loader

FILE = "runs.jsonl"
FIELDS = ("at", "harness", "model", "agent", "input_tokens", "output_tokens", "cache_read_tokens",
          "cache_write_tokens", "cost_usd", "duration", "outcome")


def path(root, plugin):
    return Path(root) / "state" / "plugins" / plugin / FILE


def record(root, plugin, **fields):
    """Append one run's metadata; best effort, never raises. A universe that is gone gets no record."""
    line = {name: fields.get(name) for name in FIELDS}
    line["at"] = line["at"] or datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        loader.append_log(path(root, plugin), json.dumps(line, sort_keys=True), root=root)
    except (OSError, ValueError):
        pass


def summary(root, plugin, since=timedelta(days=1)):
    """{runs, failed, cost_usd, outcomes} for this plugin's runs here in the last `since`, or None without a
    record. Damaged lines are skipped."""
    newest = path(root, plugin)
    files = [file for file in (newest.with_name(FILE + ".1"), newest) if file.is_file()]
    if not files:
        return None
    cutoff = datetime.now(timezone.utc) - since
    runs, cost, outcomes = 0, 0.0, {}
    for file in files:
        try:
            lines = file.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for text in lines:
            try:
                item = json.loads(text)
                at = datetime.fromisoformat(item["at"])
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(item, dict) or at.tzinfo is None or at < cutoff:
                continue
            runs += 1
            outcome = item.get("outcome") if isinstance(item.get("outcome"), str) else "failed"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if isinstance(item.get("cost_usd"), (int, float)) and not isinstance(item.get("cost_usd"), bool):
                cost += item["cost_usd"]
    return {"runs": runs, "failed": runs - outcomes.get("ok", 0), "cost_usd": round(cost, 6), "outcomes": outcomes}
