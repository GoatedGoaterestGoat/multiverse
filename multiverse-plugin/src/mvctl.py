"""CLI bridge used by the Multiverse pi extension.

pi extensions run in TypeScript, while Multiverse's session registry, inbox,
spawn broker, and side-question implementations live in Python.  This module
keeps the extension thin by exposing those shared operations as subprocess
commands.  It deliberately contains no pi compaction implementation: pi uses
its native compaction, just as Codex does.
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
        sys.stdout.write(text + ("\n" if text and not text.endswith("\n") else ""))
    return 0 if ok else 1


def _identity(args) -> tuple[str, str]:
    name = getattr(args, "name", "") or os.environ.get("MULTIVERSE_SESSION_NAME", "")
    cwd = getattr(args, "cwd", "") or os.environ.get("MULTIVERSE_SESSION_CWD", "")
    return name, cwd


# ---------------------------------------------------------------------------
# Agent-facing commands
# ---------------------------------------------------------------------------


def cmd_list_agents(args) -> int:
    from src.server import list_agents

    return _emit(
        list_agents(max_agents=args.max, working_directory=args.cwd or ""),
        args.json,
    )


def cmd_message(args) -> int:
    from src.server import message_agent

    return _emit(
        message_agent(
            target_name=args.to,
            message=args.body if args.body is not None else sys.stdin.read(),
            working_directory=args.cwd or "",
        ),
        args.json,
    )


def cmd_check_inbox(args) -> int:
    from src.server import check_inbox

    return _emit(check_inbox(), args.json)


def cmd_monitor_inbox(args) -> int:
    from src.server import monitor_inbox

    return _emit(
        asyncio.run(monitor_inbox(timeout_seconds=args.timeout)),
        args.json,
    )


def cmd_ask(args) -> int:
    from src.server import ask_agent

    return _emit(
        ask_agent(
            target_name=args.to,
            question=args.question if args.question is not None else sys.stdin.read(),
            timeout_seconds=args.timeout,
            working_directory=args.cwd or "",
        ),
        args.json,
    )


def cmd_spawn(args) -> int:
    from src.server import spawn_agent

    text = asyncio.run(
        spawn_agent(
            instruction=(
                args.instruction if args.instruction is not None else sys.stdin.read()
            ),
            name=args.name or "",
        )
    )
    return _emit(text, args.json)


# ---------------------------------------------------------------------------
# Extension lifecycle and direct delivery
# ---------------------------------------------------------------------------


def cmd_inbox_count(args) -> int:
    from src.inbox import pending_inbox_count

    name, cwd = _identity(args)
    return _emit(str(pending_inbox_count(cwd, name) if name and cwd else 0), args.json)


def cmd_claim_inbox(args) -> int:
    """Stage pending messages and return a delivery payload.

    The staged files remain durable until ``commit-inbox``.  If pi exits or
    ``sendMessage`` throws before commit, main's stale-claim recovery puts the
    messages back into the pending pool instead of silently losing them.
    """
    from src.inbox import (
        claim_pending_messages,
        clear_wake_sent,
        render_messages_as_tool_result,
    )

    name, cwd = _identity(args)
    messages = claim_pending_messages(cwd, name) if name and cwd else []
    if messages:
        clear_wake_sent(cwd, name)
    payload = {
        "count": len(messages),
        "claims": [
            Path(str(m["__claim_path"])).name
            for m in messages
            if m.get("__claim_path")
        ],
        "text": render_messages_as_tool_result(messages) if messages else "",
    }
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def cmd_commit_inbox(args) -> int:
    """Commit only staged files created for this agent's inbox."""
    from src.inbox import agent_inbox_dir, commit_claimed_messages
    from src.interagent_log import log_inbox_delivered

    name, cwd = _identity(args)
    if not name or not cwd:
        return _emit("0", args.json)

    inbox_dir = agent_inbox_dir(cwd, name)
    messages: list[dict] = []
    for basename in args.claim:
        # Accept basenames only and only our staged-message shape.  Never let a
        # TypeScript/argv bug turn this local bridge into an arbitrary unlink.
        if (
            not basename
            or Path(basename).name != basename
            or ".json.claim-" not in basename
        ):
            continue
        path = inbox_dir / basename
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["__claim_path"] = str(path)
        messages.append(data)

    if messages:
        try:
            log_inbox_delivered(
                recipient=name,
                cwd=cwd,
                hook_event="pi_extension_delivery",
                messages=messages,
                recipient_runtime="pi",
            )
        except Exception:  # logging can never block delivery acknowledgement
            pass
        commit_claimed_messages(messages)
    return _emit(str(len(messages)), args.json)


def cmd_status(args) -> int:
    from src.sessions import write_status_path

    raw = os.environ.get("MULTIVERSE_STATUS")
    if raw:
        write_status_path(Path(raw), args.status)
    return _emit("", args.json)


def cmd_record_session(args) -> int:
    from src.atomic_files import atomic_write_text

    raw = os.environ.get("MULTIVERSE_RUNTIME_SESSION_ID_PATH")
    if not raw:
        status = os.environ.get("MULTIVERSE_STATUS")
        if status:
            raw = str(Path(status).parent / "runtime_session_id")
    if raw:
        atomic_write_text(Path(raw), args.session_id.strip() + "\n")
    return _emit("", args.json)


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mvctl", description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help='wrap text as {"ok": bool, "text": str}'
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list-agents")
    p.set_defaults(fn=cmd_list_agents)
    p.add_argument("--max", type=int, default=15)
    p.add_argument("--cwd", default="")

    p = sub.add_parser("message")
    p.set_defaults(fn=cmd_message)
    p.add_argument("--to", required=True)
    p.add_argument("--body", default=None)
    p.add_argument("--cwd", default="")
    # Compatibility with pi sessions running an older loaded extension while
    # the checkout is updated in place. Delivery is always active on main.
    p.add_argument("--push", action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("check-inbox")
    p.set_defaults(fn=cmd_check_inbox)

    p = sub.add_parser("monitor-inbox")
    p.set_defaults(fn=cmd_monitor_inbox)
    p.add_argument("--timeout", type=int, default=300)

    p = sub.add_parser("ask")
    p.set_defaults(fn=cmd_ask)
    p.add_argument("--to", required=True)
    p.add_argument("--question", default=None)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--cwd", default="")

    p = sub.add_parser("spawn")
    p.set_defaults(fn=cmd_spawn)
    p.add_argument("--instruction", default=None)
    p.add_argument("--name", default="")

    for command, fn in (("inbox-count", cmd_inbox_count), ("claim-inbox", cmd_claim_inbox)):
        p = sub.add_parser(command)
        p.set_defaults(fn=fn)
        p.add_argument("--name", default="")
        p.add_argument("--cwd", default="")

    p = sub.add_parser("commit-inbox")
    p.set_defaults(fn=cmd_commit_inbox)
    p.add_argument("--claim", action="append", default=[])
    p.add_argument("--name", default="")
    p.add_argument("--cwd", default="")

    p = sub.add_parser("status")
    p.set_defaults(fn=cmd_status)
    p.add_argument("status")

    p = sub.add_parser("record-session")
    p.set_defaults(fn=cmd_record_session)
    p.add_argument("session_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except Exception as exc:  # caller converts stderr/non-zero into a tool result
        print(f"mvctl {args.cmd}: {exc}", file=sys.stderr)
        if args.json:
            json.dump({"ok": False, "text": str(exc)}, sys.stdout)
            sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
