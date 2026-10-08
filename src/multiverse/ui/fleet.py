"""Filling the agent table from a snapshot, and selecting rows in it."""
from pathlib import Path

from rich.text import Text
from textual.widgets import DataTable, Input, Static

from ..display import clean
from . import theme
from .columns import COLUMNS, COMPACT_WIDTH
from .tree import group_key, hierarchy
from .widgets import HarnessMenu


class FleetView:
    """MultiverseApp's table: rows from the snapshot, column widths, selection and the harness menu."""
    def populate_table(self):
        if not self.snapshot:
            return
        self.set_class(self.draft is not None, "drafting")
        search = self.query_one("#filter", Input).value
        data = self.snapshot
        if self.draft:
            data = self.draft.add_to(data)
        projected = hierarchy(data, self.collapsed, search)
        for row in projected:
            if self.draft and row["key"] == self.draft.key:
                row["kind"] = "draft"
        available = self.query_one("#fleet").content_size.width - 1
        fields = ["name", *(key for key in self.snapshot["columns"] if key not in ("name", "universe"))]
        if available < COMPACT_WIDTH:
            fields = [key for key in fields if COLUMNS[key].compact]
        if self.draft:
            fields = list(self.draft.columns)
        widths = {key: COLUMNS[key].width for key in fields}
        if available < 68:
            widths["name"] = 23
        if self.draft and "harness" in widths:
            widths["harness"] = min(16, max(9, max((len(name) + 2 for name in self.draft.harnesses), default=9)))
        flexible = next((key for key, column in COLUMNS.items() if column.flexible and key in fields), None)
        if flexible:
            widths[flexible] = max(6, available - 2 * len(fields) - sum(w for k, w in widths.items() if k != flexible))
        self.visible_rows = {row["key"]: row for row in projected}
        self.visible_agents = {key: row for key, row in self.visible_rows.items() if row["kind"] == "agent"}
        keys = list(self.visible_rows)
        if self.selected_key not in keys:
            # Prefer a surviving ancestor after folding/removal; otherwise first agent.
            prior = self.selected_key
            candidates = []
            if prior and prior.startswith("group:"):
                scope = Path(prior[6:])
            elif self.selected:
                scope = Path(self.selected["universe"])
            else:
                scope = None
            while scope and scope.is_relative_to(self.root):
                candidates.append(group_key(scope))
                if scope == self.root:
                    break
                scope = scope.parent.parent
            self.selected_key = next((key for key in candidates if key in keys),
                                     next(iter(self.visible_agents), keys[0] if keys else None))
        table = self.query_one("#agents", DataTable)
        rendered = [(row["key"], [self.draft_cell(row, field) if row["kind"] == "draft" else COLUMNS[field].render(row)
                                  for field in fields]) for row in projected]
        if self.moving:
            for key, cells in rendered:
                if key == self.moving["key"]:
                    cells[0].append("  ⇢ moving", style=f"italic {theme.YELLOW}")
        signature = (list(widths.items()), rendered)
        if signature != self._table_signature:
            with self.batch_update(), table.prevent(DataTable.RowHighlighted, DataTable.RowSelected):
                table.clear(columns=True)
                for field in fields:
                    table.add_column(COLUMNS[field].title, key=field, width=widths[field])
                for key, cells in rendered:
                    table.add_row(*cells, key=key)
                if keys:
                    table.move_cursor(row=keys.index(self.selected_key), animate=False)
            self._table_signature = signature
        self.query_one("#empty", Static).display = not keys
        self.query_one("#empty", Static).update("No matches. Esc clears search.")
        table.display = bool(keys)
        self.show_selection(self.selected_key)
        self.call_after_refresh(self.position_harness_menu)
        if self.inspector.row:
            self.inspector.update_row(self.visible_rows.get(self.inspector.row["key"]))

    def draft_cell(self, row, field):
        if field == "name":
            label = COLUMNS["name"].render(row)[:sum(len(guide) for guide, _color in row["guides"])]
            label.append("+ ", style=theme.TEAL)
            label.append_text(self.draft.cell("name"))
            return label
        return self.draft.column_cell(field)

    def scope_harnesses(self, scope):
        return next((g.get("harnesses", []) for g in self.snapshot["groups"] if g["path"] == str(scope)), [])

    def position_harness_menu(self):
        menu = self.query_one("#harness-menu", HarnessMenu)
        draft = self.draft
        menu.display = bool(draft and draft.active == "harness" and draft.harnesses and not self.operating)
        if not menu.display:
            return
        if self._harness_options != draft.harnesses:
            menu.clear_options().add_options(Text(name) for name in draft.harnesses)
            self._harness_options = draft.harnesses
        value = draft.values["harness"]
        menu.highlighted = draft.harnesses.index(value) if value in draft.harnesses else None
        table = self.query_one("#agents", DataTable)
        columns = table.ordered_columns
        column = next(i for i, item in enumerate(columns) if item.key.value == "harness")
        x = table.content_region.x + sum(c.width + table.cell_padding * 2 for c in columns[:column]) - int(table.scroll_x)
        row_y = table.content_region.y + table.header_height + list(table.rows).index(draft.key) - int(table.scroll_y)
        if not table.content_region.y + table.header_height <= row_y < table.content_region.bottom:
            menu.display = False
            return
        width = min(32, max(16, max(len(name) for name in draft.harnesses) + 4))
        height = min(8, len(draft.harnesses) + 2)
        x = max(0, min(x, self.size.width - width))
        y = row_y + 1
        if y + height > self.size.height - 2:
            y = max(1, row_y - height)
        menu.styles.width, menu.styles.height = width, height
        menu.styles.offset = (x, y)
        menu.scroll_to_highlight()

    def show_selection(self, key):
        row = self.visible_rows.get(key)
        self.selected_key = key
        self.selected = row if row and row["kind"] == "agent" else None
        if row and row["kind"] == "draft":
            if self.draft.active == "harness":
                value = clean(self.draft.values["harness"])
                hint = "↑↓ choose · Enter confirm · Tab next" if self.draft.harnesses else "No harnesses configured in this group"
                self.query_one("#selection", Static).update(f"Harness: {value} ▾ · {hint}")
                return
            label = Text(f"{self.draft.label()}: ", style=theme.TEAL)
            value = self.draft.cell(self.draft.active)
            # Keep the insertion point visible while editing a long value.
            budget = max(8, self.size.width - len(label.plain) - 4)
            start = max(0, self.draft.cursor - budget + 1)
            label.append_text(value[start:start + budget])
            self.query_one("#selection", Static).update(label)
            return
        if row:
            content = self.scope_trail(row)
            content += " · " + clean(row.get("summary") or row.get("description") or "n new agent · g new subverse")
            self.query_one("#selection", Static).update(content)
        else:
            self.query_one("#selection", Static).update("/ search · n new agent · g new subverse · ? keys")

    def scope_trail(self, row):
        path = Path(row["path"] if row["kind"] == "group" else row["universe"])
        groups = {g["path"]: g["name"] for g in self.snapshot["groups"]}
        parts = []
        while path.is_relative_to(self.root):
            parts.append(clean(groups.get(str(path), path.name)))
            if path == self.root:
                break
            path = path.parent.parent
        return " / ".join(reversed(parts))

    def click_row(self, key, chain):
        if not self.navigable():
            return
        self.show_selection(key)
        if self.moving:
            return
        row = self.visible_rows.get(key)
        if row and row["kind"] == "group":
            self.enter_scope(Path(row["path"])) if chain == 2 else self.action_fold()
        elif chain == 2:
            self.action_attach()

    def select_key(self, key):
        keys = list(self.visible_rows)
        if key in keys:
            self.query_one("#agents", DataTable).move_cursor(row=keys.index(key), animate=False)
            self.show_selection(key)

    def select_cursor(self):
        """Select the row under the table cursor, if the table has focus.

        RowHighlighted is queued. A fast arrow+Enter can arrive before it,
        so activation must use the actual cursor rather than its observer.
        """
        table = self.query_one("#agents", DataTable)
        if self.focused is not table or not 0 <= table.cursor_row < table.row_count:
            return False
        self.show_selection(str(table.ordered_rows[table.cursor_row].key.value))
        return True
