"""Small declarative local views. No imports, commands or executable plugins."""
from pathlib import Path

from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Label, Link, Markdown, Static


def normalize_widgets(value):
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
            if kind == "markdown":
                result.append({"type": kind, "title": title, "text": str(widget.get("text", ""))[:32768]})
            elif kind == "table":
                columns, rows = widget.get("columns"), widget.get("rows", [])
                if not isinstance(columns, list) or not columns or not all(isinstance(c, str) for c in columns):
                    raise ValueError("table columns must be a nonempty string array")
                if not isinstance(rows, list) or not all(isinstance(r, list) and len(r) == len(columns) for r in rows):
                    raise ValueError("table rows must match columns")
                result.append({"type": kind, "title": title, "columns": columns[:16],
                               "rows": [[str(c)[:2000] for c in row[:16]] for row in rows[:500]]})
            elif kind == "artifacts":
                items = widget.get("items", [])
                if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get("path"), str) for i in items):
                    raise ValueError("artifacts items require a path")
                result.append({"type": kind, "title": title, "items": [
                    {"label": str(i.get("label", i["path"]))[:200], "path": i["path"],
                     "description": str(i.get("description", ""))[:2000]} for i in items[:100]]})
            else:
                raise ValueError(f"unknown widget type: {kind}")
        except ValueError as error:
            result.append({"type": "markdown", "title": f"Widget {index + 1} error", "text": str(error)})
    return result


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
            children.append(Label(widget["title"], classes="view-title", markup=False))
            if widget["type"] == "markdown":
                children.append(Markdown(widget["text"], open_links=False))
            elif widget["type"] == "table":
                table = DataTable(id=f"view-table-{index}", classes="view-table", cursor_type="row")
                table.add_columns(*widget["columns"])
                for row in widget["rows"]:
                    # Literal cells: fixture data must not become Rich markup.
                    table.add_row(*(Text(cell) for cell in row))
                table.styles.height = min(12, max(3, len(widget["rows"]) + 1))
                children.append(table)
            else:
                for item_index, item in enumerate(widget["items"]):
                    key = f"artifact-{index}-{item_index}"
                    self.artifacts[key] = (root, item)
                    children.append(Button(Text(item["label"]), id=key, classes="artifact-button"))
                    if item["description"]:
                        children.append(Static(item["description"], classes="artifact-description", markup=False))
        if children:
            await self.mount(*children)

    @on(Button.Pressed, ".artifact-button")
    def open_artifact(self, event):
        event.stop()
        if event.button.id in self.artifacts:
            self.app.push_screen(ArtifactPreview(*self.artifacts[event.button.id]))
