"""Move a stopped agent, or a subverse whose agents are all stopped, to another universe."""
from .. import relocate, universe


def configure(parser):
    parser.add_argument("target", help="Agent name/UUID, or with --subverse a subverse path or name")
    parser.add_argument("destination", help="Universe directory, or a subverse path such as cad-lab/verification")
    parser.add_argument("--subverse", action="store_true", help="Move a subverse instead of an agent")


def run(root=None, *, target, destination, subverse=False):
    root = universe.resolve(root)
    if subverse:
        return relocate.move_subverse(relocate.exact(target, root), destination)
    return relocate.move_agent(root, target, destination)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
