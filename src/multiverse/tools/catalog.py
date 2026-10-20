"""List local capabilities without loading their prompts."""
import sys
from .. import universe
from ..universe import catalog

def configure(parser):
    parser.add_argument("--json", action="store_true",
                        help="Print the entries as a JSON list of kind, name, description and path")


def warning(error):
    print('mv2: warning: ' + ' '.join(str(error).split()), file=sys.stderr)


def run(root=None, json=False):
    entries = catalog.catalog(universe.resolve(root), on_error=warning)
    if json:
        return entries
    return "\n".join(f'{e["kind"]}/{e["name"]}: {e["description"]}\n  {e["path"]}' for e in entries)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
