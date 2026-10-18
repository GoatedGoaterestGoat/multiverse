"""What a universe offers its agents: skills, commands, workflows and harnesses, and the bootstrap text."""
from pathlib import Path

from ..store import read
from . import records
KINDS = ("skills", "commands", "workflows", "harnesses")


def _own(root, on_error=None):
    result = []
    for kind in KINDS:
        filename = "harness.json" if kind == "harnesses" else "entry.json"
        for path in sorted((root / kind).glob(f"*/{filename}")):
            try:
                meta = records.runtime(path)
            except records.RecordError as error:
                problem = ValueError(f'{error.file}: {error.reason}')
                if on_error is None:
                    raise problem from error
                on_error(problem)
                continue
            result.append({"kind": kind, "name": path.parent.name,
                           "description": meta.get("description", ""), "path": str(path.parent)})
    return result


def _plugin_entries(root):
    """Catalog entries of plugins on here; a plugin that cannot be read adds nothing."""
    from .. import plugins
    try:
        return plugins.catalog_entries(root)
    except Exception:
        return []


def catalog(root, *, on_error=None):
    """The universe's own entries, then those of plugins on here (with `plugin`) that no own entry of the same kind and name shadows."""
    result = _own(root, on_error)
    taken = {(entry["kind"], entry["name"]) for entry in result}
    return result + [entry for entry in _plugin_entries(root) if (entry["kind"], entry["name"]) not in taken]


def command(root, query, arguments=""):
    reference, qualified, plugin = query.partition("@")
    if qualified:
        # `name@plugin` or `kind/name@plugin` names a plugin's entry, even one the universe shadows.
        entries = [e for e in _plugin_entries(root) if e["plugin"] == plugin]
    else:
        entries = catalog(root)
    candidates = [e for e in entries if e["kind"] in ("commands", "workflows", "skills")
                  and reference in (e["name"], f'{e["kind"]}/{e["name"]}')]
    if len(candidates) != 1:
        raise ValueError(f"Expected one exact match for {query!r}; found {len(candidates)}. Use mv2 catalog.")
    entry = candidates[0]
    filename = "SKILL.md" if entry["kind"] == "skills" else "prompt.md"
    return Path(entry["path"], filename).read_text() + (f"\nUser arguments:\n{arguments}\n" if arguments else "")


def bootstrap(root, properties):
    # Only the short bootstrap and identity are automatic. Catalog bodies stay on disk.
    text = (root / "bootstrap.md").read_text().strip() + "\n\n" + "\n".join([
        f'Universe: {read(root / "universe.json")["name"]} ({properties["universe_id"]})',
        f'Agent: {properties["name"]} ({properties["id"]})',
        f"Universe files: {root}",
        f'Your state: {root / "state" / "agents" / properties["id"]}',
    ]) + "\n"
    lines = _plugin_lines(root)
    return text + ("\n" + "".join(line + "\n" for line in lines) if lines else "")


def _plugin_lines(root):
    """At most 2 short lines from each plugin on here, in plugin-name order, after the identity lines."""
    from .. import plugins
    try:
        return plugins.bootstrap_lines(root)
    except Exception:
        return []
