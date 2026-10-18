"""List removed agents and subverses and replaced refresh files, or delete them permanently with --empty."""
from ..universe import trash
from .. import universe


def configure(parser):
    parser.add_argument("--empty", action="store_true", help="Permanently delete every trashed entry")
    parser.add_argument("--json", action="store_true", help="Print the full record as JSON")


def run(root=None, *, empty=False, json=False):  # `json`: presentation only
    return trash.contents(universe.resolve(root), empty)


def when(stamp):
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(str(stamp)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(stamp or "at an unknown time")


def cli_run(root=None, *, empty=False, json=False):
    record = run(root, empty=empty)
    if json:
        return record
    entries = record.get("entries") or []
    if empty:
        return (f"Deleted {len(entries)} entr{'ies' if len(entries) != 1 else 'y'} from the trash for good."
                if entries else "The trash was already empty.")
    if not entries:
        return "The trash is empty."
    return "\n".join(f"{entry.get('kind') or 'entry'} {entry.get('name') or '(unnamed)'}, removed {when(entry.get('removed_at'))}"
                     for entry in entries)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
