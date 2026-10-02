"""Show or set an agent's compaction: scheduler, summary trees, recent errors."""
import sys

from .. import universe
from ..compaction import config as settings_file
from ..compaction.build import Build, Cancelled, Reporter
from ..compaction.config import IMAGE_MODES, SCHEDULERS
from ..compaction.pipeline import conversation, open_agent, plan as planned
from ..compaction.schedule import needs_tree


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
    name = scheduler or current.scheduler
    values = current.settings() if name == current.scheduler else {'scheduler': name, 'budget': current.budget}
    if current.image_mode != 'budget_fit':
        values['image_mode'] = current.image_mode
    for key, value in (('budget', budget), ('k', k), ('recency_ratio', recency_ratio),
                       ('recency_half_life', recency_half_life), ('image_mode', image_mode)):
        if value is not None:
            values[key] = value
    return settings_file.parse_schedule(values)


def _stderr(event):
    if event.get('event') == 'error':
        retry = f" (retry {event['attempt']}/{event['attempts']})" if event.get('retrying') else ''
        print(f"error: turn {(event.get('turn') or '')[:8]}: {event['kind']}: {event['message']}{retry}", file=sys.stderr)
    elif event.get('event') == 'progress' and event.get('phase') == 'built':
        print(f"built {event['turns_done']}/{event['turns']} turn(s)", file=sys.stderr)
    elif event.get('event') == 'log':
        print(event['message'], file=sys.stderr)


def run(root=None, *, agent, scheduler=None, budget=None, k=None, recency_ratio=None, recency_half_life=None,
        image_mode=None, use_default=False, plan=False, build=False):
    root = universe.resolve(root)
    target = open_agent(root, agent)
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


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
