"""Read properties and observe matching tmux panes."""
from pathlib import Path
from .. import agents, universe

SUMMARY_LENGTH = 80
ERROR_LENGTH = 200
NAME_WIDTH = 24


def configure(parser):
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--wide", action="store_true", help="Show full summaries and errors")
    parser.add_argument("--json", action="store_true")


def shorten(text, limit=SUMMARY_LENGTH):
    """One line of at most `limit` characters, cut at a word boundary; only a single longer word is cut inside."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if not text[limit - 1].isspace() and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def error_text(error, tail=2):
    """A launch error on one line. Its first line says what happened and the lines at the end of the terminal
    usually say why, so a long one keeps the first line and the last `tail`, with `…` where lines were left out."""
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    if len(lines) > tail + 1:
        lines = [lines[0], "…", *lines[-tail:]]
    return " | ".join(line if line == "…" else shorten(line, ERROR_LENGTH) for line in lines)


def state(row):
    """What a person asks first: what a live agent is doing, otherwise why it is not live."""
    if row["live"] == "alive":
        return row["status"] if row.get("status") not in (None, "unknown", "—") else "alive"
    return row["live"]


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
    rows = agents.listing(root, recursive)
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
        error = row.get("connection_error") or row.get("launch_error")
        if error:
            lines.append("  ! " + (" | ".join(line.strip() for line in error.splitlines() if line.strip()) if wide else error_text(error)))
        if row.get('recovery_note'):
            lines.append('  Note: Reconnected automatically.')
    return "\n".join(lines)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
