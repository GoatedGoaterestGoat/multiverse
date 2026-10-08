"""What a universe offers its agents: skills, commands, workflows and harnesses, and the bootstrap text."""
from pathlib import Path

from ..store import read
KINDS = ("skills", "commands", "workflows", "harnesses")


def catalog(root):
    result = []
    for kind in KINDS:
        filename = "harness.json" if kind == "harnesses" else "entry.json"
        for path in sorted((root / kind).glob(f"*/{filename}")):
            meta = read(path)
            result.append({"kind": kind, "name": path.parent.name,
                           "description": meta.get("description", ""), "path": str(path.parent)})
    return result


def command(root, query, arguments=""):
    candidates = [e for e in catalog(root) if e["kind"] in ("commands", "workflows", "skills")
                  and query in (e["name"], f'{e["kind"]}/{e["name"]}')]
    if len(candidates) != 1:
        raise ValueError(f"Expected one exact match for {query!r}; found {len(candidates)}. Use mv2 catalog.")
    entry = candidates[0]
    filename = "SKILL.md" if entry["kind"] == "skills" else "prompt.md"
    return Path(entry["path"], filename).read_text() + (f"\nUser arguments:\n{arguments}\n" if arguments else "")


def bootstrap(root, properties):
    # Only the short bootstrap and identity are automatic. Catalog bodies stay on disk.
    return (root / "bootstrap.md").read_text().strip() + "\n\n" + "\n".join([
        f'Universe: {read(root / "universe.json")["name"]} ({properties["universe_id"]})',
        f'Agent: {properties["name"]} ({properties["id"]})',
        f"Universe files: {root}",
        f'Your state: {root / "state" / "agents" / properties["id"]}',
    ]) + "\n"
