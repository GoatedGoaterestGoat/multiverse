"""Create a nested, independent universe."""
from .. import universe
from ..universe import inheritance, templates
from ..store import read

def configure(parser):
    parser.add_argument("name")
    parser.add_argument("--template", help="Use a clean template instead of copying the parent's definitions")
    parser.add_argument("--workspace")
    parser.add_argument("--json", action="store_true", help="Print the subverse's record as JSON")


def cli_run(root=None, *, name, template=None, workspace=None, json=False):
    record = run(root, name=name, template=template, workspace=workspace)
    return record if json else f"Created subverse {record.get('name') or name} in {record.get('universe')}."


def run(root=None, *, name, template=None, workspace=None, json=False):  # `json`: presentation only
    root = universe.resolve(root)
    universe.slug(name)
    destination = root / "subverses" / name
    workdir = workspace or universe.workspace(root)
    child = (inheritance.create(root, destination, name, workdir) if template is None else
             templates.create(destination, name, template, workdir))
    return {"universe": str(child), **read(child / "universe.json")}


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
