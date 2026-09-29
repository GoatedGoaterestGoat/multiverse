"""List removed agents and subverses, or delete them permanently with --empty."""
from .. import relocate, universe


def configure(parser):
    parser.add_argument("--empty", action="store_true", help="Permanently delete every trashed entry")


def run(root=None, *, empty=False):
    return relocate.trash(universe.resolve(root), empty)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
