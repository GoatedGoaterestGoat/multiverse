"""Browse universes and operate agents."""
from .. import universe

def configure(parser):
    parser.add_argument("--demo", action="store_true", help="Explore an in-memory demo without starting processes")


def run(root=None, *, demo=False):
    backend = None
    if demo:
        from ..demo import DemoBackend
        backend = DemoBackend()
        root = backend.root
    else:
        root = universe.resolve(root)
    try:
        from ..tui import start
    except ModuleNotFoundError as error:
        raise RuntimeError("Install the TUI dependencies with `uv sync --project multiverse-v2` or `pip install ./multiverse-v2`.") from error
    start(root, backend)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
