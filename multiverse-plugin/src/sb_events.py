"""Perception-event producers: translate the Multiverse file substrate into §4b events.

The connector's watcher (``SBConnector.run_producer``) calls these to turn signals under
``~/.multiverse`` into SecondBrain perception events. Each producer is a pure function of
the substrate — no side effects — so it's easy to unit-test; the connector owns the seq
assignment, durable queueing, and the "only queue for a workspace SB already knows"
gating.

Phase-2 step 1 implements ``agent.spawn_request``. Its payload carries a connector-produced
truncated ``summary`` (what the SB agent sees by default — like the user's naming widget)
plus the ``full`` prompt held back for a view-full tool. Exact field names are being
confirmed with the SecondBrain design agent (DESIGN §4b).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import spawn_requests as sr

#: How much of the spawn instruction to inline in the default-visible summary.
SPAWN_PROMPT_TRUNC = 240


def scan_spawn_requests(granted_cwds: set[str]) -> list[dict]:
    """Every spawn-request file in a granted workspace, regardless of resolution.

    We look at all ``<id>.json`` requests (not just *pending* ones) so we capture the
    **creation** event even for a request that's since been resolved. Skips resolution
    files (``*.resolved.json``), temp files (``*.tmp``), unparseable files, and requests
    for workspaces this device doesn't expose. ``granted_cwds`` must be resolved abs paths.
    """
    d = sr.SPAWN_REQUESTS_DIR
    if not d.is_dir():
        return []
    out: list[dict] = []
    for path in d.iterdir():
        name = path.name
        if not name.endswith(".json") or name.endswith(".resolved.json") or name.endswith(".tmp"):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        cwd = data.get("cwd")
        if not cwd:
            continue
        # Skip already-resolved requests: the decision is done (a future
        # user.spawn_decision covers it), so don't notify "spawn requested".
        if sr.resolution_path(data.get("id", "")).is_file():
            continue
        try:
            cwd_abs = str(Path(cwd).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
        if cwd_abs in granted_cwds:
            out.append(data)
    return out


def spawn_request_event_fields(req: dict) -> tuple[str, dict]:
    """Build ``(requesting_agent, payload)`` for an ``agent.spawn_request`` event.

    From the spawn-request file: ``from`` = the requesting agent (-> envelope ``agent``),
    ``default_name`` = the suggested name for the new agent, ``instruction`` = the full
    prompt, ``id`` = the request id. The payload (contract locked w/ SecondBrain, §4b):

      - ``summary``: connector-produced — "agent 'X' requests to spawn a new agent 'Y'
        with prompt: <truncated>". SB delivers this ~verbatim as the turn the agent sees
        (workspace-tagged), mirroring what the user sees in the naming widget.
      - ``full``: the complete instruction, inline; SB's host caches it by ``event_id``
        and surfaces it only if the agent calls the view_full tool.
      - ``new_agent_name`` + ``request_id``: structured fields SB retains on the
        pending-turn record so a future ``answer_spawn_request`` action has its handle.
    """
    requesting = req.get("from") or "?"
    new_name = req.get("default_name") or "?"
    full = req.get("instruction") or ""
    truncated = full[:SPAWN_PROMPT_TRUNC].rstrip()
    if len(full) > SPAWN_PROMPT_TRUNC:
        truncated += "…"
    summary = (
        f"agent '{requesting}' requests to spawn a new agent '{new_name}' "
        f"with prompt: {truncated}"
    )
    payload = {
        "new_agent_name": new_name,
        "summary": summary,
        "full": full,
        "request_id": req.get("id") or "",
    }
    return requesting, payload
