"""Local view panels: one Panel per widget type a view's `widgets` list can name.

A view file is declarative: it cannot import code or run commands. It can name
a widget type that a plugin on in its universe provides (`mv.dashboard.panel`),
as `type` or `plugin:type`; elsewhere that widget shows what it needs.
"""
from pathlib import Path

from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Label, Link, Markdown, Static


class Panel:
    """One kind of local view widget: how its JSON is checked, and the widgets it shows.

    `parse` returns the normalized widget or raises ValueError, which becomes an
    inline error in place of that widget. `compose` returns Textual widgets.
    """
    def parse(self, widget, title):
        raise NotImplementedError

    def compose(self, view, root, widget, index):
        raise NotImplementedError


class MarkdownPanel(Panel):
    def parse(self, widget, title):
        return {"type": "markdown", "title": title, "text": str(widget.get("text", ""))[:32768]}

    def compose(self, view, root, widget, index):
        return [Markdown(widget["text"], open_links=False)]


class TablePanel(Panel):
    def parse(self, widget, title):
        columns, rows = widget.get("columns"), widget.get("rows", [])
        if not isinstance(columns, list) or not columns or not all(isinstance(c, str) for c in columns):
            raise ValueError("table columns must be a nonempty string array")
        if not isinstance(rows, list) or not all(isinstance(r, list) and len(r) == len(columns) for r in rows):
            raise ValueError("table rows must match columns")
        return {"type": "table", "title": title, "columns": columns[:16],
                "rows": [[str(c)[:2000] for c in row[:16]] for row in rows[:500]]}

    def compose(self, view, root, widget, index):
        table = DataTable(id=f"view-table-{index}", classes="view-table", cursor_type="row")
        table.add_columns(*widget["columns"])
        for row in widget["rows"]:
            # Literal cells: fixture data must not become Rich markup.
            table.add_row(*(Text(cell) for cell in row))
        table.styles.height = min(12, max(3, len(widget["rows"]) + 1))
        return [table]


class ArtifactsPanel(Panel):
    def parse(self, widget, title):
        items = widget.get("items", [])
        if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get("path"), str) for i in items):
            raise ValueError("artifacts items require a path")
        return {"type": "artifacts", "title": title, "items": [
            {"label": str(i.get("label", i["path"]))[:200], "path": i["path"],
             "description": str(i.get("description", ""))[:2000]} for i in items[:100]]}

    def compose(self, view, root, widget, index):
        children = []
        for item_index, item in enumerate(widget["items"]):
            key = f"artifact-{index}-{item_index}"
            view.artifacts[key] = (root, item)
            children.append(Button(Text(item["label"]), id=key, classes="artifact-button"))
            if item["description"]:
                children.append(Static(item["description"], classes="artifact-description", markup=False))
        return children


# A view's `widgets` entries name one of these by "type".
PANELS = {"markdown": MarkdownPanel(), "table": TablePanel(), "artifacts": ArtifactsPanel()}
# Plugin panels by widget type and universe, instantiated once: {(type, universe): Panel}.
_plugin_panels = {}


def panel(kind, root=None):
    """The Panel for a widget type: core, else the plugin one on in `root`; ValueError says what it needs."""
    if kind in PANELS:
        return PANELS[kind]
    if root is None:
        raise ValueError(f"unknown widget type: {kind}")
    try:
        changed = (Path(root) / "plugins.json").stat().st_mtime_ns
    except OSError:
        changed = None
    key = (kind, str(root), changed)
    if key not in _plugin_panels:
        # Kept until plugins.json changes or columns.reset_plugin_caches(), a failure too:
        # a refresh never imports again.
        from .. import plugins
        try:
            loaded, reference = plugins.dashboard_panel(root, kind)
            _plugin_panels[key] = loaded.resolve(reference)()
        except plugins.PluginError as error:
            _plugin_panels[key] = ValueError(str(error))
        except Exception as error:
            _plugin_panels[key] = ValueError(f"widget type {kind} could not be loaded: {type(error).__name__}: {error}")
    found = _plugin_panels[key]
    if isinstance(found, ValueError):
        raise ValueError(str(found))
    return found


def _data_panel(kind, root):
    """A function making the placeholder for a widget that a plugin renders from data, or None."""
    from . import plugin_panels
    found = plugin_panels.find(root, kind)
    if found is None:
        return None
    return lambda widget, title: plugin_panels.placeholder(found[0], found[1], widget, title)


def normalize_widgets(value, root=None):
    """Validate each widget independently so a bad widget cannot hide the fleet."""
    if not isinstance(value, list):
        value = [None]
    result = []
    for index, widget in enumerate(value[:32]):
        try:
            if not isinstance(widget, dict):
                raise ValueError("expected an object")
            kind = widget.get("type")
            title = str(widget.get("title", kind or "Widget"))[:200]
            if not isinstance(kind, str):
                raise ValueError(f"unknown widget type: {kind}")
            if kind not in PANELS and root is not None and (data := _data_panel(kind, root)) is not None:
                result.append(data(widget, title))  # rendered later, off the event loop (ui/plugin_panels.py)
                continue
            result.append(panel(kind, root).parse(widget, title))
        except Exception as error:  # any failure stays inside its widget
            result.append(error_widget(f"Widget {index + 1} error", error))
    return result


def error_widget(title, error):
    """A markdown widget in place of one that could not be read or shown."""
    return {"type": "markdown", "title": title, "text": str(error)}


def artifact_path(root, value):
    root = Path(root).resolve()
    relative = Path(value)
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root):
        raise ValueError("Artifact must stay inside this universe directory")
    if not path.is_file():
        raise ValueError("Artifact is missing or is not a regular file")
    return path


class ArtifactPreview(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, root, item):
        super().__init__()
        self.root, self.item = root, item

    def compose(self):
        with Vertical(id="details-dialog"):
            yield Label(self.item["label"], classes="detail-title", markup=False)
            with VerticalScroll(id="full-details"):
                try:
                    path = artifact_path(self.root, self.item["path"])
                    yield Static(self.item["description"], markup=False)
                    yield Link("Open artifact in default viewer ↗", url=path.as_uri(), id="artifact-link")
                    yield Static(str(path), markup=False)
                    if path.stat().st_size > 262144:
                        yield Static("Preview omitted: file exceeds 256 KiB. Use the artifact link.")
                    else:
                        try:
                            content = path.read_text(encoding="utf-8")
                        except UnicodeError:
                            yield Static("Binary artifact. Use the artifact link to view it.")
                        else:
                            if path.suffix.lower() in (".md", ".markdown"):
                                yield Markdown(content, open_links=False)
                            else:
                                yield Static(Syntax(content, "xml" if path.suffix == ".svg" else "text", word_wrap=True))
                except (OSError, ValueError) as error:
                    yield Static(str(error), id="artifact-error", markup=False)
        yield Footer()

    def action_close(self):
        self.dismiss()


class LocalView(VerticalScroll):
    """A focusable queue and preview surface beside the compact hierarchy."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.signature = None
        self.artifacts = {}

    async def show_view(self, root, widgets):
        signature = (str(root), widgets)
        self.set_class(bool(widgets), "populated")
        if signature == self.signature:
            return
        self.signature = signature
        self.artifacts = {}
        await self.remove_children()
        children = []
        for index, widget in enumerate(widgets):
            try:
                shown = panel(widget["type"], root).compose(self, root, widget, index)
            except Exception as error:
                widget = error_widget(f"Widget {index + 1} error", error)
                shown = PANELS["markdown"].compose(self, root, widget, index)
            title = widget["title"]
            plugin = widget.get("plugin")
            if plugin and title and title != plugin:
                title = f"{title} · {plugin}"  # where a plugin's panel comes from
            if title:
                children.append(Label(title, classes="view-title", markup=False))
            children.extend(shown)
        if children:
            await self.mount(*children)

    @on(Button.Pressed, ".artifact-button")
    def open_artifact(self, event):
        event.stop()
        if event.button.id in self.artifacts:
            self.app.push_screen(ArtifactPreview(*self.artifacts[event.button.id]))
