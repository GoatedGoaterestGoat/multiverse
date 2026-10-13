"""`mv2 compaction`: show or set an agent's compaction: scheduler, summary trees, recent errors.

The CLI runs `run`; the plugin's settings screens (screens.py) call
`overview`, `save_settings`, `agent_view` and `set_scheduler` directly.
"""
import json
import sys

from multiverse.plugins.api import files

from . import support
from .engine import config as settings_file
from .engine.build import Build, Cancelled, Reporter
from .engine.config import IMAGE_MODES, SCHEDULERS
from .engine.pipeline import conversation, plan as planned
from .engine.schedule import allocate, frontier_for, frontier_tokens, needs_tree
from .engine.store import Store

SAVED = object()  # agent_view previews the agent's saved scheduler


def configure(parser):
    parser.add_argument('agent')
    parser.add_argument('--scheduler', choices=list(SCHEDULERS),
                        help="Give the agent its own scheduler (default: the universe's)")
    parser.add_argument('--budget', type=int, help='Tokens the compacted conversation may keep (prompt text always stays)')
    parser.add_argument('--k', type=int, help='last_k_raw: how many recent turns stay whole')
    parser.add_argument('--recency-ratio', type=float, help='recency_boosted: weight of the newest turn')
    parser.add_argument('--recency-half-life', type=float, help='recency_boosted: turns until the weight halves')
    parser.add_argument('--image-mode', choices=IMAGE_MODES, help='Which prompt images a compaction keeps')
    parser.add_argument('--use-default', action='store_true', help="Return the agent to the universe's scheduler")
    parser.add_argument('--plan', action='store_true', help='Show how each turn would be compacted now')
    parser.add_argument('--build', action='store_true', help='Build the summary trees a compaction now needs')


def _changed_schedule(agent, scheduler, budget, k, recency_ratio, recency_half_life, image_mode):
    current, _ = settings_file.schedule_for(agent.directory, agent.settings)
    return settings_file.override(current, {'scheduler': scheduler, 'budget': budget, 'k': k, 'recency_ratio': recency_ratio,
                                            'recency_half_life': recency_half_life, 'image_mode': image_mode})


def _stderr(event):
    if event.get('event') == 'error':
        retry = f" (retry {event['attempt']}/{event['attempts']})" if event.get('retrying') else ''
        print(f"error: turn {(event.get('turn') or '')[:8]}: {event['kind']}: {event['message']}{retry}", file=sys.stderr)
    elif event.get('event') == 'progress' and event.get('phase') == 'built':
        print(f"built {event['turns_done']}/{event['turns']} turn(s)", file=sys.stderr)
    elif event.get('event') == 'log':
        print(event['message'], file=sys.stderr)


def find_agent(root, agent):
    """The named agent's compaction context."""
    return support.open_agent(root, agent)


def run(root=None, *, agent, scheduler=None, budget=None, k=None, recency_ratio=None, recency_half_life=None,
        image_mode=None, use_default=False, plan=False, build=False):
    root = support.MV.universe(root).path
    target = find_agent(root, agent)
    changing = any(value is not None for value in (scheduler, budget, k, recency_ratio, recency_half_life, image_mode))
    if use_default and changing:
        raise ValueError('Use either --use-default or scheduler options, not both')
    if use_default:
        settings_file.save_schedule(target.directory, None)
    elif changing:
        settings_file.save_schedule(target.directory, _changed_schedule(target, scheduler, budget, k,
                                                                        recency_ratio, recency_half_life, image_mode))
    schedule, origin = target.schedule()
    store = target.store
    status = store.status()
    result = {'agent': target.directory.name, 'harness': target.harness, 'enabled': target.settings.enabled,
              'unavailable': target.source.unavailable(),
              'scheduler': schedule.settings(), 'scheduler_from': origin,
              'builder': settings_file.describe(target.settings)['builder'], 'trees_cached': store.tree_count(),
              'activity': status.get('activity'), 'failed_turns': status.get('failures', {}),
              'recent_errors': status.get('errors', [])[-5:]}
    if not (plan or build):
        return result
    every = target.source.turns()
    turns = conversation(every)
    rows = planned(turns, schedule, store)
    if build:
        missing = [turn for turn, row in zip(turns, rows) if needs_tree(turn, row['share']) and turn.root is None]
        try:
            failures = Build(target.source, store, target.settings.builder, Reporter(store, _stderr)).turns(missing)
        except Cancelled:
            failures = {'*': 'cancelled'}
        result['built'] = len(missing) - len(failures)
        result['build_failures'] = failures
        rows = planned(turns, schedule, store)
    if plan:
        result['plan'] = rows
        result['turns'] = len(rows)
        result['dropped_turns'] = [{'turn': turn.id, 'reason': turn.dropped} for turn in every if turn.dropped]
        result['trees_missing'] = sum(1 for row in rows if row['needs_tree'] and not row['has_tree'])
    return result


def _harnesses(root):
    """This universe's harnesses that support compaction: {name: family}."""
    found = {}
    for manifest in sorted((root / 'harnesses').glob('*/harness.json')):
        try:
            family = support.MV.harness_info(root, manifest.parent.name).family
        except (OSError, ValueError, KeyError):
            continue
        if support.builder_support(family) is not None:
            found[manifest.parent.name] = family
    return found


def _defaults(family):
    found = support.builder_support(family) if family else None
    return found[0] if found else {}


def _activity(store):
    activity = store.status().get('activity')
    if activity:
        return activity.get('phase') or 'busy'
    return 'building trees' if store.worker_holder() is not None else None


def overview(root=None):
    """The universe's compaction settings as written and in effect, its harnesses and its agents."""
    root = support.MV.universe(root).path
    path = settings_file.path(root)
    written, problem = {}, None
    if path.exists():
        try:
            written = json.loads(path.read_text())
            if not isinstance(written, dict):
                written, problem = {}, f'{path}: not a JSON object'
        except (OSError, ValueError) as error:
            problem = f'{path}: {error}'
    try:
        loaded = settings_file.parse(written if path.exists() else settings_file.blank(path))
    except ValueError as error:
        loaded, problem = settings_file.Config(), problem or f'{path}: {error}'
    families = _harnesses(root)
    shared = loaded.for_harness('')
    rows = []
    for name in sorted(set(families) | set(loaded.harnesses)):
        family, defaults = families.get(name), _defaults(families.get(name))
        settings = loaded.for_harness(name, defaults)
        state = support.state(root, name, family, loaded) if family is not None and not problem else (None, None)
        rows.append({'name': name, 'supported': family is not None, 'enabled': settings.enabled,
                     'default': settings.default.settings(), 'builder': settings_file.describe(settings)['builder'],
                     'defaults': defaults, 'options': [key for key in defaults if key not in settings_file.BUILDER],
                     'problem': state[1]})
    members = []
    for item in support.MV.universe(root).agents():
        if item.harness not in families:
            continue
        directory = item.directory
        settings = loaded.for_harness(item.harness, _defaults(families[item.harness]))
        try:
            schedule, origin = settings_file.schedule_for(directory, settings)
        except ValueError:
            schedule, origin = settings.default, 'agent'
        store, status = Store(directory), Store(directory).status()
        errors = status.get('errors', [])
        members.append({'id': item.id, 'name': item.name, 'harness': item.harness, 'live': item.live,
                        'scheduler': schedule.settings(), 'scheduler_from': origin, 'trees_cached': store.tree_count(),
                        'activity': _activity(store), 'failed_turns': len(status.get('failures', {})),
                        'last_error': errors[-1].get('message') if errors else None})
    return {'universe': str(root), 'path': str(path), 'exists': path.exists(), 'schema': loaded.schema,
            'problem': problem, 'file': written,
            'enabled': shared.enabled, 'default': shared.default.settings(), 'harnesses': rows, 'agents': members,
            'schedulers': {name: list(fields) for name, fields in SCHEDULERS.items()}, 'image_modes': list(IMAGE_MODES)}


def save_settings(root=None, value=None):
    """Write the settings file once the whole file and every harness's own checks accept it; returns the new overview.

    It goes where the universe's settings are (settings_file.path): an unmigrated
    compaction.json stays one; plugin-settings/compaction.json is schema 2.
    """
    root = support.MV.universe(root).path
    path = settings_file.path(root)
    if not settings_file.legacy(path) and isinstance(value, dict) and 'schema' not in value:
        value = {'schema': 2, **value}
    loaded = settings_file.parse(value)
    if not settings_file.legacy(path) and loaded.schema != 2:
        raise ValueError(f'{path} holds schema 2 settings')
    for name, family in _harnesses(root).items():
        problem = support.state(root, name, family, loaded)[1]
        if problem:
            raise ValueError(problem)
    files.write_json(path, value)
    return overview(root)


def agent_view(root=None, *, agent, scheduler=SAVED, preview=True):
    """One agent's compaction: its status, its scheduler, and the plan `scheduler` would make (nothing is saved).

    `scheduler` is SAVED for the agent's own setting, None for the default it
    would follow, or scheduler settings to try. `failures` says why each turn
    whose build is still failed failed, as 'turn <n>: <reason>'.
    """
    root = support.MV.universe(root).path
    target = find_agent(root, agent)
    written, origin = settings_file.schedule_for(target.directory, target.settings)
    if scheduler is SAVED:
        trying = written
    elif scheduler is None:
        trying = target.settings.default
    else:
        trying = settings_file.parse_schedule(scheduler)
    store, status = target.store, target.store.status()
    try:
        name = files.read_json(target.directory / 'properties.json').get('name')
    except (OSError, ValueError):
        name = None
    result = {'agent': target.directory.name, 'name': name, 'harness': target.harness, 'enabled': target.settings.enabled,
              'unavailable': target.source.unavailable(), 'scheduler': written.settings(), 'scheduler_from': origin,
              'default': target.settings.default.settings(), 'trees_cached': store.tree_count(),
              'activity': _activity(store), 'failed_turns': len(status.get('failures', {})),
              'recent_errors': status.get('errors', [])[-5:]}
    failed = status.get('failures', {})
    if not preview:
        # Numbering failed turns needs the turns; read them only when some failed.
        result['failures'] = _failures(failed, conversation(target.source.turns()) if failed else [])
        return result
    schedule = target.effective(trying)
    every = target.source.turns()
    turns = conversation(every)
    result['failures'] = _failures(failed, turns)
    rows = planned(turns, schedule, store)
    shares, images = allocate(turns, schedule)
    before = after = 0
    partial = False
    for turn in turns:
        prompt = turn.prompt.tokens if turn.prompt else 0
        before += prompt + turn.tokens + turn.omitted
        after += prompt - (turn.prompt.images if turn.prompt and turn.id not in images else 0)
        if turn.root is not None or not needs_tree(turn, shares[turn.id]):
            after += frontier_tokens(frontier_for(turn, shares[turn.id]))
        else:
            partial = True
    result['plan'] = rows
    result['summary'] = {'turns': len(turns), 'summarized': sum(1 for row in rows if not row['raw']),
                         'trees_missing': sum(1 for row in rows if row['needs_tree'] and not row['has_tree']),
                         'dropped_turns': len(every) - len(turns), 'tokens_before': before, 'tokens_after': after,
                         'after_partial': partial}
    return result


def _failures(failed: dict, turns) -> list[str]:
    """Each turn whose build is still failed, as 'turn <n>: <reason>', numbered as the plan numbers turns.

    A failed turn no longer in the conversation is named by its id instead.
    """
    position = {turn.id: index for index, turn in enumerate(turns, 1)}
    ordered = sorted(failed.items(), key=lambda item: (position.get(item[0], len(position) + 1), item[0]))
    return [f"turn {position[turn_id] if turn_id in position else turn_id[:8]}: {(entry or {}).get('reason') or 'failed'}"
            for turn_id, entry in ordered]


def set_scheduler(root=None, *, agent, scheduler):
    """Give the agent its own scheduler, or with None return it to the default; returns agent_view without a plan."""
    root = support.MV.universe(root).path
    target = find_agent(root, agent)
    settings_file.save_schedule(target.directory, None if scheduler is None else settings_file.parse_schedule(scheduler))
    return agent_view(root, agent=agent, preview=False)

