"""Report native harness status for the current launch.

For harness integrations (the Claude mod, the Pi extension), run as
`python -m multiverse.tools.runtime_update`; it is not an `mv2` command.
An integration also reports here that its process moved to another native
session (Claude's /clear), and core rebinds the agent to it.
"""
import argparse
import json
import os
import re

from ..store import read
from ..universe import agents
from ._common import add_agent, callback

# What an integration itself can know moved its session; core records "detected" and "seeded".
SESSION_REASONS = ("clear", "new", "resume", "fork")

BRIDGE_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
BRIDGE_ID_LENGTH = 40


def bridge_id(value):
    """The id an integration reports for itself, or `none` to clear it.

    Only the shape is checked. The id is data the integration reports: each adapter compares
    it with its own exact constant, so a well-formed id for another integration never makes
    a launch ready or deliverable.
    """
    if not isinstance(value, str) or len(value) > BRIDGE_ID_LENGTH or not BRIDGE_ID.fullmatch(value):
        raise ValueError(f"--bridge must be lowercase letters, digits and single hyphens, starting with a letter, "
                         f"at most {BRIDGE_ID_LENGTH} characters (for example claude-mod-v1), or none")
    return value


def bridge_argument(value):
    try:
        return bridge_id(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def configure(parser):
    add_agent(parser)
    # --activity is the former name.
    parser.add_argument("--status", "--activity", dest="status", choices=("idle", "working", "waiting", "unknown"),
                        help="idle, working, waiting or unknown")
    parser.add_argument("--usage-json", help="JSON context/cost/rate-limit usage from the harness")

    parser.add_argument("--bridge", type=bridge_argument, metavar="ID",
                        help="the integration's own bridge id, for example claude-mod-v1, or none")
    parser.add_argument("--native-session-id", metavar="ID", help="the native session this process is on now")
    parser.add_argument("--previous-session-id", metavar="ID", help="the native session it moved from")
    parser.add_argument("--session-reason", choices=SESSION_REASONS, help="why it moved, for example clear")

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


def run(root=None, *, agent=None, status=None, usage_json=None, bridge=None,
        native_session_id=None, previous_session_id=None, session_reason=None):
    session = (native_session_id, previous_session_id, session_reason)
    if any(session) and not all(session):
        raise ValueError("--native-session-id, --previous-session-id and --session-reason go together")
    if status is None and usage_json is None and native_session_id is None:
        raise ValueError("Specify --status, --usage-json or --native-session-id")
    root, directory, launch_id = callback(root, agent)
    if native_session_id is not None:
        return rebind(root, directory, launch_id, previous_session_id, native_session_id, session_reason)
    changes = {}
    if status is not None:
        changes["status"] = status
    if usage_json is not None:
        changes["usage"] = usage(usage_json)
    if bridge:
        changes["native_bridge"] = None if bridge_id(bridge) == "none" else bridge
    return agents.update_runtime(directory, launch_id, **changes)


def rebind(root, directory, launch_id, previous, session_id, reason):
    """Move this exact launch's agent to the session its process is on now.

    Core refuses an outdated launch, a previous session that is no longer the agent's and a session bound to
    another agent. Where the harness can observe its process's session (Claude's process file), the move must be
    to the session it shows. Where it cannot, any process holding this launch's identity can move the pointer.
    `home` is the harness's storage as this launch sees it: an integration runs in the launch's environment.
    """
    from .. import harnesses
    from ..harnesses.types import SessionHandle
    properties = read(directory / "properties.json")
    adapter = harnesses.load(root, properties.get("harness", ""))
    home = getattr(adapter, "session_home", lambda environment: None)(dict(os.environ))
    observer = getattr(adapter, "observed_binding", None)
    if callable(observer) and properties.get("workspace"):
        observed = observer(SessionHandle.from_records(properties, read(directory / "runtime.json"), directory))
        if observed is not None and observed.native_id != session_id:
            raise ValueError("Not moved: this launch's process is on another session")
    return agents.rebind_session(root, directory.name, previous, session_id, reason, launch_id, home=home)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
