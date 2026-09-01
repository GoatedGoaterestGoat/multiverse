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
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _emit(text: str, as_json: bool, ok: bool | None = None) -> int:
    """Print a tool result and return the process exit code.

    The server-side tools report failure in-band, as a string starting with
    ``Error`` — they have no other error channel. Emitting that with ``ok=True``
    made every mvctl command exit 0, so the extension's ``r.ok`` was always
    true and a hard failure surfaced as an informational notification. Infer
    ``ok`` from that convention unless the caller states it outright.
    """
    if ok is None:
        ok = not text.lstrip().startswith("Error")
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

    The staged files remain durable until ``commit-inbox``. A known injection
    failure uses ``release-inbox`` immediately; if pi exits outright, main's
    stale-claim recovery returns them instead of silently losing them.
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


def cmd_release_inbox(args) -> int:
    """Undo this deliverer's own claim after a failed hand-over.

    ``claim-inbox`` stages messages out of the pending pool; if the extension
    then fails to inject them, they are invisible until the 60s stale-claim
    sweep.  Releasing puts them back immediately, so the next delivery attempt
    (the 2s watcher poll) picks them up instead of the sender waiting a minute.
    """
    from src.inbox import release_claimed_messages

    name, cwd = _identity(args)
    if not name or not cwd:
        return _emit("0", args.json)
    return _emit(str(release_claimed_messages(cwd, name, list(args.claim))), args.json)


def cmd_claim_pi_prompts(args) -> int:
    """Stage SecondBrain prompts for injection through pi.sendUserMessage."""
    from src.pi_prompt_queue import claim_prompts

    name, cwd = _identity(args)
    prompts = claim_prompts(cwd, name) if name and cwd else []
    payload = {
        "count": len(prompts),
        "items": [
            {
                "text": str(prompt.get("text") or ""),
                "claim": Path(str(prompt["__claim_path"])).name,
            }
            for prompt in prompts
            if prompt.get("__claim_path")
        ],
    }
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def cmd_commit_pi_prompts(args) -> int:
    from src.pi_prompt_queue import commit_prompts, prompt_dir

    name, cwd = _identity(args)
    if not name or not cwd:
        return _emit("0", args.json)
    directory = prompt_dir(cwd, name)
    prompts: list[dict] = []
    for basename in args.claim:
        if (
            not basename
            or Path(basename).name != basename
            or ".json.claim-" not in basename
        ):
            continue
        path = directory / basename
        if path.is_file():
            prompts.append({"__claim_path": str(path)})
    return _emit(str(commit_prompts(prompts)), args.json)


def cmd_release_pi_prompts(args) -> int:
    from src.pi_prompt_queue import release_prompts

    name, cwd = _identity(args)
    if not name or not cwd:
        return _emit("0", args.json)
    return _emit(str(release_prompts(cwd, name, list(args.claim))), args.json)


def cmd_record_pi_prompt_consumer(args) -> int:
    from src.pi_prompt_queue import record_prompt_consumer_capability

    raw = os.environ.get("MULTIVERSE_STATUS")
    if raw:
        record_prompt_consumer_capability(Path(raw).parent)
    return _emit("", args.json)


def cmd_clear_pi_prompt_consumer(args) -> int:
    from src.pi_prompt_queue import clear_prompt_consumer_capability

    raw = os.environ.get("MULTIVERSE_STATUS")
    if raw:
        clear_prompt_consumer_capability(Path(raw).parent)
    return _emit("", args.json)


def cmd_secondbrain_user_prompt(args) -> int:
    from src.sb_notify import record_user_prompt_signal

    name, cwd = _identity(args)
    if not name or not cwd:
        return _emit("", args.json)
    text = base64.b64decode(args.text_base64.encode(), validate=True).decode("utf-8")
    record_user_prompt_signal(cwd, name, text)
    return _emit("", args.json)


def cmd_secondbrain_response(args) -> int:
    from src.sb_notify import record_response_signal

    name, cwd = _identity(args)
    if name and cwd:
        record_response_signal(cwd, name)
    return _emit("", args.json)


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
    # Compatibility for pi processes that loaded the branch's older extension
    # before an in-place checkout update. Delivery is always active now.
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

    for command, fn in (
        ("commit-inbox", cmd_commit_inbox),
        ("release-inbox", cmd_release_inbox),
        ("commit-pi-prompts", cmd_commit_pi_prompts),
        ("release-pi-prompts", cmd_release_pi_prompts),
    ):
        p = sub.add_parser(command)
        p.set_defaults(fn=fn)
        p.add_argument("--claim", action="append", default=[])
        p.add_argument("--name", default="")
        p.add_argument("--cwd", default="")

    p = sub.add_parser("claim-pi-prompts")
    p.set_defaults(fn=cmd_claim_pi_prompts)
    p.add_argument("--name", default="")
    p.add_argument("--cwd", default="")

    p = sub.add_parser("record-pi-prompt-consumer")
    p.set_defaults(fn=cmd_record_pi_prompt_consumer)

    p = sub.add_parser("clear-pi-prompt-consumer")
    p.set_defaults(fn=cmd_clear_pi_prompt_consumer)

    p = sub.add_parser("secondbrain-user-prompt")
    p.set_defaults(fn=cmd_secondbrain_user_prompt)
    p.add_argument("--text-base64", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--cwd", default="")

    p = sub.add_parser("secondbrain-response")
    p.set_defaults(fn=cmd_secondbrain_response)
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
