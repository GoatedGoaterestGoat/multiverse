"""List local capabilities without loading their prompts."""
from .. import universe

def configure(parser):
    parser.add_argument("--json", action="store_true",
                        help="Print the entries as a JSON list of kind, name, description and path")


def run(root=None, json=False):
    entries = universe.catalog(universe.resolve(root))
    if json:
        return entries
    return "\n".join(f'{e["kind"]}/{e["name"]}: {e["description"]}\n  {e["path"]}' for e in entries)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
