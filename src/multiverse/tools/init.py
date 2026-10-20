"""Clean template install into DIRECTORY/.multiverse."""
from pathlib import Path
from ..universe import templates
from ..store import read

def configure(parser):
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument("--name")
    parser.add_argument("--template", default="base")
    parser.add_argument("--json", action="store_true", help="Print the universe's record as JSON")


def cli_run(root=None, directory=".", name=None, template="base", json=False):
    record = run(root, directory, name, template)
    return record if json else f"Created universe {record.get('name')} in {record.get('universe')}."


def run(root=None, directory=".", name=None, template="base", json=False):  # `json`: presentation only
    workspace = Path(directory).resolve()
    installed = templates.create(workspace / ".multiverse", name or workspace.name, template, workspace)
    return {"universe": str(installed), **read(installed / "universe.json")}


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
