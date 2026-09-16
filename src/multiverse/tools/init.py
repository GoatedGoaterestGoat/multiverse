"""Clean template install into DIRECTORY/.multiverse."""
from pathlib import Path
from .. import universe
from ..store import read

def configure(parser):
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument("--name")
    parser.add_argument("--template", default="base")


def run(root=None, directory=".", name=None, template="base"):
    workspace = Path(directory).resolve()
    installed = universe.create(workspace / ".multiverse", name or workspace.name, template, workspace)
    return {"universe": str(installed), **read(installed / "universe.json")}


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
