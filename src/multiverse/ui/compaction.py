"""Dashboard screens for tree compaction: a universe's settings, and one agent's scheduler with a plan preview.

The screens own presentation only. Reading and writing go through the backend
(`compaction_overview`, `save_compaction`, `compaction_agent`,
`set_compaction_scheduler`), always off the event loop. The fields and save
rules come from ui/settings.py; this module knows which settings compaction has
and which scheduler parameters apply.
"""
import asyncio
import copy
from pathlib import Path

from textual import work
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Label, Static

from ..compaction import config
from ..display import clean, tokens
from ..store import read
from .settings import BOOL_OPTIONS, MISSING, Field, FieldScreen, convert, delete, field, lookup, put

GENERIC = config.describe(config.Settings())['builder']
SCHEDULER_NAMES = tuple(config.SCHEDULERS)
SAVED = "Compaction settings saved. Restart agents for a changed enabled setting to apply."  # the status line after a save
REFRESH = 3.0  # seconds between refreshes of what the build process changes


def scheduler_fields(group, base, data, defaults, mode=None):
    """The fields of one scheduler: `mode` is (path, option label for not set) when it may follow a default."""
    fields = []
    if mode is not None:
        path, follows = mode
        fields.append(field(f'{group}.mode', path, 'default scheduler', 'choice', {}, options=((None, follows), ('own', 'override universe settings')),
                            group=group, role='mode'))
        fields[-1].initial = 'own' if lookup(data, path) is not MISSING else None
    name = defaults.get('scheduler', 'last_k_raw')
    fields.append(field(f'{group}.scheduler', (*base, 'scheduler'), 'scheduler', 'choice', data,
                        options=((None, f'default: {name}'), *((item, item) for item in SCHEDULER_NAMES)), group=group, role='scheduler'))
    fields.append(field(f'{group}.budget', (*base, 'budget'), 'budget (tokens)', 'int', data, defaults.get('budget', 0),
                        group=group, role='budget'))
    fields.append(field(f'{group}.image_mode', (*base, 'image_mode'), 'image mode', 'choice', data,
                        options=((None, f"default: {defaults.get('image_mode', 'budget_fit')}"),
                                 *((item, item) for item in config.IMAGE_MODES)), group=group, role='image_mode'))
    fields.append(field(f'{group}.k', (*base, 'k'), 'k (last turns raw)', 'int', data, defaults.get('k', config.Schedule().k),
                        group=group, role='k'))
    fields.append(field(f'{group}.recency_ratio', (*base, 'recency_ratio'), 'recency ratio', 'float', data,
                        defaults.get('recency_ratio', config.Schedule().recency_ratio), group=group, role='recency_ratio'))
    fields.append(field(f'{group}.recency_half_life', (*base, 'recency_half_life'), 'recency half-life', 'float', data,
                        defaults.get('recency_half_life', config.Schedule().recency_half_life), group=group, role='recency_half_life'))
    return fields


def builder_fields(entry, data):
    """The tree builder fields of one harness overview entry."""
    name, defaults = entry['name'], {**GENERIC, **entry.get('defaults', {})}
    base = ('harnesses', name, 'builder')
    group = f'{name}.builder'

    def make(key, label, kind, path=None, options=()):
        path = path or (key,)
        return field(f'{name}.builder.{".".join(path)}', (*base, *path), label, kind, data,
                     lookup(defaults, path) if lookup(defaults, path) is not MISSING else '', options, group=group)

    fields = [make('model', 'model', 'text'), make('fallback_model', 'fallback (null: none)', 'nullable'),
              make('effort', 'effort', 'text')]
    for option in entry.get('options', ()):
        value = defaults.get(option)
        kind = 'choice' if isinstance(value, bool) else 'int' if isinstance(value, int) else 'float' if isinstance(value, float) else 'text'
        fields.append(make(option, option, kind, options=BOOL_OPTIONS if kind == 'choice' else ()))
    fields += [make('background', 'background', 'choice', options=BOOL_OPTIONS),
               make('concurrency', 'concurrency', 'int'), make('turn_retries', 'turn retries', 'int'),
               make('rate_limit.retries', 'rate limit retries', 'int', ('rate_limit', 'retries')),
               make('wait_first', 'wait first (s)', 'float', ('rate_limit', 'wait_seconds', 'first')),
               make('wait_multiplier', 'wait multiplier', 'float', ('rate_limit', 'wait_seconds', 'multiplier')),
               make('wait_max', 'wait max (s)', 'float', ('rate_limit', 'wait_seconds', 'max'))]
    return fields


def sections(overview):
    """[(title, fields)] for a universe's compaction.json, from the overview."""
    data = overview.get('file') or {}
    shared = [field('shared.enabled', ('enabled',), 'enabled', 'choice', data, options=((None, 'default: false'), *BOOL_OPTIONS))]
    shared += scheduler_fields('shared', ('default',), data, config.Schedule().settings())
    found = [('All harnesses', shared)]
    for entry in overview.get('harnesses', ()):
        name = entry['name']
        own = [field(f'{name}.enabled', ('harnesses', name, 'enabled'), 'enabled', 'choice', data,
                     options=((None, 'follow universe settings'), *BOOL_OPTIONS))]
        own += scheduler_fields(f'{name}.default', ('harnesses', name, 'default'), data, config.Schedule().settings(),
                                mode=(('harnesses', name, 'default'), 'follow universe settings'))
        own += builder_fields(entry, data)
        found.append((f'Harness {name}' + ('' if entry.get('supported', True) else ' (tree compaction not supported)'), own))
    return found


def visible(item, values):
    """Whether a field applies to what is chosen for its group."""
    if item.role == 'mode' or item.group is None:
        return True
    if f'{item.group}.mode' in values and values[f'{item.group}.mode'] is None:
        return False
    scheduler = values.get(f'{item.group}.scheduler') or 'last_k_raw'
    if item.role == 'k':
        return scheduler == 'last_k_raw'
    if item.role in ('recency_ratio', 'recency_half_life'):
        return scheduler == 'recency_boosted'
    return True


def apply_changes(file, fields, values):
    """`file` with only the edited fields changed; a cleared field's key is deleted."""
    new = copy.deepcopy(file or {})
    changed_schedulers = {}
    for item in fields:
        if values[item.key] == item.initial:
            continue
        if item.role == 'mode':
            if values[item.key] is None:
                delete(new, item.path)
            else:
                put(new, item.path, {})
            continue
        if f'{item.group}.mode' in values and values[f'{item.group}.mode'] is None:
            continue
        value = convert(item, values[item.key])
        if value is MISSING:
            delete(new, item.path)
        else:
            put(new, item.path, value)
        if item.role == 'scheduler':
            changed_schedulers[item.group] = item.path[:-1]
    # A scheduler takes only its own parameters: a changed one drops the others'.
    for base in changed_schedulers.values():
        found = lookup(new, base)
        if isinstance(found, dict):
            allowed = {'scheduler', 'budget', 'image_mode', *config.SCHEDULERS.get(found.get('scheduler', 'last_k_raw'), ())}
            for key in [key for key in found if key not in allowed]:
                del found[key]
    return new


def own_scheduler(fields, values, prefix):
    """The scheduler dict a group's visible, non-blank fields describe."""
    result = {}
    for item in fields:
        if item.group == prefix and item.role != 'mode' and visible(item, values):
            value = convert(item, values[item.key])
            if value is not MISSING:
                result[item.path[-1]] = value
    return result


class SchedulerScreen(FieldScreen):
    """A field screen whose scheduler fields show only for the scheduler chosen in their group."""
    def applies(self, item, values):
        return visible(item, values)


def universe_name(root):
    """The universe's own name from universe.json, else its directory's."""
    try:
        return str(read(Path(root) / "universe.json").get("name") or Path(root).name)
    except (OSError, ValueError, AttributeError):
        return Path(root).name


class CompactionScreen(SchedulerScreen):
    """A scope's compaction.json, section by section, and its agents."""

    def __init__(self, root, backend, name=""):
        super().__init__(root, backend)
        self.scope_name = name or universe_name(root)
        self.overview = None
        self.reloading = False

    def compose(self):
        with Vertical(id="compaction-dialog"):
            yield Label("Compaction · " + clean(self.scope_name), classes="detail-title", markup=False)
            yield Static("", id="compaction-problem", markup=False)
            yield VerticalScroll(Static("Reading…", id="compaction-loading"), id="compaction-body")
            yield Static("", classes="cerror", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#compaction-problem").display = False
        self.load()
        self.set_interval(REFRESH, self.tick)

    def tick(self):
        """Keep the agents table current while the screen is in front; edits in the fields stay."""
        if self.overview is not None and not self.saving and not self.reloading and self.app.screen is self:
            self.reload_agents()

    @work(group="compaction", exit_on_error=False)
    async def load(self):
        try:
            overview = await asyncio.to_thread(self.backend.compaction_overview, self.root)
        except Exception as error:
            self.query_one("#compaction-loading", Static).update("Compaction unavailable.")
            self.error(str(error))
            return
        await self.show(overview)

    async def show(self, overview):
        """Build the sections from an overview (again after an agent changed)."""
        self.overview = overview
        body = self.query_one("#compaction-body", VerticalScroll)
        await body.remove_children()
        self.fields, self.rows, self.widgets = [], {}, {}
        problem = overview.get('problem')
        self.query_one("#compaction-problem").display = bool(problem)
        self.query_one("#compaction-problem", Static).update(f"compaction.json cannot be used: {problem}" if problem else "")
        widgets = []
        for title, items in sections(overview):
            widgets.append(Static(title, classes="csection", markup=False))
            widgets += self.field_rows(items)
        for entry in overview.get('harnesses', ()):
            if entry.get('problem'):
                widgets.append(Static(f"{entry['name']}: {entry['problem']}", classes="cwarn", markup=False))
        widgets.append(Static("Agents · Enter to configure agent-specific compaction settings", classes="csection", markup=False))
        table = DataTable(id="compaction-agents", cursor_type="row", zebra_stripes=False)
        widgets.append(table)
        await body.mount(*widgets)
        table.add_columns("Agent", "Harness", "Scheduler", "Trees", "State")
        self.fill_agents()
        self.refresh_rows()

    def fill_agents(self):
        table = self.query_one("#compaction-agents", DataTable)
        row = table.cursor_row
        table.clear()
        for agent in self.overview.get('agents', ()):
            table.add_row(clean(agent['name']), clean(agent['harness']), scheduler_summary(agent),
                          str(agent.get('trees_cached', 0)), agent_state(agent), key=agent['id'])
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1), animate=False)

    def on_data_table_row_selected(self, event):
        agent = next((a for a in (self.overview or {}).get('agents', ()) if a['id'] == str(event.row_key.value)), None)
        if agent:
            self.app.push_screen(AgentScreen(self.root, self.backend, agent), self.after_agent)

    def after_agent(self, saved):
        if saved:
            self.reload_agents()

    @work(group="compaction-agents", exit_on_error=False)
    async def reload_agents(self):
        """Refresh the agents table after one changed; edits in the fields stay."""
        self.reloading = True
        try:
            overview = await asyncio.to_thread(self.backend.compaction_overview, self.root)
        except Exception as error:
            self.reloading = False
            self.error(str(error))
            return
        self.reloading = False
        self.overview = {**self.overview, 'agents': overview.get('agents', [])}
        self.fill_agents()

    def action_save(self):
        if self.saving or self.overview is None:
            return
        try:
            file = apply_changes(self.overview.get('file') or {}, self.fields, self.values())
        except ValueError as error:
            self.error(str(error))
            return
        self.saving = True
        self.error("Saving…")
        self.persist(file)

    @work(group="compaction-save", exit_on_error=False)
    async def persist(self, file):
        try:
            await asyncio.to_thread(self.backend.save_compaction, self.root, file)
        except Exception as error:
            self.saving = False
            self.error(str(error))
            return
        self.saving = False
        self.dismiss(SAVED)


def scheduler_summary(agent):
    settings = agent.get('scheduler') or {}
    text = settings.get('scheduler', '?') + (f" {tokens(settings['budget'])}" if 'budget' in settings else '')
    return text if agent.get('scheduler_from') == 'agent' else 'default'


def agent_state(agent):
    parts = [clean(agent.get('activity') or 'idle')]
    if agent.get('failed_turns'):
        parts.append(f"{agent['failed_turns']} failed turns")
    return " · ".join(parts)


def share_text(value):
    """A turn's share of the budget: the allocator gives a token count."""
    return tokens(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else clean(value)


class AgentScreen(SchedulerScreen):
    """One agent's compaction: status, scheduler and a plan preview that follows the fields."""
    DEBOUNCE = 0.4

    def __init__(self, root, backend, agent):
        super().__init__(root, backend)
        self.agent = agent
        self.detail = None
        self.generation = 0
        self.previewing = self.dirty = False
        self.timer = None
        self.preview_error = False  # the error line shows a preview's problem, which a later preview may clear

    def compose(self):
        with Vertical(id="compaction-dialog"):
            yield Label(f"Compaction · {clean(self.agent['name'])} ({clean(self.agent['harness'])})", classes="detail-title", markup=False)
            with VerticalScroll(id="agent-top"):
                yield Static("Reading…", id="agent-status", markup=False)
                yield Static("", classes="csection", id="agent-scheduler-title", markup=False)
                yield Static("", id="agent-follows", markup=False)
            yield Static("PLAN", classes="csection", markup=False)
            yield Static("", id="plan-summary", markup=False)
            yield DataTable(id="plan", cursor_type="row", zebra_stripes=False)
            yield Static("", classes="cerror", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#plan", DataTable).add_columns("#", "Prompt", "Leaves", "Tokens", "Share", "Kept", "Tree")
        self.load()
        self.set_interval(REFRESH, self.tick)

    def tick(self):
        """Follow the build: trees, activity and the plan's built/missing marks, with the scheduler as currently edited."""
        if self.detail is not None and not self.saving and self.app.screen is self:
            self.start_preview()

    @work(group="compaction", exit_on_error=False)
    async def load(self):
        try:
            detail = await asyncio.to_thread(self.backend.compaction_agent, self.root, self.agent['id'])
        except Exception as error:
            self.query_one("#agent-status", Static).update("Compaction unavailable for this agent.")
            self.error(str(error))
            return
        await self.show(detail)

    async def show(self, detail):
        self.detail = detail
        scheduler = detail.get('scheduler') or {}
        default = detail.get('default') or {}
        self.show_status(detail)
        self.query_one("#agent-scheduler-title", Static).update("SCHEDULER")
        body = self.query_one("#agent-top", VerticalScroll)
        for row in list(self.rows.values()):
            await row.remove()
        self.fields, self.rows, self.widgets = [], {}, {}
        own = detail.get('scheduler_from') == 'agent'
        data = {'scheduler': scheduler}
        mode = Field('agent.mode', ('scheduler',), 'scheduler', 'choice', options=((None, 'follow harness settings'), ('own', 'override harness settings')),
                     initial='own' if own else None, group='agent', role='mode')
        rows = self.field_rows([mode, *scheduler_fields('agent', ('scheduler',), data, default)])
        await body.mount(*rows, before=self.query_one("#agent-follows"))
        self.follows(default)
        self.refresh_rows()
        self.fill_plan(detail)

    def show_status(self, detail):
        """Status lines; an error shows only while a turn's build is still failed (a retry that worked clears it)."""
        state = ['enabled' if detail.get('enabled') else 'off']
        if detail.get('unavailable'):
            state.append(clean(detail['unavailable']))
        state += [f"{detail.get('trees_cached', 0)} trees", clean(detail.get('activity') or 'idle')]
        failed = detail.get('failed_turns') or 0
        if failed:
            state.append(f"{failed} failed turns")
        lines = [" · ".join(state)]
        if failed:
            reasons = [clean(item) for item in (detail.get('failures') or [])]
            errors = reasons or [clean(item.get('message', item) if isinstance(item, dict) else item)
                                 for item in (detail.get('recent_errors') or [])[-3:]]
            lines += ["Error: " + item for item in errors[:3]]
        self.query_one("#agent-status", Static).update("\n".join(lines))

    def follows(self, default):
        text = "Following harness settings: " + ", ".join(f"{key} {value}" for key, value in default.items())
        self.query_one("#agent-follows", Static).update(text)
        self.refresh_rows()

    def refresh_rows(self):
        super().refresh_rows()
        mode = self.widgets.get('agent.mode')
        self.query_one("#agent-follows").display = mode is not None and mode.value is None

    def requested(self):
        """The scheduler to preview and save: None for the default, else a settings dict."""
        values = self.values()
        if values['agent.mode'] is None:
            return None
        return own_scheduler(self.fields, values, 'agent')

    def changed(self):
        self.generation += 1
        if self.timer:
            self.timer.stop()
        self.timer = self.set_timer(self.DEBOUNCE, self.start_preview)

    def start_preview(self):
        self.timer = None
        if self.previewing:
            self.dirty = True
            return
        try:
            scheduler = self.requested()
        except ValueError as error:
            if not self.saving:
                self.error(str(error))
                self.preview_error = True
            return
        self.previewing = True
        self.preview(scheduler, self.generation)

    @work(group="compaction-preview", exit_on_error=False)
    async def preview(self, scheduler, generation):
        try:
            detail = await asyncio.to_thread(self.backend.compaction_agent, self.root, self.agent['id'],
                                             scheduler=scheduler, preview=True)
            failure = None
        except Exception as error:
            detail, failure = None, error
        self.previewing = False
        if generation == self.generation and not self.saving:
            if failure is not None:
                self.error(str(failure))
                self.preview_error = True
            elif detail is not None:
                # Only a preview's own problem is cleared here; a save's error or "Saving…" stays.
                if self.preview_error:
                    self.error("")
                    self.preview_error = False
                self.detail = {**self.detail, **{k: detail[k] for k in ('trees_cached', 'activity', 'failed_turns',
                                                                          'recent_errors', 'failures', 'enabled', 'unavailable')
                                                 if k in detail}}
                self.show_status(self.detail)
                self.fill_plan(detail)
        if self.dirty:
            self.dirty = False
            self.start_preview()

    def fill_plan(self, detail):
        table = self.query_one("#plan", DataTable)
        table.clear()
        for position, turn in enumerate(detail.get('plan') or (), 1):
            kept = 'raw' if turn.get('raw') else 'summarized'
            tree = ('built' if turn.get('has_tree') else 'missing') if turn.get('needs_tree') else '—'
            table.add_row(str(position), clean(turn.get('prompt', ''))[:30], str(turn.get('leaves', '')),
                          tokens(turn['tokens']) if isinstance(turn.get('tokens'), (int, float)) else '—',
                          share_text(turn.get('share')), kept, tree)
        summary = detail.get('summary') or {}
        after = tokens(summary.get('tokens_after', 0)) + ('+' if summary.get('after_partial') else '')
        text = (f"{summary.get('turns', 0)} turns · {summary.get('summarized', 0)} summarized · "
                f"{summary.get('trees_missing', 0)} trees missing · {tokens(summary.get('tokens_before', 0))} → {after}"
                + (' (partial: trees missing)' if summary.get('after_partial') else ''))
        if summary.get('dropped_turns'):
            text += f" · {summary['dropped_turns']} turns dropped"
        self.query_one("#plan-summary", Static).update(text)

    def action_save(self):
        if self.saving or self.detail is None:
            return
        try:
            scheduler = self.requested()
        except ValueError as error:
            self.error(str(error))
            return
        self.saving = True
        self.preview_error = False
        self.error("Saving…")
        self.persist(scheduler)

    @work(group="compaction-save", exit_on_error=False)
    async def persist(self, scheduler):
        try:
            await asyncio.to_thread(self.backend.set_compaction_scheduler, self.root, self.agent['id'], scheduler)
        except Exception as error:
            self.saving = False
            self.error(str(error))
            return
        self.saving = False
        self.dismiss(True)
