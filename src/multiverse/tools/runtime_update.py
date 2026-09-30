"""Report native harness status for the current launch.

For harness integrations (the Claude mod, the Pi extension), run as
`python -m multiverse.tools.runtime_update`; it is not an `mv2` command.
"""
import json

from .. import agents
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    # --activity is the former name.
    parser.add_argument("--status", "--activity", dest="status", choices=("idle", "working", "waiting", "unknown"),
                        help="idle, working, waiting or unknown")
    parser.add_argument("--usage-json", help="JSON context/cost/rate-limit usage from the harness")

    parser.add_argument("--bridge", choices=("pi-inbox-v1", "pi-socket-v1", "claude-mod-v1", "none"))

def number(value, low=0, high=None):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= low
    return value if ok and (high is None or value <= high) else None


def usage(text):
    """Keep only the known fields, with unknown values left out, never zeroed."""
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise ValueError(f"--usage-json is not JSON: {error}") from None
    if not isinstance(raw, dict):
        raise ValueError("--usage-json must be a JSON object")
    context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    limits = []
    for item in raw.get("rate_limits") if isinstance(raw.get("rate_limits"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("window"), str) and number(item.get("percent_used")) is not None:
            limits.append({"window": item["window"][:32], "percent_used": item["percent_used"],
                           **({"resets_at": item["resets_at"][:40]} if isinstance(item.get("resets_at"), str) else {})})
    known = {key: number(context.get(key), high=100 if key == "percent" else None) for key in ("tokens", "window", "percent")}
    value = {"context": {key: item for key, item in known.items() if item is not None},
             "rate_limits": limits, "reported_at": agents.now()}
    if number(raw.get("cost_usd")) is not None:
        value["cost_usd"] = raw["cost_usd"]
    return value


def run(root=None, *, agent=None, status=None, usage_json=None, bridge=None):
    if status is None and usage_json is None:
        raise ValueError("Specify --status or --usage-json")
    root, directory, launch_id = callback(root, agent)
    changes = {}
    if status is not None:
        changes["status"] = status
    if usage_json is not None:
        changes["usage"] = usage(usage_json)
    if bridge:
        changes["native_bridge"] = None if bridge == "none" else bridge
    return agents.update_runtime(directory, launch_id, **changes)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
