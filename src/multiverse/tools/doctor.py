"""Check local definitions and report uncertain agent state."""
import sys

from .. import agents, harnesses, universe
from ..universe import catalog
from ..store import inbox

def configure(parser):
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")


def one_line(text):
    return " | ".join(line.strip() for line in str(text).splitlines() if line.strip()).rstrip(".")


def check(root, notes=None):
    """Each problem twice: as recorded (what --json and callers get) and as a sentence."""
    found = []
    for scope in universe.tree(root):
        try:
            if not universe.workspace(scope).is_dir():
                found.append((f"Missing workspace: {scope}", f"The workspace of {scope} does not exist."))
            catalog.catalog(scope)
            for item in agents.listing(scope):
                runtime = item.get("runtime", {})
                failed = bool(item.get("launch_error")) or runtime.get("state") == "failed"
                recovery = runtime.get('auto_recovery') or {}
                connection_error = item.get('connection_error')
                if item["live"] == "unknown" or failed or connection_error or recovery.get('state') in ('failed', 'skipped'):
                    reason = connection_error or item.get("reason") or recovery.get('reason') or runtime.get("error", "Launch failed")
                    found.append((f'{item["name"]}: {reason}', f"{item['name']}'s " +
                                  ("last launch failed: " if failed else "connection was lost: " if connection_error
                                   else "automatic reconnect failed: " if recovery.get('state') in ('failed', 'skipped')
                                   else "state is unknown: ") + one_line(reason) + "."))
                elif item.get('recovery_note') and notes is not None:
                    notes.append(f"{item['name']} reconnected automatically.")
                inbox(scope / "state" / "agents" / item["id"], all_messages=True)
            found.extend((problem, problem) for problem in _compaction(scope))
        except (OSError, ValueError, KeyError) as error:
            found.append((f"{scope}: {error}", f"{scope}: {error}"))
    return found


def run(root=None, json=False):  # `json`: presentation only
    root = universe.resolve(root)
    notes = []
    problems = [recorded for recorded, _ in check(root, notes)]
    return {"ok": not problems, "problems": problems, "scope": str(root), **({'notes': notes} if notes else {})}


def sentences(found):
    """`No problems.`, `1 problem: ...`, or a short list."""
    texts = [text if text.endswith((".", "!", "?")) else text + "." for _, text in found]
    if not texts:
        return "No problems."
    if len(texts) == 1:
        return f"1 problem: {texts[0]}"
    return f"{len(texts)} problems:\n" + "\n".join(f"- {text}" for text in texts)


def cli_run(root=None, *, json=False):
    if json:
        return run(root)
    notes = []
    found = check(universe.resolve(root), notes)
    print(sentences(found))
    for note in notes:
        print('Note: ' + note)
    if found:
        sys.exit(1)


def _compaction(scope):
    """Why this universe's compaction.json cannot be used, by the file or by a harness that compacts."""
    from ..compaction import config
    try:
        config.load(scope)
    except ValueError as error:
        return [str(error)]
    found = []
    for manifest in sorted((scope / "harnesses").glob("*/harness.json")):
        try:
            adapter = harnesses.load(scope, manifest.parent.name)
        except (OSError, ValueError, KeyError):
            continue
        state = getattr(adapter, "compaction_state", None)
        problem = state(scope)[1] if state else None
        if problem:
            found.append(problem)
    return found


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
