"""Read properties and observe matching tmux panes."""
from pathlib import Path
from ..display import error_text, shorten, state
from ..runtime import lifecycle
from .. import universe

NAME_WIDTH = 24


def configure(parser):
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--wide", action="store_true", help="Show full summaries and errors")
    parser.add_argument("--json", action="store_true")


def place(row, root):
    """The universe an agent is in, or None when it is in this one: a subverse by its path below this universe."""
    path = Path(row["universe"])
    if path == Path(root):
        return None
    try:
        return "/".join(part for part in path.relative_to(root).parts if part != "subverses")
    except ValueError:
        return str(path)


def run(root=None, *, recursive=False, json=False, wide=False):
    root = universe.resolve(root)
    rows = lifecycle.listing(root, recursive)
    if json:
        return rows
    if not rows:
        return "No agents."
    # Agents that wait on a person come first; otherwise the order is the listing's.
    rows = sorted(rows, key=lambda row: row.get("status") != "waiting")
    width = min(max(len(row["name"]) for row in rows), NAME_WIDTH)
    lines = []
    for row in rows:
        summary = " ".join(row.get("summary", "").split()) if wide else shorten(row.get("summary", ""))
        where = place(row, root)
        line = f'{row["name"]:{width}}  {state(row):8}  {row.get("phase", "?"):8}  {summary}'
        lines.append((line + (f"  [{where}]" if where else "")).rstrip())
        damage = row.get('damaged')
        error = (f"{damage['file']}: {damage['reason']}" if damage else None) or (row.get('runtime', {}).get('native_refusal') or {}).get('message') or row.get("connection_error") or row.get("launch_error")
        if error:
            lines.append("  ! " + (" | ".join(line.strip() for line in error.splitlines() if line.strip()) if wide else error_text(error)))
        if row.get('delivery_problem'):
            lines.append('  ! ' + row['delivery_problem'])
        if row.get('recovery_note'):
            lines.append('  Note: Reconnected automatically.')
    return "\n".join(lines)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
