"""Compaction settings: the universe's settings file and each agent's own scheduler.

Compaction is the `compaction` plugin: `plugins.json` turns it on or off for a
universe or subverse, and `plugin-settings/compaction.json` configures it:

    {"schema": 2,
     "default": {"scheduler": "last_k_raw", "k": 2, "budget": 0, "image_mode": "budget_fit"},
     "harnesses": {
       "claude": {
         "builder": {"model": "sonnet", "fallback_model": "claude-sonnet-4-6", "effort": "medium",
                     "claude_max_retries": 3,
                     "background": true, "concurrency": 1, "turn_retries": 3,
                     "rate_limit": {"retries": 10, "wait_seconds": {"first": 20, "multiplier": 2, "max": 300}}}}}}

While the plugin is on it applies to every harness that supports it;
`harnesses.<name>.enabled: false` turns one harness off. The `default`
scheduler is shared by every harness, and `harnesses.<name>.default` may
replace it. Tree building belongs to one harness, so `builder` exists only
under `harnesses.<name>`: its model, fallback model and effort name that
harness's own, and any field not listed above is an option of that harness
(Claude's `claude_max_retries`). The harness supplies their defaults and
checks them (CompactionSource.builder_defaults and check_builder); the other
fields mean the same for every harness. Every field is optional, and a
missing file means the defaults.

Until `mv2 plugins migrate` converts it, a universe's pre-plugin
`compaction.json` (schema 1) is read in its place, as it always was: its
top-level `enabled` (off by default) and `harnesses.<name>.enabled` decide
which harnesses compact.

An agent may have its own scheduler in `state/agents/<id>/compaction/config.json`;
without one it follows its harness's default as it is now.
"""
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import re

from multiverse.store import write

SETTINGS = 'plugin-settings/compaction.json'  # the plugin's settings (schema 2)
LEGACY = 'compaction.json'                    # the pre-plugin file (schema 1), read until it is migrated
FILE = LEGACY
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
    schema: int = 1

    def for_harness(self, name: str, defaults: dict | None = None) -> Settings:
        """One harness's settings; `defaults` are its builder defaults (CompactionSource.builder_defaults)."""
        override = self.harnesses.get(name, {})
        merged = {**self.shared, **{key: override[key] for key in SHARED if key in override}}
        merged['builder'] = _merge(defaults or {}, override.get('builder', {}))
        return _settings(merged, f'harnesses.{name}' if name in self.harnesses else 'compaction settings')


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


def override(schedule: Schedule, changes: dict) -> Schedule:
    """`schedule` with `changes` (scheduler fields) applied, checked as a whole.

    A different scheduler keeps the budget and image_mode and takes its own
    defaults for the rest; a field the resulting scheduler does not take is an
    error.
    """
    name = changes.get('scheduler') or schedule.scheduler
    values = schedule.settings() if name == schedule.scheduler else {'scheduler': name, 'budget': schedule.budget}
    if schedule.image_mode != 'budget_fit':
        values['image_mode'] = schedule.image_mode
    values.update({key: value for key, value in changes.items() if value is not None})
    return parse_schedule(values)


ARGUMENTS = ('scheduler', 'budget', 'k', 'recency_ratio', 'recency_half_life', 'image_mode')
ARGUMENTS_HELP = ('a scheduler name (' + ', '.join(SCHEDULERS) + '), a budget (60000 or 60k), and key=value for '
                  + ', '.join(ARGUMENTS) + ', e.g. "proportional 60k" or "budget=20k k=4"')
_BUDGET = re.compile(r'(\d+(?:\.\d+)?)([km]?)', re.IGNORECASE)


def _budget(text: str) -> int:
    match = _BUDGET.fullmatch(text.replace('_', ''))
    if not match:
        raise ValueError(f'budget must be a whole number of tokens, like 60000 or 60k, not {text!r}')
    value = float(match.group(1)) * {'': 1, 'k': 1_000, 'm': 1_000_000}[match.group(2).lower()]
    if value != int(value):
        raise ValueError(f'budget must be a whole number of tokens, not {text!r}')
    return int(value)


def _argument(key: str, text: str):
    if key == 'budget':
        return _budget(text)
    if key == 'k':
        if not text.isdigit():
            raise ValueError(f'k must be a whole number, not {text!r}')
        return int(text)
    if key in ('recency_ratio', 'recency_half_life'):
        try:
            return float(text)
        except ValueError:
            raise ValueError(f'{key} must be a number, not {text!r}') from None
    return text


def parse_arguments(text: str | None) -> dict:
    """One-off scheduler settings from a compaction command's arguments: `proportional 60k`, `budget=20k k=4`.

    Accepts a scheduler name, a bare budget and key=value pairs (keys may use
    - for _), each setting at most once. Raises ValueError, naming what it
    accepts, on anything else.
    """
    changes: dict = {}

    def put(key, value, word):
        if key in changes:
            raise ValueError(f'{key} is given twice ({word!r})')
        changes[key] = value

    for word in (text or '').split():
        key, separator, value = word.partition('=')
        key = key.lower().replace('-', '_')
        try:
            if separator:
                if key not in ARGUMENTS:
                    raise ValueError(f'unknown setting {key!r}')
                if not value:
                    raise ValueError(f'{key} has no value')
                put(key, _argument(key, value), word)
            elif key in SCHEDULERS:
                put('scheduler', key, word)
            elif word[:1].isdigit():
                put('budget', _budget(word), word)
            else:
                raise ValueError(f'unknown argument {word!r}')
        except ValueError as error:
            raise ValueError(f'{error}. It accepts {ARGUMENTS_HELP}') from None
    return changes


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
    """Settings as written: schema 2 (plugin-settings/compaction.json) or schema 1 (compaction.json, the default)."""
    if isinstance(value, dict) and 'builder' in value:
        raise ValueError('builder settings belong to one harness: move them to harnesses.<name>.builder')
    value = _fields(value, ('schema', *SHARED, 'harnesses'), 'compaction settings')
    schema = value.get('schema', 1)
    if schema not in (1, 2):
        raise ValueError(f'Unsupported compaction settings schema {schema!r}')
    if schema == 2 and 'enabled' in value:
        raise ValueError('"enabled" is not a setting: plugins.json turns compaction on or off (mv2 plugins enable '
                         'or disable compaction); harnesses.<name>.enabled: false turns one harness off')
    harnesses = value.get('harnesses', {})
    if not isinstance(harnesses, dict):
        raise ValueError('harnesses must map harness names to settings')
    for name, override in harnesses.items():
        _fields(override, SECTION, f'harnesses.{name}')
    shared = {key: value[key] for key in SHARED if key in value}
    if schema == 2:
        shared['enabled'] = True  # the plugin is on wherever these settings are read
    config = Config(shared, dict(harnesses), schema)
    # Report a mistake in any section now, not when that harness first compacts.
    _settings(config.shared, 'compaction settings')
    for name in harnesses:
        config.for_harness(name)
    return config


def path(root) -> Path:
    """The universe's settings file: plugin-settings/compaction.json, or compaction.json until it is migrated.

    Settings written to a universe that has neither go to the former.
    """
    settings, legacy = Path(root) / SETTINGS, Path(root) / LEGACY
    return legacy if legacy.exists() and not settings.exists() else settings


def legacy(file) -> bool:
    """Whether a settings file is the pre-plugin compaction.json at a universe's root."""
    return Path(file).parent.name != Path(SETTINGS).parent.name


def blank(file) -> dict:
    """What a missing settings file means: the defaults of plugin-settings/compaction.json."""
    return {} if legacy(file) else {'schema': 2}


def load(root) -> Config:
    """The universe's settings (see path); a missing file means the defaults."""
    file = path(root)
    try:
        return parse(json.loads(file.read_text()) if file.exists() else blank(file))
    except ValueError as error:
        raise ValueError(f'{file}: {error}') from error


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
