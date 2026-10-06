"""List removed agents and subverses and replaced refresh files, or delete them permanently with --empty."""
from ..universe import trash
from .. import universe


def configure(parser):
    parser.add_argument("--empty", action="store_true", help="Permanently delete every trashed entry")


def run(root=None, *, empty=False):
    return trash.contents(universe.resolve(root), empty)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
