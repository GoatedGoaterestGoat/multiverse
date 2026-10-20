"""Move a stopped agent, or a subverse whose agents are all stopped, to another universe."""
from ..universe import relocate
from .. import universe


def configure(parser):
    parser.add_argument("target", help="Agent name/UUID, or with --subverse a subverse path or name")
    parser.add_argument("destination", help="Universe directory, or a subverse path such as cad-lab/verification")
    parser.add_argument("--subverse", action="store_true", help="Move a subverse instead of an agent")
    parser.add_argument("--json", action="store_true", help="Print the moved record as JSON")


def cli_run(root=None, *, target, destination, subverse=False, json=False):
    record = run(root, target=target, destination=destination, subverse=subverse)
    if json:
        return record
    where = record.get("universe")
    if subverse:
        return f"Moved subverse {record.get('name') or target} to {where}."
    from pathlib import Path
    from ..store import read
    try:
        name = read(Path(where) / "universe.json").get("name")
    except (OSError, ValueError, TypeError):
        name = None
    return f"Moved {record.get('name') or target} to {name} ({where})." if name else f"Moved {record.get('name') or target} to {where}."


def run(root=None, *, target, destination, subverse=False, json=False):  # `json`: presentation only
    # Relative names resolve from the chosen universe, for targets and destinations alike.
    root = universe.resolve(root)
    destination = relocate.exact(destination, root)
    if subverse:
        return relocate.move_subverse(relocate.exact(target, root), destination)
    return relocate.move_agent(root, target, destination)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
