"""The dashboard's colors, named once for Python styles and the stylesheet.

The palette is Catppuccin Mocha plus three shades for input fields and the
table cursor. `css_variables()` exposes every color to `app.tcss` as `$mv-NAME`.

A theme plugin (the one `plugins.json` names as `theme`) replaces any of these
by name. `apply` must run before the other ui modules are imported: some build
styles from these values when they load.
"""
import re

CRUST = "#11111b"
MANTLE = "#181825"
BASE = "#1e1e2e"
FIELD = "#242535"        # search and form inputs
SURFACE0 = "#313244"
CURSOR = "#31374e"       # selected row; hover
CURSOR_FOCUS = "#334252"  # selected row while the table has focus
SURFACE1 = "#45475a"
OVERLAY1 = "#7f849c"
OVERLAY2 = "#9399b2"
SUBTEXT0 = "#a6adc8"
TEXT = "#cdd6f4"
TEAL = "#94e2d5"
BLUE = "#89b4fa"
MAUVE = "#cba6f7"
PINK = "#f5c2e7"
GREEN = "#a6e3a1"
YELLOW = "#f9e2af"
PEACH = "#fab387"
RED = "#f38ba8"

# Subverse accents, in the order `"accent": "auto"` assigns them.
ACCENT_NAMES = ("TEAL", "MAUVE", "BLUE", "YELLOW", "PINK")
ACCENTS = tuple(globals()[name] for name in ACCENT_NAMES)


def css_variables():
    return {"mv-" + name.lower().replace("_", "-"): value for name, value in globals().items()
            if name.isupper() and isinstance(value, str) and value.startswith("#")}


def apply(colors):
    """Use `colors` ({"BLUE": "#89b4fa"}) in place of the defaults; returns the names it could not use."""
    rejected = []
    for name, value in (colors or {}).items():
        current = globals().get(name)
        if (isinstance(name, str) and name.isupper() and isinstance(current, str) and current.startswith("#")
                and isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value)):
            globals()[name] = value
        else:
            rejected.append(str(name))
    globals()["ACCENTS"] = tuple(globals()[name] for name in ACCENT_NAMES)
    return rejected
