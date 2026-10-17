"""Browse universes and operate agents."""
from .. import universe

def configure(parser):
    pass


def run(root=None):
    root = universe.resolve(root)
    from ..plugins import loader
    loader.process = "dashboard"  # what plugins see as mv.process
    _theme(root)
    try:
        from ..ui.app import start
    except ModuleNotFoundError as error:
        raise RuntimeError("Install the TUI dependencies with `uv sync` or `pip install .`.") from error
    start(root)


def _theme(root):
    """Apply the theme plugin plugins.json names, before the dashboard's modules read the palette."""
    try:
        from .. import plugins
        from ..plugins import loader
        from ..ui import theme
        chosen = plugins.theme_colors(root)
        if chosen is not None:
            loaded, colors = chosen
            rejected = theme.apply(colors)
            if rejected:
                loader.record(loaded, "theme", ValueError(f"not #rrggbb colors of the palette: {', '.join(rejected)}"), root)
    except Exception:
        pass  # the dashboard opens with its own colors


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
