"""Compaction settings: the universe's `compaction.json` and each agent's own scheduler.

A universe or subverse opts in with `compaction.json` at its root:

    {"schema": 1,
     "enabled": false,
     "default": {"scheduler": "last_k_raw", "k": 2, "budget": 0, "image_mode": "budget_fit"},
     "harnesses": {
       "claude": {
         "enabled": true,
         "builder": {"model": "sonnet", "fallback_model": "claude-sonnet-4-6", "effort": "medium",
                     "claude_max_retries": 3,
                     "background": true, "concurrency": 1, "turn_retries": 3,
                     "rate_limit": {"retries": 10, "wait_seconds": {"first": 20, "multiplier": 2, "max": 300}}}}}}

`enabled` and the `default` scheduler are shared by every harness, and
`harnesses.<name>` (by its directory under `harnesses/`) may replace either.
Tree building belongs to one harness, so `builder` exists only under
`harnesses.<name>`: its model, fallback model and effort name that harness's
own, and any field not listed above is an option of that harness (Claude's
`claude_max_retries`). The harness supplies their defaults and checks them
(CompactionSource.builder_defaults and check_builder); the other fields mean
the same for every harness. Every field is optional; a missing file means
compaction is off everywhere. An agent may have its own scheduler in
`state/agents/<id>/compaction/config.json`; without one it follows its
harness's default as it is now.
"""
from dataclasses import dataclass, field, replace
import json
from pathlib import Path

from ..store import write

FILE = 'compaction.json'
# Each scheduler and the parameters it takes besides `budget`.
SCHEDULERS = {'last_k_raw': ('k',), 'proportional': (), 'uniform': (), 'recency_boosted': ('recency_ratio', 'recency_half_life')}
IMAGE_MODES = ('budget_fit', 'keep_all', 'drop_all')
SHARED = ('enabled', 'default')
SECTION = ('enabled', 'default', 'builder')
# Builder fields every harness understands; any other field is the harness's own option.
BUILDER = ('model', 'fallback_model', 'effort', 'background', 'concurrency', 'turn_retries', 'rate_limit')


@dataclass(frozen=True)
class Schedule:
    """How a budget is shared across turns, as Multiverse v1 shares it.

    `budget` covers the whole compacted conversation. Prompt text is always
    kept and comes off the top; prompt images are kept as `image_mode` says
    (`budget_fit`: newest first while they fit half of what is left,
    `keep_all`, or `drop_all`); the scheduler shares the rest among the turns'
    assistant content. `last_k_raw` keeps the last `k` turns whole and shares
    what is left among the earlier ones.
    """
    scheduler: str = 'last_k_raw'
    budget: int = 0
    k: int = 2
    recency_ratio: float = 3.0
    recency_half_life: float = 10.0
    image_mode: str = 'budget_fit'

    def settings(self):
        """Only the fields this scheduler uses, as they are written in a file."""
        values = {'scheduler': self.scheduler, 'budget': self.budget}
        values.update({name: getattr(self, name) for name in SCHEDULERS[self.scheduler]})
        if self.image_mode != 'budget_fit':
            values['image_mode'] = self.image_mode
        return values


@dataclass(frozen=True)
class RateLimit:
    """Waits between retries of one merge call that was rate limited or overloaded."""
    retries: int = 10
    first: float = 20.0
    multiplier: float = 2.0
    maximum: float = 300.0

    def wait(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait before retry number `attempt` (1-based)."""
        delay = min(self.first * self.multiplier ** (attempt - 1), self.maximum)
        return min(max(delay, retry_after or 0.0), self.maximum)


@dataclass(frozen=True)
class Builder:
    """One harness's tree building.

    `model`, `fallback_model` (takes over a turn that `model` still refuses
    after its retries; None fails the turn instead) and `effort` name that
    harness's own; `options` are its other settings, as written. The harness
    supplies their defaults and checks them.
    """
    model: str | None = None
    fallback_model: str | None = None
    effort: str | None = None
    options: dict = field(default_factory=dict)
    background: bool = True
    concurrency: int = 1
    turn_retries: int = 3
    rate_limit: RateLimit = field(default_factory=RateLimit)


@dataclass(frozen=True)
class Settings:
    """What one harness's sessions use."""
    enabled: bool = False
    default: Schedule = field(default_factory=Schedule)
    builder: Builder = field(default_factory=Builder)


@dataclass(frozen=True)
class Config:
    """The whole file: shared settings and each harness's own, as written."""
    shared: dict = field(default_factory=dict)
    harnesses: dict = field(default_factory=dict)

    def for_harness(self, name: str, defaults: dict | None = None) -> Settings:
        """One harness's settings; `defaults` are its builder defaults (CompactionSource.builder_defaults)."""
        override = self.harnesses.get(name, {})
        merged = {**self.shared, **{key: override[key] for key in SHARED if key in override}}
        merged['builder'] = _merge(defaults or {}, override.get('builder', {}))
        return _settings(merged, f'harnesses.{name}' if name in self.harnesses else FILE)


def _merge(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = _merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return result


def _fields(value, allowed, where):
    if not isinstance(value, dict):
        raise ValueError(f'{where} must be an object')
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f'Unknown {where} fields: {sorted(unknown)}')
    return value


def _integer(value, where, low=0, high=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        bound = f'from {low} to {high}' if high is not None else f'of at least {low}'
        raise ValueError(f'{where} must be a whole number {bound}')
    return value


def _number(value, where, low=0.0, strict=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < low or (strict and value == low):
        raise ValueError(f'{where} must be a number {"above" if strict else "of at least"} {low:g}')
    return float(value)


def parse_schedule(value, where='scheduler settings') -> Schedule:
    value = _fields(value, ('scheduler', 'budget', 'k', 'recency_ratio', 'recency_half_life', 'image_mode'), where)
    name = value.get('scheduler', 'last_k_raw')
    if name not in SCHEDULERS:
        raise ValueError(f'Unknown scheduler {name!r}; choose one of {", ".join(SCHEDULERS)}')
    extra = set(value) - {'scheduler', 'budget', 'image_mode', *SCHEDULERS[name]}
    if extra:
        raise ValueError(f'{name} does not take {", ".join(sorted(extra))}')
    schedule = Schedule(name, _integer(value.get('budget', 0), 'budget'))
    if 'k' in value:
        schedule = replace(schedule, k=_integer(value['k'], 'k'))
    if 'recency_ratio' in value:
        schedule = replace(schedule, recency_ratio=_number(value['recency_ratio'], 'recency_ratio', 1.0))
    if 'recency_half_life' in value:
        schedule = replace(schedule, recency_half_life=_number(value['recency_half_life'], 'recency_half_life', strict=True))
    if 'image_mode' in value:
        if value['image_mode'] not in IMAGE_MODES:
            raise ValueError(f'image_mode must be one of {", ".join(IMAGE_MODES)}')
        schedule = replace(schedule, image_mode=value['image_mode'])
    return schedule


def _name(value, where):
    """A model or effort name, or None."""
    if value is not None and (not isinstance(value, str) or not value or value.startswith('-') or '\0' in value):
        raise ValueError(f'{where} must be a name, or null')
    return value


def _settings(value, where) -> Settings:
    enabled = value.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError(f'{where}: enabled must be true or false')
    default = parse_schedule(value.get('default', {}), f'{where}: default')
    builder = value.get('builder', {})
    if not isinstance(builder, dict):
        raise ValueError(f'{where}: builder must be an object')
    background = builder.get('background', True)
    if not isinstance(background, bool):
        raise ValueError(f'{where}: builder.background must be true or false')
    limits = _fields(builder.get('rate_limit', {}), ('retries', 'wait_seconds'), f'{where}: builder.rate_limit')
    waits = _fields(limits.get('wait_seconds', {}), ('first', 'multiplier', 'max'), f'{where}: builder.rate_limit.wait_seconds')
    rate_limit = RateLimit(
        _integer(limits.get('retries', 10), f'{where}: rate_limit.retries'),
        _number(waits.get('first', 20), f'{where}: wait_seconds.first', strict=True),
        _number(waits.get('multiplier', 2), f'{where}: wait_seconds.multiplier', 1.0),
        _number(waits.get('max', 300), f'{where}: wait_seconds.max', strict=True))
    if rate_limit.maximum < rate_limit.first:
        raise ValueError(f'{where}: wait_seconds.max must be at least wait_seconds.first')
    return Settings(enabled, default, Builder(
        model=_name(builder.get('model'), f'{where}: builder.model'),
        fallback_model=_name(builder.get('fallback_model'), f'{where}: builder.fallback_model'),
        effort=_name(builder.get('effort'), f'{where}: builder.effort'),
        options={key: item for key, item in builder.items() if key not in BUILDER},
        background=background,
        concurrency=_integer(builder.get('concurrency', 1), f'{where}: builder.concurrency', 1),
        turn_retries=_integer(builder.get('turn_retries', 3), f'{where}: builder.turn_retries'), rate_limit=rate_limit))


def parse(value) -> Config:
    if isinstance(value, dict) and 'builder' in value:
        raise ValueError('builder settings belong to one harness: move them to harnesses.<name>.builder')
    value = _fields(value, ('schema', *SHARED, 'harnesses'), FILE)
    if value.get('schema', 1) != 1:
        raise ValueError(f'Unsupported {FILE} schema')
    harnesses = value.get('harnesses', {})
    if not isinstance(harnesses, dict):
        raise ValueError('harnesses must map harness names to settings')
    for name, override in harnesses.items():
        _fields(override, SECTION, f'harnesses.{name}')
    config = Config({key: value[key] for key in SHARED if key in value}, dict(harnesses))
    # Report a mistake in any section now, not when that harness first compacts.
    _settings(config.shared, FILE)
    for name in harnesses:
        config.for_harness(name)
    return config


def load(root) -> Config:
    """The universe's settings; compaction is off everywhere when it has no compaction.json."""
    path = Path(root) / FILE
    if not path.exists():
        return Config()
    try:
        return parse(json.loads(path.read_text()))
    except ValueError as error:
        raise ValueError(f'{path}: {error}') from error


def agent_file(agent_dir) -> Path:
    return Path(agent_dir) / 'compaction' / 'config.json'


def schedule_for(agent_dir, settings: Settings) -> tuple[Schedule, str]:
    """The agent's scheduler and where it came from: `agent` or `universe`."""
    path = agent_file(agent_dir)
    if not path.exists():
        return settings.default, 'universe'
    try:
        return parse_schedule(json.loads(path.read_text()), str(path)), 'agent'
    except ValueError as error:
        raise ValueError(f'{path}: {error}') from error


def save_schedule(agent_dir, schedule: Schedule | None):
    """Give the agent its own scheduler, or with None return it to the universe default."""
    path = agent_file(agent_dir)
    if schedule is None:
        path.unlink(missing_ok=True)
    else:
        write(path, schedule.settings())


def describe(settings: Settings) -> dict:
    """Settings in effect, in the file's own shape."""
    builder, limits = settings.builder, settings.builder.rate_limit
    shown = {'model': builder.model, 'fallback_model': builder.fallback_model, 'effort': builder.effort,
             **builder.options, 'background': builder.background, 'concurrency': builder.concurrency,
             'turn_retries': builder.turn_retries,
             'rate_limit': {'retries': limits.retries,
                            'wait_seconds': {'first': limits.first, 'multiplier': limits.multiplier, 'max': limits.maximum}}}
    return {'enabled': settings.enabled, 'default': settings.default.settings(), 'builder': shown}
