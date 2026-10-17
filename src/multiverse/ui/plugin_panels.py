"""Plugin panels in a scope's local view: found by name, rendered off the event loop and clipped like core panels.

A data panel (`mv.dashboard.panel(type, render)`) is a function of its context that returns Markdown, Table or
Artifacts blocks. The view names it as a widget (`type` or `plugin:type`); a panel marked `auto` also appears after
the view's own widgets in every scope where its plugin is on, unless the view's `plugin_panels` says otherwise
("auto", "none" or a list of widget types). Naming happens when the view is read; rendering happens in the
snapshot worker, so the dashboard's event loop only draws plain widget dicts. Nothing here imports Textual
plugin code, and a failing panel becomes one line of text and a count against its plugin.
"""
from .. import plugins
from ..plugins import api, loader
from ..plugins.loader import DataPanel
from .panels import PANELS, error_widget

FAILED = object()


def find(root, kind):
    """(Loaded, DataPanel) for a widget type that a plugin on in `root` renders from data, or None.

    `PLUGIN:type` names one plugin; a bare type works while only one plugin offers it. ValueError when two do.
    """
    plugin, _, name = kind.rpartition(":")
    owners = [(loaded, spec) for loaded, panel_kind, spec in plugins.dashboard_panels(root)
              if isinstance(spec, DataPanel) and panel_kind == name and (not plugin or loaded.name == plugin)]
    if len(owners) > 1:
        raise ValueError(f"widget type {kind} is offered by {' and '.join(loaded.name for loaded, _ in owners)}; "
                         f"name it as PLUGIN:{kind}")
    return owners[0] if owners else None


def placeholder(loaded, spec, widget, title):
    """What the view keeps for a data panel until the snapshot renders it."""
    return {"type": "plugin", "title": title, "plugin": loaded.name, "kind": spec.kind, "widget": dict(widget)}


def render(root, loaded, spec, widget, title, rows):
    """The widget dicts one data panel gives right now: its blocks, or one line saying why not."""
    context = plugins.panel_context(loaded, root, widget, rows, plugins.on_here(root))
    result = loader.guard(loaded, f"panel {spec.kind}", spec.render, context, root=root, default=FAILED,
                          timeout=loader.SYNCHRONOUS_BUDGET)
    if result is FAILED:
        return [{**error_widget(title, f"{loaded.name}: panel {spec.kind} could not be shown (see mv2 plugins)"),
                 "plugin": loaded.name}]
    try:
        blocks = plugins.panel_blocks(result, title)
        shown = []
        for index, block in enumerate(blocks[:8]):
            parsed = PANELS[block["type"]].parse(block, title if index == 0 else "")
            shown.append({**parsed, "plugin": loaded.name})
        return shown
    except (api.Error, ValueError) as error:
        loader.record(loaded, f"panel {spec.kind}", error, root)
        return [{**error_widget(title, f"{loaded.name}: panel {spec.kind}: {error}"), "plugin": loaded.name}]


def allowed(option, loaded, kind):
    """Whether a view's `plugin_panels` lets an auto panel in."""
    if option == "none":
        return False
    return option == "auto" or kind in option or f"{loaded.name}:{kind}" in option


def expand(root, widgets, rows, option="auto"):
    """The view's widgets with every data panel rendered, then the auto panels that are allowed."""
    result, named = [], set()
    for widget in widgets:
        if widget.get("type") != "plugin":
            result.append(widget)
            continue
        named.add((widget["plugin"], widget["kind"]))
        found = find(root, f"{widget['plugin']}:{widget['kind']}")
        if found is None:  # turned off since the view was read
            result.append(error_widget(widget["title"], f"widget type {widget['kind']} needs plugin {widget['plugin']}, which is off here"))
            continue
        result += render(root, found[0], found[1], widget["widget"], widget["title"], rows)
    if option != "none":
        for loaded, kind, spec in plugins.dashboard_panels(root):
            if isinstance(spec, DataPanel) and spec.auto and (loaded.name, kind) not in named and allowed(option, loaded, kind):
                result += render(root, loaded, spec, {}, spec.title or loaded.name, rows)
    return result


def option_of(value):
    """A view's `plugin_panels`: "auto" (the default), "none" or a list of widget types; ValueError otherwise."""
    if value is None:
        return "auto"
    if value in ("auto", "none"):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError('plugin_panels must be "auto", "none" or a list of widget types')
