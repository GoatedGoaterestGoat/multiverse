"""Check local definitions and report uncertain agent state."""
from .. import agents, universe
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
        except (OSError, ValueError, KeyError) as error:
            problems.append(f"{scope}: {error}")
    return {"ok": not problems, "problems": problems, "scope": str(root)}


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
