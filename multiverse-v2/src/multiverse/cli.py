import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from . import agents, universe
from .store import ack, append_message, inbox, read


def emit(value):
    print(value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False))


def tui_start(root, backend=None):
    try:
        from .tui import start
    except ModuleNotFoundError as error:
        raise RuntimeError("Install the TUI dependencies with `uv sync --project multiverse-v2` or `pip install ./multiverse-v2`.") from error
    start(root, backend)


def attach(root, reference):
    backend, pane = agents.terminal_action(root, reference, "attach")
    # switch-client only works inside this same tmux server; nested attachment
    # is possible for other servers after clearing the nesting sentinel.
    current = os.environ.get("TMUX", "").split(",")[0]
    if current == backend.prefix[2]:
        backend.run("switch-client", "-t", pane)
    else:
        env = dict(os.environ)
        env.pop("TMUX", None)
        subprocess.run([*backend.prefix, "attach-session", "-t", pane], env=env, check=True)


def parser():
    p = argparse.ArgumentParser(description="Local prompt-led universes. No daemon or MCP required.")
    p.add_argument("-u", "--universe", help="Universe directory or workspace (otherwise env/current directory)")
    commands = p.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="Clean template install into DIRECTORY/.multiverse")
    init.add_argument("directory", nargs="?", default=".")
    init.add_argument("--name")
    init.add_argument("--template", default="base")
    child = commands.add_parser("subverse", help="Create a nested, independent universe")
    child.add_argument("name")
    child.add_argument("--template", default="base")
    child.add_argument("--workspace")
    ls = commands.add_parser("list", help="Read properties and observe matching tmux panes")
    ls.add_argument("--recursive", action="store_true")
    ls.add_argument("--json", action="store_true")
    commands.add_parser("catalog", help="List local capabilities without loading their prompts")
    cmd = commands.add_parser("command", help="Print an exact matching prompt; never execute it")
    cmd.add_argument("name")
    cmd.add_argument("arguments", nargs=argparse.REMAINDER)
    spawn = commands.add_parser("spawn", help="Launch an agent in a tagged tmux pane")
    spawn.add_argument("--name", required=True)
    spawn.add_argument("--harness", default="codex", help="Configured harness name (default: codex)")
    task = spawn.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="Initial prompt sent to the new agent")
    task.add_argument("--task-file", type=Path)
    spawn.add_argument("--model")
    resume = commands.add_parser("resume")
    resume.add_argument("agent")
    resume.add_argument("--model")
    for action in ("stop", "attach", "inspect"):
        commands.add_parser(action).add_argument("agent")
    send = commands.add_parser("send", help="Append a durable message; delivery is adapter-specific")
    send.add_argument("agent")
    message = send.add_mutually_exclusive_group(required=True)
    message.add_argument("--text")
    message.add_argument("--file", type=Path)
    message.add_argument("--stdin", action="store_true")
    send.add_argument("--reply-to")
    for action in ("inbox", "ack", "status", "runtime-update", "session-bind"):
        cmd = commands.add_parser(action)
        cmd.add_argument("--agent", default=os.environ.get("MV2_AGENT_ID"))
        if action == "ack":
            cmd.add_argument("--cursor", type=int, required=True)
        elif action == "status":
            cmd.add_argument("--phase", choices=("starting", "working", "blocked", "done"))
            cmd.add_argument("--summary")
        elif action == "runtime-update":
            cmd.add_argument("--activity", choices=("idle", "working", "unknown"), required=True)
        elif action == "session-bind":
            cmd.add_argument("--session-id", required=True, help="Native harness session UUID for exact resume")
    tui = commands.add_parser("tui", help="Browse universes and operate agents")
    tui.add_argument("--demo", action="store_true", help="Explore an in-memory demo without starting processes")
    commands.add_parser("doctor", help="Check local definitions and report uncertain agent state")
    return p


def run(args):
    if args.action == "tui" and args.demo:
        from .demo import DemoBackend
        backend = DemoBackend()
        tui_start(backend.root, backend)
        return None
    if args.action == "init":
        workspace = Path(args.directory).resolve()
        root = universe.create(workspace / ".multiverse", args.name or workspace.name, args.template, workspace)
        return {"universe": str(root), **read(root / "universe.json")}
    root = universe.resolve(args.universe)
    action = args.action
    if action == "subverse":
        universe.slug(args.name)
        child = universe.create(root / "subverses" / args.name, args.name, args.template,
                                args.workspace or universe.workspace(root))
        return {"universe": str(child), **read(child / "universe.json")}
    if action == "list":
        rows = agents.listing(root, args.recursive)
        if args.json:
            return rows
        return "\n".join(f'{r["id"][:8]}  {r["name"]:16} {r["live"]:8} {r["activity"]:8} {r.get("phase", "?"):9} {r.get("summary", "")} [{r["universe"]}]' for r in rows) or "No agents."
    if action == "catalog":
        return "\n".join(f'{e["kind"]}/{e["name"]}: {e["description"]}\n  {e["path"]}' for e in universe.catalog(root))
    if action == "command":
        return universe.command(root, args.name, " ".join(args.arguments))
    if action == "spawn":
        task = args.task_file.read_text() if args.task_file else args.task
        return agents.start(root, args.name, task, args.harness, args.model)
    if action == "resume":
        return agents.start(root, resume=args.agent, model=args.model)
    if action == "attach":
        attach(root, args.agent)
        return None
    if action in ("stop", "inspect"):
        return agents.terminal_action(root, args.agent, action)
    if action == "send":
        text = args.file.read_text() if args.file else sys.stdin.read() if args.stdin else args.text
        sender = {"agent_id": os.environ.get("MV2_AGENT_ID"), "universe": os.environ.get("MV2_UNIVERSE"),
                  "name": "agent" if os.environ.get("MV2_AGENT_ID") else "user"}
        result = append_message(agents.directory(root, args.agent), text, sender, args.reply_to)
        return {**result, "state": "queued"}
    if action in ("inbox", "ack", "status", "runtime-update", "session-bind"):
        if not args.agent:
            raise ValueError("Specify --agent outside an agent session")
        agent_dir = agents.directory(root, args.agent)
        # Old adapter callbacks must not consume inbox messages for a newer launch.
        launch_id = os.environ.get("MV2_LAUNCH_ID")
        if launch_id and agents.runtime(agent_dir).get("launch_id") != launch_id:
            raise ValueError("Outdated launch")
        if action == "inbox":
            return inbox(agent_dir)
        if action == "ack":
            return ack(agent_dir, args.cursor)
        if action == "status":
            return agents.status(root, args.agent, args.phase, args.summary)
        if action == "session-bind":
            return agents.bind_session(root, args.agent, args.session_id, launch_id)
        return agents.update_runtime(agent_dir, launch_id, activity=args.activity)
    if action == "tui":
        tui_start(root)
        return None
    if action == "doctor":
        problems = []
        for scope in universe.tree(root):
            try:
                if not universe.workspace(scope).is_dir():
                    problems.append(f"Missing workspace: {scope}")
                universe.catalog(scope)
                for item in agents.listing(scope):
                    if item["live"] == "unknown" or item.get("runtime", {}).get("state") == "failed":
                        problems.append(f'{item["name"]}: {item.get("reason") or item.get("runtime", {}).get("error", "Launch failed")}')
                    inbox(scope / "state" / "agents" / item["id"])
            except (OSError, ValueError, KeyError) as error:
                problems.append(f"{scope}: {error}")
        return {"ok": not problems, "problems": problems, "scope": str(root)}


def main():
    args = parser().parse_args()
    try:
        result = run(args)
        if result is not None:
            emit(result)
        if args.action == "doctor" and not result["ok"]:
            sys.exit(1)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"mv2: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
