"""Compaction settings: the universe's `compaction.json` and each agent's own scheduler.

A universe or subverse opts in with `compaction.json` at its root. The top
level holds settings shared by every harness; `harnesses.<name>` overrides them
for one harness, by the name of its directory under `harnesses/`:

    {"schema": 1,
     "enabled": false,
     "default": {"scheduler": "last_k_raw", "k": 2, "budget": 0},
     "builder": {"background": true, "model": "sonnet", "effort": "medium",
                 "concurrency": 1, "turn_retries": 3,
                 "rate_limit": {"claude_max_retries": 3, "retries": 10,
                                "wait_seconds": {"first": 20, "multiplier": 2, "max": 300}}},
     "harnesses": {"claude": {"enabled": true}}}

An override replaces `enabled` and the `default` scheduler, and merges into
`builder` field by field. Every field is optional; a missing file means
compaction is off everywhere. An agent may have its own scheduler in
`state/agents/<id>/compaction/config.json`; without one it follows its
harness's default as it is now.
"""
from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path

from ..store import write

FILE = 'compaction.json'
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# Each scheduler and the parameters it takes besides `budget`.
SCHEDULERS = {'last_k_raw': ('k',), 'proportional': (), 'uniform': (), 'recency_boosted': ('recency_ratio', 'recency_half_life')}
SECTION = ('enabled', 'default', 'builder')


@dataclass(frozen=True)
class Schedule:
    """How a budget is shared across turns; `budget` covers assistant content only.

    Prompts are always kept verbatim and are not counted. `last_k_raw` keeps the
    last `k` turns whole and shares `budget` among the earlier ones.
    """
    scheduler: str = 'last_k_raw'
    budget: int = 0
    k: int = 2
    recency_ratio: float = 3.0
    recency_half_life: float = 10.0

    def settings(self):
        """Only the fields this scheduler uses, as they are written in a file."""
        values = {'scheduler': self.scheduler, 'budget': self.budget}
        values.update({name: getattr(self, name) for name in SCHEDULERS[self.scheduler]})
        return values


@dataclass(frozen=True)
class RateLimit:
    """Waits between retries of one merge call that was rate limited or overloaded.

    `claude_max_retries` is passed to each Claude call (CLAUDE_CODE_MAX_RETRIES)
    for its own quick retries before ours.
    """
    claude_max_retries: int = 3
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
    background: bool = True
    model: str = 'sonnet'
    effort: str = 'medium'
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
    """The whole file: shared settings and per-harness overrides, as written."""
    shared: dict = field(default_factory=dict)
    harnesses: dict = field(default_factory=dict)

    def for_harness(self, name: str) -> Settings:
        override = self.harnesses.get(name, {})
        merged = {**self.shared, **{key: override[key] for key in ('enabled', 'default') if key in override}}
        merged['builder'] = _merge(self.shared.get('builder', {}), override.get('builder', {}))
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
    value = _fields(value, ('scheduler', 'budget', 'k', 'recency_ratio', 'recency_half_life'), where)
    name = value.get('scheduler', 'last_k_raw')
    if name not in SCHEDULERS:
        raise ValueError(f'Unknown scheduler {name!r}; choose one of {", ".join(SCHEDULERS)}')
    extra = set(value) - {'scheduler', 'budget', *SCHEDULERS[name]}
    if extra:
        raise ValueError(f'{name} does not take {", ".join(sorted(extra))}')
    schedule = Schedule(name, _integer(value.get('budget', 0), 'budget'))
    if 'k' in value:
        schedule = replace(schedule, k=_integer(value['k'], 'k'))
    if 'recency_ratio' in value:
        schedule = replace(schedule, recency_ratio=_number(value['recency_ratio'], 'recency_ratio', 1.0))
    if 'recency_half_life' in value:
        schedule = replace(schedule, recency_half_life=_number(value['recency_half_life'], 'recency_half_life', strict=True))
    return schedule


def _settings(value, where) -> Settings:
    enabled = value.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError(f'{where}: enabled must be true or false')
    default = parse_schedule(value.get('default', {}), f'{where}: default')
    builder = _fields(value.get('builder', {}),
                      ('background', 'model', 'effort', 'concurrency', 'turn_retries', 'rate_limit'), f'{where}: builder')
    background = builder.get('background', True)
    if not isinstance(background, bool):
        raise ValueError(f'{where}: builder.background must be true or false')
    model = builder.get('model', 'sonnet')
    if not isinstance(model, str) or not model or model.startswith('-') or '\0' in model:
        raise ValueError(f'{where}: builder.model must name a model')
    effort = builder.get('effort', 'medium')
    if effort not in EFFORTS:
        raise ValueError(f'{where}: builder.effort must be one of {", ".join(EFFORTS)}')
    limits = _fields(builder.get('rate_limit', {}), ('claude_max_retries', 'retries', 'wait_seconds'),
                     f'{where}: builder.rate_limit')
    waits = _fields(limits.get('wait_seconds', {}), ('first', 'multiplier', 'max'), f'{where}: builder.rate_limit.wait_seconds')
    rate_limit = RateLimit(
        _integer(limits.get('claude_max_retries', 3), f'{where}: claude_max_retries', 0, 15),
        _integer(limits.get('retries', 10), f'{where}: rate_limit.retries'),
        _number(waits.get('first', 20), f'{where}: wait_seconds.first', strict=True),
        _number(waits.get('multiplier', 2), f'{where}: wait_seconds.multiplier', 1.0),
        _number(waits.get('max', 300), f'{where}: wait_seconds.max', strict=True))
    if rate_limit.maximum < rate_limit.first:
        raise ValueError(f'{where}: wait_seconds.max must be at least wait_seconds.first')
    return Settings(enabled, default, Builder(
        background, model, effort, _integer(builder.get('concurrency', 1), f'{where}: builder.concurrency', 1),
        _integer(builder.get('turn_retries', 3), f'{where}: builder.turn_retries'), rate_limit))


def parse(value) -> Config:
    value = _fields(value, ('schema', *SECTION, 'harnesses'), FILE)
    if value.get('schema', 1) != 1:
        raise ValueError(f'Unsupported {FILE} schema')
    harnesses = value.get('harnesses', {})
    if not isinstance(harnesses, dict):
        raise ValueError('harnesses must map harness names to settings')
    for name, override in harnesses.items():
        _fields(override, SECTION, f'harnesses.{name}')
    config = Config({key: value[key] for key in SECTION if key in value}, dict(harnesses))
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
    builder = asdict(settings.builder)
    limits = builder.pop('rate_limit')
    builder['rate_limit'] = {'claude_max_retries': limits['claude_max_retries'], 'retries': limits['retries'],
                             'wait_seconds': {'first': limits['first'], 'multiplier': limits['multiplier'],
                                              'max': limits['maximum']}}
    return {'enabled': settings.enabled, 'default': settings.default.settings(), 'builder': builder}
