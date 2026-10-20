"""Move a stopped agent, or a subverse whose agents are all stopped, to the trash."""
from ..universe import relocate
from .. import universe


def configure(parser):
    parser.add_argument("target", help="Agent name/UUID, or with --subverse a subverse path or name")
    parser.add_argument("--subverse", action="store_true", help="Remove a subverse instead of an agent")
    parser.add_argument("--json", action="store_true", help="Print the full record as JSON")


def run(root=None, *, target, subverse=False, json=False):  # `json`: presentation only
    root = universe.resolve(root)
    if subverse:
        return relocate.remove_subverse(relocate.exact(target, root))
    return relocate.remove_agent(root, target)


def cli_run(root=None, *, target, subverse=False, json=False):
    record = run(root, target=target, subverse=subverse)
    if json:
        return record
    kind = "subverse " if record.get("kind") == "subverse" else ""
    return f"Moved {kind}{record.get('name') or target} to the trash."


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
