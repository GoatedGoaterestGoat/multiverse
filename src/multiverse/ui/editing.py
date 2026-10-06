"""Changing the tree from the dashboard: new agents and subverses as inline drafts, moving and deleting."""
from pathlib import Path

from textual.widgets import DataTable, Input

from ..display import clean
from .drafts import AgentDraft, SubverseDraft
from .screens import Form
from .tree import agent_key, group_key



class TreeEditing:
    """MultiverseApp's actions for n, g, x and d. A draft or a pending move is a mode the app checks."""
    def action_spawn(self):
        if self.ready():
            root = self.target_scope()
            if not self.snapshot:
                return
            options = self.scope_harnesses(root)
            if not options:
                self.set_status(f"No harnesses configured in {self.scope_name(root)}.", error=True)
                return
            names = {a["name"] for a in self.snapshot["agents"] if Path(a["universe"]) == root}
            harness = next((name for name in ("codex", "claude") if name in options), options[0])
            self.start_draft(AgentDraft(root, self.selected_key, {"name": self.unused("worker", names), "harness": harness,
                                                                  "model": ""}, options),
                             f"New agent in {self.scope_name(root)} · Tab field · Enter create · Esc cancel")

    @staticmethod
    def unused(base, names):
        name, number = base, 2
        while name in names:
            name, number = f"{base}-{number}", number + 1
        return name

    def subverse_names(self, root):
        return {Path(g["path"]).name for g in self.snapshot["groups"] if g["parent"] == str(root)}

    def start_draft(self, draft, message):
        self.hide_inspector()
        self.query_one("#filter", Input).value = ""
        self.query_one("#filter").display = False
        self.draft = draft
        self.selected_key = draft.key
        scope = draft.scope
        while scope.is_relative_to(self.root):
            self.collapsed.discard(str(scope))
            if scope == self.root:
                break
            scope = scope.parent.parent
        self.set_status(message)
        self.populate_table()
        self.query_one("#agents", DataTable).focus()

    def draft_key(self, event):
        if self.operating:
            return
        if event.key == "escape":
            self.cancel_draft()
        elif event.key == "enter" and self.draft.active == "harness":
            if self.draft.values["harness"] in self.draft.harnesses:
                self.draft.focus("model")
                self.populate_table()
        elif event.key in ("enter", "ctrl+enter"):
            self.commit_draft()
        else:
            self.draft.edit(event)
            self.populate_table()

    def cancel_draft(self):
        if self.draft and not self.operating:
            self.selected_key = self.draft.previous_key
            self.draft = None
            self.set_status("")
            self.populate_table()
            self.query_one("#agents", DataTable).focus()

    def commit_draft(self):
        draft = self.draft
        if not draft or self.operating:
            return
        values = {key: value.strip() for key, value in draft.values.items()}
        if problem := draft.problem(values, self.snapshot):
            field, message = problem
            draft.focus(field)
            self.set_status(message, error=True)
            self.populate_table()
            return
        self.submit_operation(draft.verb, getattr(self.backend, draft.operation), draft.scope, values, after=self.created)

    def created(self, result):
        draft, self.draft = self.draft, None
        self.collapsed.discard(str(draft.scope))
        self.selected_key = draft.created_key(result)

    def action_subverse(self):
        if self.ready():
            root = self.target_scope()
            if not self.snapshot:
                return
            self.start_draft(SubverseDraft(root, self.selected_key, {"name": self.unused("subverse", self.subverse_names(root))}),
                             f"New subverse in {self.scope_name(root)} · Enter create · Esc cancel")

    def movable(self, row, verb):
        """Explain why a row cannot be moved or deleted; the backend rechecks on disk."""
        if row["kind"] == "group" and Path(row["path"]) == self.root:
            return (f"Go back to the parent universe to {verb} {clean(row['name'])}." if self.history
                    else f"The top-level universe cannot be {verb}d here.")
        running = row["alive"] if row["kind"] == "group" else int(row["live"] in ('alive', 'ending'))
        if running:
            what = f"{running} running agent{'s' if running != 1 else ''} inside" if row["kind"] == "group" else "it"
            return f"Stop {what} first (s), then {verb} {clean(row['name'])}."
        return None

    def action_cut(self):
        if self.moving:
            self.place()
            return
        row = self.visible_rows.get(self.selected_key)
        if not self.ready() or not row or row["kind"] not in ("agent", "group"):
            return
        if problem := self.movable(row, "move"):
            self.set_status(problem, error=True)
            return
        self.hide_inspector()
        self.moving = dict(row)
        self.set_status(f"Moving {clean(row['name'])} · choose a group · Enter or x places it · Esc cancels")
        self.populate_table()

    def cancel_move(self):
        self.moving = None
        self.set_status("")
        self.populate_table()

    def place(self):
        if not self.navigable():
            return
        moving, destination = self.moving, self.target_scope()
        if moving["kind"] == "agent":
            self.submit_operation("Move agent", self.backend.move_agent, Path(moving["universe"]), moving["id"],
                                  destination, after=lambda result: self.moved(agent_key(result), destination))
        else:
            self.submit_operation("Move subverse", self.backend.move_subverse, Path(moving["path"]), destination,
                                  after=lambda result: self.moved(group_key(Path(result["universe"])), destination))

    def moved(self, key, destination):
        self.moving = None
        self.collapsed.discard(str(destination))
        self.selected_key = key

    def action_remove(self):
        row = self.visible_rows.get(self.selected_key)
        if not self.ready() or not row or row["kind"] not in ("agent", "group"):
            return
        if problem := self.movable(row, "delete"):
            self.set_status(problem, error=True)
            return
        name = clean(row["name"])
        if row["kind"] == "agent":
            detail = f"{name} moves to this universe's trash with its inbox and launch history."
            callback, args, label = self.backend.remove_agent, (Path(row["universe"]), row["id"]), "Delete agent"
        else:
            count = row["total"]
            detail = (f"{name}, its subverses and {count} agent{'s' if count != 1 else ''} move to the parent universe's trash.")
            callback, args, label = self.backend.remove_subverse, (Path(row["path"]),), "Delete subverse"
        self.push_screen(Form(f"Delete {name}?", description=
                              f"{detail} mv2 trash lists it; mv2 trash --empty deletes permanently. "
                              "Ctrl+Enter deletes. Esc cancels."),
                         lambda values: self.submit_operation(label, callback, *args) if values is not None else None)
