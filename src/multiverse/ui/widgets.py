"""The agent table and the small widgets around it; they route input to the app's actions."""
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, OptionList, Static


class HarnessMenu(OptionList, can_focus=False):
    """Mouse-selectable menu; keyboard input stays with the inline draft."""


class AgentTable(DataTable):
    BINDINGS = [
        Binding("enter", "app.open", "Open"),
        Binding("space", "app.fold", "Fold", show=False),
        Binding("left,h", "app.tree_left", "Fold / parent", show=False),
        Binding("right,l", "app.tree_right", "Expand / child", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    async def _on_click(self, event):
        if self.app.draft:
            if not self.app.operating:
                row = event.style.meta.get("row", -1)
                if not 0 <= row < self.row_count or self.ordered_rows[row].key.value != self.app.draft.key:
                    return
                column = event.style.meta.get("column", -1)
                if 0 <= column < len(self.ordered_columns):
                    field = str(self.ordered_columns[column].key.value)
                    self.app.draft.focus_column(field)
                    self.app.populate_table()
            return
        # Textual's default emits RowSelected only on an already selected cell.
        # Folder rows should fold on the first click; agent activation is double-click.
        await super()._on_click(event)
        row = event.style.meta.get("row", -1)
        if 0 <= row < self.row_count and not event.style.meta.get("out_of_bounds"):
            self.app.click_row(str(self.ordered_rows[row].key.value), event.chain)

    def on_key(self, event):
        if self.app.draft:
            event.stop()
            event.prevent_default()
            self.app.draft_key(event)
        elif event.key in ("up", "down", "left", "right", "j", "k", "h", "l", "home", "end", "pageup", "pagedown"):
            self.app.cancel_inspector_timer()

    def on_paste(self, event):
        if self.app.draft:
            event.stop()
            event.prevent_default()
            if not self.app.operating:
                self.app.draft.paste(event.text)
                self.app.populate_table()


class Fleet(Vertical):
    def on_resize(self):
        # Child resize occurs after layout: use the actual available column width.
        self.call_later(self.app.populate_table)


class Breadcrumb(Static):
    def on_click(self):
        self.app.action_back(from_mouse=True)
