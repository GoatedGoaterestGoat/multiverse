"""Browse universes and operate agents."""
from .. import universe

def configure(parser):
    pass


def run(root=None):
    root = universe.resolve(root)
    try:
        from ..tui import start
    except ModuleNotFoundError as error:
        raise RuntimeError("Install the TUI dependencies with `uv sync` or `pip install .`.") from error
    start(root)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
