#!/usr/bin/env python3
"""List — and optionally reap — Claude-Code-native background forks.

Claude Code runs a session as a *background agent* by forking it:

    claude bg-pty-host --bg-pty-host /tmp/cc-daemon-*/…/pty/<short>.sock …
      -- <claude> --session-id <NEW> --fork-session --resume <PARENT>.jsonl

It does this on its own initiative — most commonly when a
``run_in_background`` Bash task finishes while the session is detached, so the
model has to be re-invoked out-of-band to see the notification. The fork
copies the parent's transcript verbatim, custom-title included, and inherits
the parent's ``MULTIVERSE_*`` env. Two consequences for multiverse:

  * its transcript surfaces as a second dormant agent with the parent's name
  * its SessionStart hook can swing the parent's ``claude_uuid`` pointer

Both are handled in-process now (``sessions.is_native_bg_fork`` hides the row,
``record-claude-uuid.sh`` refuses the pointer write), so this script is for
inspection and manual cleanup rather than something that needs to run.

    bg-forks.py                 # list live fork processes + fork transcripts
    bg-forks.py --kill          # SIGTERM the fork processes (see WARNING)
    bg-forks.py --kill --purge  # …and delete their transcripts + mirrors

WARNING on --kill: a background fork is usually doing *real work* — it is the
continuation of the agent, not a throwaway. In one observed case the fork was
the only thing that finished the task (it wrote the completion summary; the
interactive session had gone idle 35 minutes earlier). Killing it discards
whatever it has not yet written. Prefer letting it finish; the row is hidden
either way. --purge additionally deletes transcripts, and it refuses any fork
whose rows are not fully contained in its parent, so it can never drop work
that exists nowhere else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from src.paths import claude_projects_dir, multiverse_dir  # noqa: E402


def _chain_uuids(path: Path) -> set[str]:
    out: set[str] = set()
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    u = json.loads(line).get("uuid")
                except json.JSONDecodeError:
                    continue
                if u:
                    out.add(u)
    except OSError:
        pass
    return out


def _meta(path: Path) -> dict:
    """last custom-title + last sessionKind, cheaply."""
    title = None
    last_kind = None
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("type") == "custom-title" and r.get("customTitle"):
                    title = r["customTitle"]
                if r.get("sessionKind"):
                    last_kind = r["sessionKind"]
    except OSError:
        pass
    return {"title": title, "bg": last_kind == "bg"}


def live_fork_procs() -> list[dict]:
    """Fork processes currently running, parsed from the process table."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid,etime,args"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for line in out.splitlines()[1:]:
        if "--fork-session" not in line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etime, args = parts
        toks = args.split()
        sid = parent = None
        for i, t in enumerate(toks):
            if t == "--session-id" and i + 1 < len(toks):
                sid = toks[i + 1]
            elif t == "--resume" and i + 1 < len(toks):
                parent = re.sub(r"\.jsonl$", "", os.path.basename(toks[i + 1]))
        procs.append({"pid": int(pid), "ppid": int(ppid), "etime": etime,
                      "session_id": sid, "parent": parent})
    return procs


def fork_transcripts(cwd: str | None) -> list[dict]:
    """Transcripts on disk whose last sessionKind is 'bg'."""
    proj = claude_projects_dir()
    dirs = []
    if cwd:
        dirs = [proj / re.sub(r"[^a-zA-Z0-9]", "-", cwd)]
    elif proj.is_dir():
        dirs = [d for d in proj.iterdir() if d.is_dir()]
    found = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.jsonl")):
            m = _meta(p)
            if not m["bg"]:
                continue
            found.append({
                "path": p, "uuid": p.stem, "title": m["title"],
                "size": p.stat().st_size, "project": d.name,
            })
    return found


def _parent_candidates(uuid: str, project: str) -> list[Path]:
    """Sibling transcripts that could be this fork's parent."""
    d = claude_projects_dir() / project
    return [p for p in d.glob("*.jsonl") if p.stem != uuid]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", help="limit transcript scan to one workspace")
    ap.add_argument("--kill", action="store_true", help="SIGTERM fork processes")
    ap.add_argument("--purge", action="store_true",
                    help="with --kill: also delete fully-contained fork transcripts")
    args = ap.parse_args()

    procs = live_fork_procs()
    print(f"=== live fork processes: {len(procs)} ===")
    for p in procs:
        print(f"  pid={p['pid']:<8} age={p['etime']:<12} "
              f"session={(p['session_id'] or '?')[:8]} forked-from={(p['parent'] or '?')[:8]}")
    if not procs:
        print("  none")

    tx = fork_transcripts(args.cwd)
    print(f"\n=== fork transcripts on disk: {len(tx)} ===")
    for t in tx:
        print(f"  {t['uuid'][:8]}  {t['size']:>12,} B  title={t['title']!r}")
    if not tx:
        print("  none")

    if not args.kill:
        print("\n(no changes made; pass --kill to reap — read the WARNING in --help first)")
        return 0

    print(f"\n=== killing {len(procs)} fork process(es) ===")
    for p in procs:
        try:
            os.kill(p["pid"], signal.SIGTERM)
            print(f"  SIGTERM -> {p['pid']}")
        except (OSError, ProcessLookupError) as e:
            print(f"  {p['pid']}: {e}")

    if not args.purge:
        return 0

    print("\n=== purging fully-contained fork transcripts ===")
    mvr = multiverse_dir()
    for t in tx:
        own = _chain_uuids(t["path"])
        covered = False
        for cand in _parent_candidates(t["uuid"], t["project"]):
            if own and not (own - _chain_uuids(cand)):
                covered = True
                print(f"  {t['uuid'][:8]}: contained in {cand.stem[:8]} -> safe")
                break
        if not covered:
            print(f"  {t['uuid'][:8]}: NOT contained in any sibling -> KEEPING "
                  f"(holds {len(own)} rows that may exist nowhere else)")
            continue
        for p in [t["path"],
                  mvr / "full_jsonls" / f"{t['uuid']}.jsonl",
                  mvr / "full_jsonls" / f"{t['uuid']}.marker"]:
            if p.exists():
                p.unlink()
                print(f"      removed {p.name}")
        td = mvr / "trees" / t["uuid"]
        if td.is_dir():
            shutil.rmtree(td)
            print(f"      removed trees/{t['uuid'][:8]}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
