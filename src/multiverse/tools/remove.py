"""Move a stopped agent, or a subverse whose agents are all stopped, to the trash."""
from .. import relocate, universe


def configure(parser):
    parser.add_argument("target", help="Agent name/UUID, or with --subverse a subverse path or name")
    parser.add_argument("--subverse", action="store_true", help="Remove a subverse instead of an agent")


def run(root=None, *, target, subverse=False):
    root = universe.resolve(root)
    if subverse:
        return relocate.remove_subverse(relocate.exact(target, root))
    return relocate.remove_agent(root, target)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
