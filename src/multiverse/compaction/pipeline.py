"""One compaction: allocate, make sure the needed trees exist, expand and render."""
from dataclasses import dataclass, replace
import threading

from .. import native
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

    def schedule(self) -> tuple[Schedule, str]:
        """The agent's scheduler and where it came from; prompt images dropped when the harness cannot keep them."""
        schedule, origin = settings_file.schedule_for(self.directory, self.settings)
        if not self.source.keeps_images:
            schedule = replace(schedule, image_mode='drop_all')
        return schedule, origin


def open_agent(root, reference) -> Agent:
    """The agent's compaction context, through its harness adapter's compaction support."""
    directory, adapter, session, _ = native.context(root, reference)
    settings = settings_file.load(root).for_harness(session.harness)
    factory = getattr(adapter, 'compaction', None)
    if factory is None:
        raise CompactionError(f'The {session.harness} harness does not support compaction')
    return Agent(root, directory, session.harness, settings, factory(session))


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


def compact(agent: Agent, offered, reporter: Reporter, cancel: threading.Event | None = None):
    """The installable payload of the agent's conversation compacted by its scheduler.

    Raises CompactionError when it cannot be made in full: nothing partial is
    ever returned for installing.
    """
    schedule, _ = agent.schedule()
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
             'scheduler': schedule.settings()}
    return payload, stats
