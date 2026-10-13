"""One compaction: allocate, make sure the needed trees exist, expand and render."""
from dataclasses import dataclass, replace
import threading

from . import config as settings_file
from .build import Build, Reporter
from .config import Schedule, Settings
from .model import Turn
from .protocol import CompactionSource
from .schedule import allocate, frontier_for, frontier_tokens, kept_raw, needs_tree
from .store import Store


class CompactionError(Exception):
    """A compaction that cannot go ahead; the conversation stays as it is."""


@dataclass
class Agent:
    """One agent's compaction context: its directory, settings and transcript source."""
    root: object
    directory: object
    harness: str
    settings: Settings
    source: CompactionSource

    @property
    def store(self) -> Store:
        return Store(self.directory)

    def schedule(self, changes: dict | None = None) -> tuple[Schedule, str]:
        """The agent's scheduler in effect and where it came from (`agent` or `universe`).

        `changes` are one-off scheduler settings (a compaction command's
        arguments) applied on top; the origin is then `arguments`.
        """
        schedule, origin = settings_file.schedule_for(self.directory, self.settings)
        if changes:
            schedule, origin = settings_file.override(schedule, changes), 'arguments'
        return self.effective(schedule), origin

    def effective(self, schedule: Schedule) -> Schedule:
        """`schedule` as this harness applies it: prompt images dropped when it cannot keep them."""
        return schedule if self.source.keeps_images else replace(schedule, image_mode='drop_all')


def open_agent(root, directory, harness, source: CompactionSource) -> Agent:
    """The agent's compaction context, given its harness's compaction source for its session.

    The caller finds the agent and its source (support.open_agent), so the
    engine depends on no harness. The source supplies its builder defaults and
    checks the builder settings.
    """
    settings = settings_file.load(root).for_harness(harness, source.builder_defaults)
    try:
        source.check_builder(settings.builder)
    except ValueError as error:
        raise ValueError(f'{settings_file.path(root)}: harnesses.{harness}.builder: {error}') from error
    return Agent(root, directory, harness, settings, source)


def conversation(turns: list[Turn]) -> list[Turn]:
    """The turns compaction works on: every turn the harness did not mark as dropped."""
    return [turn for turn in turns if not turn.dropped]


def plan(turns: list[Turn], schedule: Schedule, store: Store | None = None) -> list[dict]:
    """Per turn: its size, its share, whether it stays raw and whether its tree exists."""
    shares, images = allocate(turns, schedule)
    rows = []
    for turn in turns:
        share = shares[turn.id]
        has_tree = store.load_tree(turn) if store is not None else turn.root is not None
        rows.append({'turn': turn.id, 'prompt': turn.prompt.preview if turn.prompt else '', 'leaves': len(turn.leaves),
                     'tokens': turn.tokens, 'omitted': turn.omitted, 'share': share, 'raw': kept_raw(turn, share),
                     'needs_tree': needs_tree(turn, share), 'has_tree': has_tree,
                     'prompt_images': 'kept' if turn.id in images else 'dropped' if turn.prompt and turn.prompt.images else None})
    return rows


def compact(agent: Agent, offered, reporter: Reporter, cancel: threading.Event | None = None,
            changes: dict | None = None):
    """The installable payload of the agent's conversation compacted by its scheduler.

    `changes` are one-off scheduler settings for this compaction only. Raises
    CompactionError when it cannot be made in full: nothing partial is ever
    returned for installing.
    """
    schedule, origin = agent.schedule(changes)
    every = agent.source.turns()
    turns = conversation(every)
    if not turns:
        raise CompactionError('There is no conversation to compact yet')
    store = agent.store
    shares, images = allocate(turns, schedule)
    for turn in turns:
        store.load_tree(turn)
    missing = [turn for turn in turns if needs_tree(turn, shares[turn.id]) and turn.root is None]
    if missing:
        reporter.info(f'building {len(missing)} missing summary tree(s)')
        failures = Build(agent.source, store, agent.settings.builder, reporter, cancel).turns(missing)
        if failures:
            reasons = '; '.join(f'turn {turn_id[:8]}: {reason}' for turn_id, reason in failures.items())
            raise CompactionError(f'Could not build {len(failures)} summary tree(s): {reasons}')
    frontiers = {turn.id: frontier_for(turn, shares[turn.id]) for turn in turns}
    payload = agent.source.render(turns, frontiers, offered, images)
    prompts = sum(turn.prompt.tokens for turn in turns if turn.prompt)
    dropped_images = sum(turn.prompt.images for turn in turns if turn.prompt and turn.id not in images)
    stats = {'turns': len(turns), 'summarized': sum(1 for turn in turns if not kept_raw(turn, shares[turn.id])),
             'dropped_turns': len(every) - len(turns),
             'tokens_before': prompts + sum(turn.tokens + turn.omitted for turn in turns),
             'tokens_after': prompts - dropped_images + sum(frontier_tokens(frontier) for frontier in frontiers.values()),
             'scheduler': schedule.settings(), 'scheduler_from': origin}
    return payload, stats
