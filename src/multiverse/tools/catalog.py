"""List local capabilities without loading their prompts."""
from .. import universe

def configure(parser):
    pass


def run(root=None):
    return "\n".join(f'{e["kind"]}/{e["name"]}: {e["description"]}\n  {e["path"]}' for e in universe.catalog(universe.resolve(root)))


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
