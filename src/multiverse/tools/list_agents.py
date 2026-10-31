"""Read properties and observe matching tmux panes."""
from pathlib import Path
from ..display import error_text, shorten, state, live_label
from ..runtime import lifecycle
from ..universe import focus
from .. import universe

NAME_WIDTH = 24
# What --columns can show, in the text listing. `status` is what a live agent is doing, or why it is not live.
COLUMNS = {
    "name": lambda row, root, wide: row["name"],
    "status": lambda row, root, wide: state(row),
    "phase": lambda row, root, wide: row.get("phase") or "?",
    "summary": lambda row, root, wide: " ".join((row.get("summary") or "").split()) if wide else shorten(row.get("summary", "")),
    "harness": lambda row, root, wide: row.get("harness") or "?",
    "universe": lambda row, root, wide: place(row, root) or ".",
    "id": lambda row, root, wide: row.get("id") or "",
}
DEFAULT_COLUMNS = ("name", "status", "phase", "summary")
# The --json fields that need each agent's token usage read, the slow part of a listing; nothing else reads it.
USAGE_FIELDS = ("usage", "tokens", "usage_detail")
# Where a refusal under --json points: there are too many fields to list in one line.
JSON_FIELDS_HINT = "fields of mv2 list --json, such as id,name,live,status; plain mv2 list --json shows every field"


def configure(parser):
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--wide", action="store_true", help="Show full summaries and errors")
    parser.add_argument("--columns", help=f"Show these columns, comma-separated, in this order: {', '.join(COLUMNS)}. "
                                          "One column prints one value per line and nothing else. With --json, name "
                                          "fields of each entry instead, such as id,name,live,status; usage is read "
                                          f"only for {', '.join(USAGE_FIELDS)}")
    parser.add_argument("--json", action="store_true")


def chosen(columns, json=False):
    """The column names --columns asks for, in order, or with `json` the fields of each --json entry; ValueError for
    anything else, naming what it can show."""
    names = [name.strip() for name in columns.split(",")]
    if not any(names):  # an empty --columns, as from --columns "$UNSET", must not fall back to the whole table
        raise ValueError(f"Name at least one of the {JSON_FIELDS_HINT}" if json
                         else f"Name at least one column: {', '.join(COLUMNS)}")
    if not all(names):
        raise ValueError(f"--columns {columns!r} has an empty entry: separate column names with single commas")
    unknown = [name for name in names if name not in (lifecycle.ROW_FIELDS if json else COLUMNS)]
    if unknown and json:
        raise ValueError(f"Not a field of mv2 list --json: {', '.join(unknown)}. Name {JSON_FIELDS_HINT}")
    if unknown:
        raise ValueError(f"Unknown column {', '.join(unknown)}: choose from {', '.join(COLUMNS)}")
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ValueError(f"--columns names {', '.join(repeated)} more than once")
    return names


def place(row, root):
    """The universe an agent is in, or None when it is in this one: a subverse by its path below this universe."""
    path = Path(row["universe"])
    if path == Path(root):
        return None
    try:
        return "/".join(part for part in path.relative_to(root).parts if part != "subverses")
    except ValueError:
        return str(path)


def run(root=None, *, recursive=False, json=False, wide=False, columns=None):
    """The listing as text, or with `json` as rows: every field, or with `columns` enough for those fields (cli_run
    keeps just them). Usage is read only where the output shows it, as the slowest part of a listing."""
    names = chosen(columns, json) if columns is not None else (None if json else DEFAULT_COLUMNS)
    root = universe.resolve(root)
    rows = lifecycle.listing(root, recursive, usage=json and (names is None or any(name in USAGE_FIELDS for name in names)))
    if json:
        return rows
    if len(names) == 1:  # for scripts: one value per agent, and nothing that could be read as one
        return "\n".join(COLUMNS[names[0]](row, root, wide) for row in rows)
    if not rows:
        return "No agents."
    # Agents that wait on a person come first; otherwise the order is the listing's.
    rows = sorted(rows, key=lambda row: row.get("status") != "waiting")
    table = [[COLUMNS[name](row, root, wide) for name in names] for row in rows]
    widths = [max(len(values[index]) for values in table) for index in range(len(names) - 1)]
    widths = [min(width, NAME_WIDTH) if name == "name" else width for name, width in zip(names, widths)]
    lines = []
    for row, values in zip(rows, table):
        line = "  ".join([f"{value:{width}}" for value, width in zip(values, widths)] + values[-1:])
        where = None if columns is not None else place(row, root)
        lines.append((line + (f"  [{where}]" if where else "")).rstrip())
        lines.extend(problems(row, wide))
    return "\n".join(lines)


def problems(row, wide):
    """The lines under an agent's row that say what is wrong with it, or what to know first."""
    lines = []
    damage = row.get('damaged')
    error = (f"{damage['file']}: {damage['reason']}" if damage else None) or (row.get('runtime', {}).get('native_refusal') or {}).get('message') or row.get("connection_error") or row.get("launch_error")
    if error:
        lines.append("  ! " + (" | ".join(line.strip() for line in error.splitlines() if line.strip()) if wide or damage else error_text(error)))
    if row.get('delivery_problem'):
        lines.append('  ! ' + row['delivery_problem'])
    if row.get('recovery_note'):
        lines.append('  Note: Reconnected automatically.')
    if row.get('focus'):
        lines.append(f"  Focused {focus.ends(row['focus'])}: send it only --delivery inbox-only messages.")
    return lines


def cli_run(root=None, *, recursive=False, json=False, wide=False, columns=None):
    result = run(root, recursive=recursive, json=json, wide=wide, columns=columns)
    if not json:
        return result or None  # one column and no agents: print nothing, not an empty line
    # The dashboard uses run(json=True) and retains the core observation enum.
    rows = [{**row, 'live': live_label(row),
             **({'reason': 'Launch is being prepared'} if live_label(row) == 'starting' else {})}
            for row in result]
    if columns is None:
        return rows
    names = chosen(columns, json=True)  # each entry keeps the fields asked, in that order; one it lacks is left out
    return [{name: row[name] for name in names if name in row} for row in rows]


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
