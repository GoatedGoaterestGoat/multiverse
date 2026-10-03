"""Check local definitions and report uncertain agent state."""
import sys

from .. import agents, harnesses, universe
from ..store import inbox

def configure(parser):
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")


def one_line(text):
    return " | ".join(line.strip() for line in str(text).splitlines() if line.strip()).rstrip(".")


def check(root):
    """Each problem twice: as recorded (what --json and callers get) and as a sentence."""
    found = []
    for scope in universe.tree(root):
        try:
            if not universe.workspace(scope).is_dir():
                found.append((f"Missing workspace: {scope}", f"The workspace of {scope} does not exist."))
            universe.catalog(scope)
            for item in agents.listing(scope):
                runtime = item.get("runtime", {})
                failed = bool(item.get("launch_error")) or runtime.get("state") == "failed"
                if item["live"] == "unknown" or failed:
                    reason = item.get("reason") or runtime.get("error", "Launch failed")
                    found.append((f'{item["name"]}: {reason}', f"{item['name']}'s " +
                                  ("last launch failed: " if failed else "state is unknown: ") + one_line(reason) + "."))
                inbox(scope / "state" / "agents" / item["id"], all_messages=True)
            found.extend((problem, problem) for problem in _compaction(scope))
        except (OSError, ValueError, KeyError) as error:
            found.append((f"{scope}: {error}", f"{scope}: {error}"))
    return found


def run(root=None, json=False):  # `json`: presentation only
    root = universe.resolve(root)
    problems = [recorded for recorded, _ in check(root)]
    return {"ok": not problems, "problems": problems, "scope": str(root)}


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
    found = check(universe.resolve(root))
    print(sentences(found))
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
