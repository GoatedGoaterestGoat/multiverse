"""Create a nested, independent universe."""
from .. import universe
from ..universe import templates
from ..store import read

def configure(parser):
    parser.add_argument("name")
    parser.add_argument("--template", default="base")
    parser.add_argument("--workspace")


def run(root=None, *, name, template="base", workspace=None):
    root = universe.resolve(root)
    universe.slug(name)
    child = templates.create(root / "subverses" / name, name, template, workspace or universe.workspace(root))
    return {"universe": str(child), **read(child / "universe.json")}


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
