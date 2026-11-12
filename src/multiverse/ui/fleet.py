"""Filling the agent table from a snapshot, and selecting rows in it."""
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual.css.query import NoMatches
from textual.widgets import DataTable, Input, Static

from ..display import clean, phase_label
from . import theme
from .columns import COLUMNS, COMPACT_WIDTH, MARKS, MODEL_WIDE, PHASE_WIDE, PluginColumn, lookup
from .drafts import HINT
from .tree import group_key, hierarchy
from .widgets import DraftMenu


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
        fields = ["name", *(key for key in self.snapshot["columns"] if key not in ("name", "universe") and key not in MARKS)]
        if available < COMPACT_WIDTH:
            fields = [key for key in fields if lookup(key).compact]
        if self.draft:
            fields = list(self.draft.columns)
        widths = {key: lookup(key).width for key in fields}
        if available < 68:
            widths["name"] = 23
        if self.draft and "harness" in widths:
            widths["harness"] = min(16, max(9, max((len(name) + 2 for name in self.draft.harnesses), default=9)))
        flexible = (next((key for key, column in COLUMNS.items() if column.flexible and key in fields), None)
                    or next((key for key in fields if key not in COLUMNS and lookup(key).flexible), None))
        # Core's Phase and Model (not a plugin column standing in for one) grow to fit what they show, a blocked
        # agent's dependency or a long model name and its effort, but only into the room the other columns leave,
        # the flexible one keeping its least width; what still doesn't fit is shortened by the column itself.
        growing = ((COLUMNS["phase"], PHASE_WIDE, lambda row: clean(phase_label(row) or "")),
                   (COLUMNS["model"], MODEL_WIDE, COLUMNS["model"].label))
        for column, bound, shown in growing:
            for key in [key for key in widths if lookup(key) is column]:
                room = (available - 2 * len(fields) - (6 if flexible else 0)
                        - sum(w for k, w in widths.items() if k not in (key, flexible)))
                widths[key] = max(widths[key], min(bound, room, max(
                    (cell_len(shown(row)) for row in projected if row["kind"] == "agent"), default=0)))
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
        moving = (("  ⇢ moving", f"italic {theme.YELLOW}"),)

        def cell(row, field):
            if row["kind"] == "draft":
                return self.draft_cell(row, field)
            if field == "name":  # its marks stay in view: a long name is shortened to the column's width instead
                return COLUMNS["name"].render(row, widths["name"],
                                              moving if self.moving and row["key"] == self.moving["key"] else ())
            column = lookup(field)
            if column is COLUMNS["phase"] or column is COLUMNS["model"] or isinstance(column, PluginColumn):
                return column.render(row, widths[field])
            return column.render(row)

        rendered = [(row["key"], [cell(row, field) for field in fields]) for row in projected]
        signature = (list(widths.items()), rendered)
        if signature != self._table_signature:
            with self.batch_update(), table.prevent(DataTable.RowHighlighted, DataTable.RowSelected):
                table.clear(columns=True)
                for field in fields:
                    table.add_column(Text(lookup(field).title), key=field, width=widths[field])
                for key, cells in rendered:
                    table.add_row(*cells, key=key)
                if keys:
                    table.move_cursor(row=keys.index(self.selected_key), animate=False)
            self._table_signature = signature
        self.query_one("#empty", Static).display = not keys
        self.query_one("#empty", Static).update("No matches. Esc clears search.")
        table.display = bool(keys)
        self.show_selection(self.selected_key)
        self.models_if_needed()
        self.call_after_refresh(self.position_draft_menu)
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

    def position_draft_menu(self):
        """Show the draft's choices for the active field under it: the harnesses, or the model list and efforts."""
        menu = self.query_one("#draft-menu", DraftMenu)
        draft = self.draft
        field = draft.active if draft else None
        options = draft.options() if field in ("harness", "model", "effort") else None
        menu.display = bool(options and not self.operating)
        if not menu.display:
            return
        labels = tuple(name if field == "harness" else draft.option_label(field, name) for name in options)
        if self._menu_options != (field, labels):
            menu.clear_options().add_options(Text(label) for label in labels)
            self._menu_options = (field, labels)
        value = draft.values[field]
        menu.highlighted = options.index(value) if value in options else None
        table = self.query_one("#agents", DataTable)
        columns = table.ordered_columns
        column = next(i for i, item in enumerate(columns) if item.key.value == ("harness" if field == "harness" else "summary"))
        x = table.content_region.x + sum(c.width + table.cell_padding * 2 for c in columns[:column]) - int(table.scroll_x)
        if field != "harness":  # under its own words in the "Model: … · Effort: …" cell
            cell = draft.column_cell("summary").plain
            x += table.cell_padding + (cell.index("Effort: ") + 8 if field == "effort" else 7)
        row_y = table.content_region.y + table.header_height + list(table.rows).index(draft.key) - int(table.scroll_y)
        if not table.content_region.y + table.header_height <= row_y < table.content_region.bottom:
            menu.display = False
            return
        width = min(48, max(16, max(len(label) for label in labels) + 4))
        height = min(10, len(labels) + 2)
        x = max(0, min(x, self.size.width - width))
        y = row_y + 1
        if y + height > self.size.height - 2:
            y = max(1, row_y - height)
        menu.styles.width, menu.styles.height = width, height
        menu.styles.offset = (x, y)
        menu.scroll_to_highlight()

    def update_selection(self, content):
        """Show the selected row's line; a row highlighted while the dashboard is shutting down has nowhere to show it."""
        try:
            self.query_one("#selection", Static).update(content)
        except NoMatches:
            pass

    def show_selection(self, key):
        row = self.visible_rows.get(key)
        self.selected_key = key
        self.selected = row if row and row["kind"] == "agent" else None
        if row and row["kind"] == "draft":
            if self.draft.active == "harness":
                value = clean(self.draft.values["harness"])
                hint = "↑↓ choose · Enter create · Tab model" if self.draft.harnesses else "No harnesses configured in this group"
                self.update_selection(f"Harness: {value} ▾ · {hint}")
                return
            if (hint := self.model_hint()) is not None:
                self.update_selection(hint)
                return
            label = Text(f"{self.draft.label()}: ", style=theme.TEAL)
            value = self.draft.cell(self.draft.active)
            # Keep the insertion point visible while editing a long value.
            budget = max(8, self.size.width - len(label.plain) - 4)
            start = max(0, self.draft.cursor - budget + 1)
            label.append_text(value[start:start + budget])
            self.update_selection(label)
            return
        if row:
            content = self.scope_trail(row)
            if row.get("focus"):
                content += " · ◉ " + clean(row["focus_line"])
            content += " · " + clean(row.get("summary") or row.get("description") or "n new agent · g new subverse")
            self.update_selection(content)
        else:
            self.update_selection("/ search · n new agent · g new subverse · ? keys")

    def model_hint(self):
        """The selection line for the draft's model or effort while it is a picker or being read, else None."""
        draft = self.draft
        if draft.active not in ("model", "effort") or not hasattr(draft, "choices"):
            return None
        fields = draft.fields
        after = draft.labels[fields[(fields.index(draft.active) + 1) % len(fields)]].lower()
        label = Text(f"{draft.label()}: ", style=theme.TEAL)
        if draft.active == "model" and draft.reading():
            return label + Text(f"reading {clean(draft.values['harness'])}'s models… · Tab {after}", style=HINT)
        found = draft.choices.get(draft.values["harness"])
        if (draft.active == "model" and draft.typed() and draft.values["model"]
                and (draft.model() is None or not draft.select_all)):
            return None  # being typed: the line shows it with the cursor
        if draft.options() is None:
            if isinstance(found, dict) and found.get("error"):  # typed, with why there's no list to pick from
                return label + Text(clean(draft.values["model"]) or "default") + Text(
                    f" · couldn't read its models: {clean(found['error'])}", style=HINT)
            return None
        label.append(draft.option_label(draft.active, draft.values[draft.active]) + " ▾")
        label.append(f" · ↑↓ choose{' or type a model ID' if draft.active == 'model' and draft.typed() else ''}"
                     f" · Enter create · Tab {after}", style=HINT)
        if draft.active == "model" and isinstance(found, dict):
            if found.get("narrowed"):
                label.append(" · narrowed in this universe's harness settings", style=HINT)
            for problem in found.get("problems") or ():
                label.append(f" · {clean(problem)}", style=theme.YELLOW)
        return label

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
