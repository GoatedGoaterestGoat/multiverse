"""``mvctl`` — the harness-neutral bridge to multiverse's state layer.

Claude Code reaches multiverse through an MCP server and hook scripts, both
of which are Python and can import ``src/`` directly. pi has no MCP; its
integration is a TypeScript extension (``pi-extension/``) running inside the
pi process. Rather than reimplement the inbox protocol, spawn brokering, and
fork machinery in TypeScript — two implementations of the same on-disk
contract, free to drift — the extension shells out to this CLI.

Every subcommand is a thin adapter over the exact functions the MCP server
exposes, so the two harnesses share one implementation of every behavior
(push-notification routing, monitor-brokered spawn approval, inbox claiming,
side-question forks). What differs between harnesses lives in
``src/harness/``, not here.

Output contract: agent-facing subcommands print the tool result on stdout and
exit 0. ``--json`` wraps that as ``{"ok": bool, "text": str}`` for callers
that need to distinguish a failure from a tool result that merely reads like
one. Errors go to stderr with a non-zero exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _emit(text: str, as_json: bool, ok: bool = True) -> int:
    if as_json:
        json.dump({"ok": ok, "text": text}, sys.stdout)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Inter-agent subcommands — straight passthrough to the shared tool bodies
# ---------------------------------------------------------------------------

def cmd_list_agents(args) -> int:
    from src.server import list_agents
    return _emit(list_agents(max_agents=args.max, working_directory=args.cwd or ""), args.json)


def cmd_message(args) -> int:
    from src.server import message_agent
    return _emit(
        message_agent(
            target_name=args.to,
            message=args.body if args.body is not None else sys.stdin.read(),
            push_notification=args.push,
        ),
        args.json,
    )


def cmd_check_inbox(args) -> int:
    from src.server import check_inbox
    return _emit(check_inbox(), args.json)


def cmd_monitor_inbox(args) -> int:
    from src.server import monitor_inbox
    return _emit(asyncio.run(monitor_inbox(timeout_seconds=args.timeout)), args.json)


def cmd_ask(args) -> int:
    from src.server import ask_agent
    return _emit(
        ask_agent(
            target_name=args.to,
            question=args.question if args.question is not None else sys.stdin.read(),
            timeout_seconds=args.timeout,
        ),
        args.json,
    )


def cmd_spawn(args) -> int:
    from src.server import spawn_agent
    return _emit(
        spawn_agent(
            instruction=args.instruction if args.instruction is not None else sys.stdin.read(),
            name=args.name or "",
        ),
        args.json,
    )


def cmd_inbox_count(args) -> int:
    """Pending-message count without consuming anything.

    The pi extension polls this to drive its footer badge, which is why it
    must not claim: claiming here would deliver a message into a status line
    and drop it from the conversation.
    """
    from src.inbox import pending_inbox_count
    name = args.name or os.environ.get("MULTIVERSE_SESSION_NAME") or ""
    cwd = args.cwd or os.environ.get("MULTIVERSE_SESSION_CWD") or ""
    if not name or not cwd:
        return _emit("0", args.json)
    return _emit(str(pending_inbox_count(cwd, name)), args.json)


def cmd_peek_inbox(args) -> int:
    """Render pending messages WITHOUT consuming them, plus their ids.

    The pi extension's delivery is peek -> send -> ack (see
    ``inbox.ack_messages`` for why), so this is the read half. The Claude path
    never uses it; its hook claims and delivers atomically.
    """
    from src.inbox import list_pending_messages, render_messages_as_tool_result
    name = args.name or os.environ.get("MULTIVERSE_SESSION_NAME") or ""
    cwd = args.cwd or os.environ.get("MULTIVERSE_SESSION_CWD") or ""
    if not name or not cwd:
        json.dump({"count": 0, "ids": [], "text": ""}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    messages = list_pending_messages(cwd, name)
    ids = [m.get("message_id", "") for m in messages if m.get("message_id")]
    text = render_messages_as_tool_result(messages) if messages else ""
    json.dump({"count": len(messages), "ids": ids, "text": text}, sys.stdout,
              ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def cmd_ack_inbox(args) -> int:
    """Consume messages by id after a successful delivery."""
    from src.inbox import ack_messages
    from src.interagent_log import log_inbox_delivered
    name = args.name or os.environ.get("MULTIVERSE_SESSION_NAME") or ""
    cwd = args.cwd or os.environ.get("MULTIVERSE_SESSION_CWD") or ""
    ids = [i for i in (args.ids or "").split(",") if i]
    if not (name and cwd and ids):
        return _emit("0", args.json)
    n = ack_messages(cwd, name, ids)
    if n:
        try:
            log_inbox_delivered(
                recipient=name,
                cwd=cwd,
                hook_event="pi_extension_delivery",
                messages=[{"message_id": i} for i in ids[:n]],
            )
        except Exception:  # noqa: BLE001 — logging must never fail a delivery
            pass
    return _emit(str(n), args.json)


def cmd_status(args) -> int:
    """Write this session's status.json — the pi analogue of set-status.sh."""
    import time
    path = os.environ.get("MULTIVERSE_STATUS")
    if not path:
        return _emit("", args.json)
    try:
        with open(path, "w") as fh:
            json.dump({"status": args.status, "ts": int(time.time())}, fh)
    except OSError as e:
        print(f"mvctl status: {e}", file=sys.stderr)
        return 1
    return _emit("", args.json)


def cmd_record_session(args) -> int:
    """Record the harness session id the monitor should resume.

    Claude Code has a SessionStart hook for this (``record-claude-uuid.sh``);
    on pi the extension calls it from its ``session_start`` handler. The file
    name stays ``claude_uuid`` so a mixed dashboard reads one field.
    """
    state = os.environ.get("MULTIVERSE_STATUS")
    if not state:
        return _emit("", args.json)
    target = Path(state).parent / "claude_uuid"
    try:
        target.write_text(args.session_id.strip() + "\n")
    except OSError as e:
        print(f"mvctl record-session: {e}", file=sys.stderr)
        return 1
    return _emit("", args.json)


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

def cmd_compact_plan(args) -> int:
    """Plan a goat compaction of a pi session and print it as JSON.

    Called from the extension's ``session_before_compact`` handler, whose
    return value this maps onto directly (``summary`` / ``firstKeptEntryId``
    / ``tokensBefore`` / ``details``). Trees are read from the same cache the
    Claude path uses; turns without a cached tree fall back to the greedy
    leaf-prefix expansion, so compaction never blocks on construction.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))
    from compaction.pi_parser import parse_pi_session
    from compaction.pi_render import compact_forest_to_pi_entry
    from src.tree_cache import attach_cached_trees

    forest = parse_pi_session(args.session)
    n_cached = attach_cached_trees(forest, args.session_id) if args.session_id else 0
    payload = compact_forest_to_pi_entry(
        forest, args.budget, image_mode=args.image_mode
    )
    payload["_multiverse"] = {
        "turns": len(forest),
        "turnsWithTree": n_cached,
        "budget": args.budget,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def cmd_compact_apply(args) -> int:
    """Compact a pi session in place, whole-session, to a token budget.

    The monitor's ``c`` action drives the Claude-Code-specific pipeline
    (two-JSONL mirror, tree-build worker, atomic live replace). pi needs none
    of that: compaction is one appended ``compaction`` entry, and the entries
    it replaces stay in the file. So the equivalent of a manual whole-session
    compaction is this command — the write is a single appended line, which is
    also why there is no lock and no rollback path to get wrong.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))
    from compaction.pi_parser import parse_pi_session
    from compaction.pi_render import compact_forest_to_pi_jsonl
    from src.tree_cache import attach_cached_trees

    session = Path(args.session)
    forest = parse_pi_session(str(session))
    if not forest:
        print(f"mvctl compact-apply: no turns found in {session}", file=sys.stderr)
        return 1
    if args.session_id:
        attach_cached_trees(forest, args.session_id)

    text = compact_forest_to_pi_jsonl(
        forest,
        args.budget,
        session_id=args.session_id or session.stem,
        cwd=str(session.parent),
        source_path=str(session),
        image_mode=args.image_mode,
    )
    if args.dry_run:
        sys.stdout.write(text.rsplit("\n", 2)[-2] + "\n")
        return 0

    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".mv-compact-", suffix=".jsonl",
                               dir=str(session.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, session)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return _emit(f"compacted {session} to a {args.budget:,}-token budget", args.json)


def cmd_compact_stats(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))
    from compaction.pi_parser import parse_pi_session

    forest = parse_pi_session(args.session)
    total = 0
    for turn in forest:
        total += sum(l.tokens for l in turn.leaves)
        if turn.user_prompt is not None:
            total += turn.user_prompt.tokens
    json.dump({"turns": len(forest), "tokens": total}, sys.stdout)
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mvctl", description=__doc__)
    p.add_argument("--json", action="store_true",
                   help="wrap the result as {\"ok\":bool,\"text\":str}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list-agents"); s.set_defaults(fn=cmd_list_agents)
    s.add_argument("--max", type=int, default=15)
    s.add_argument("--cwd", default="")

    s = sub.add_parser("message"); s.set_defaults(fn=cmd_message)
    s.add_argument("--to", required=True)
    s.add_argument("--body", default=None, help="message text; omit to read stdin")
    s.add_argument("--push", action="store_true")

    s = sub.add_parser("check-inbox"); s.set_defaults(fn=cmd_check_inbox)

    s = sub.add_parser("monitor-inbox"); s.set_defaults(fn=cmd_monitor_inbox)
    s.add_argument("--timeout", type=int, default=300)

    s = sub.add_parser("ask"); s.set_defaults(fn=cmd_ask)
    s.add_argument("--to", required=True)
    s.add_argument("--question", default=None)
    s.add_argument("--timeout", type=int, default=60)

    s = sub.add_parser("spawn"); s.set_defaults(fn=cmd_spawn)
    s.add_argument("--instruction", default=None)
    s.add_argument("--name", default="")

    s = sub.add_parser("inbox-count"); s.set_defaults(fn=cmd_inbox_count)
    s.add_argument("--name", default="")
    s.add_argument("--cwd", default="")

    s = sub.add_parser("peek-inbox"); s.set_defaults(fn=cmd_peek_inbox)
    s.add_argument("--name", default="")
    s.add_argument("--cwd", default="")

    s = sub.add_parser("ack-inbox"); s.set_defaults(fn=cmd_ack_inbox)
    s.add_argument("--ids", default="", help="comma-separated message ids")
    s.add_argument("--name", default="")
    s.add_argument("--cwd", default="")

    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s.add_argument("status")

    s = sub.add_parser("record-session"); s.set_defaults(fn=cmd_record_session)
    s.add_argument("session_id")

    s = sub.add_parser("compact-plan"); s.set_defaults(fn=cmd_compact_plan)
    s.add_argument("--session", required=True, help="path to the pi session JSONL")
    s.add_argument("--session-id", default="", help="id used to find cached trees")
    s.add_argument("--budget", type=int, required=True)
    s.add_argument("--image-mode", default="budget_fit",
                   choices=["budget_fit", "keep_all", "drop_all"])

    s = sub.add_parser("compact-apply"); s.set_defaults(fn=cmd_compact_apply)
    s.add_argument("--session", required=True, help="path to the pi session JSONL")
    s.add_argument("--session-id", default="", help="id used to find cached trees")
    s.add_argument("--budget", type=int, required=True)
    s.add_argument("--image-mode", default="budget_fit",
                   choices=["budget_fit", "keep_all", "drop_all"])
    s.add_argument("--dry-run", action="store_true",
                   help="print the compaction entry instead of writing it")

    s = sub.add_parser("compact-stats"); s.set_defaults(fn=cmd_compact_stats)
    s.add_argument("--session", required=True)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except Exception as e:  # noqa: BLE001 — the caller is a tool result
        print(f"mvctl {args.cmd}: {e}", file=sys.stderr)
        if args.json:
            json.dump({"ok": False, "text": str(e)}, sys.stdout)
            sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
