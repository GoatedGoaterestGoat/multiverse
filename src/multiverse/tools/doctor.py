"""Check local definitions and report uncertain agent state."""
from .. import agents, harnesses, universe
from ..store import inbox

def configure(parser):
    pass


def run(root=None):
    root = universe.resolve(root)
    problems = []
    for scope in universe.tree(root):
        try:
            if not universe.workspace(scope).is_dir():
                problems.append(f"Missing workspace: {scope}")
            universe.catalog(scope)
            for item in agents.listing(scope):
                if item["live"] == "unknown" or item.get("runtime", {}).get("state") == "failed":
                    problems.append(f'{item["name"]}: {item.get("reason") or item.get("runtime", {}).get("error", "Launch failed")}')
                inbox(scope / "state" / "agents" / item["id"], all_messages=True)
            problems.extend(_compaction(scope))
        except (OSError, ValueError, KeyError) as error:
            problems.append(f"{scope}: {error}")
    return {"ok": not problems, "problems": problems, "scope": str(root)}


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
