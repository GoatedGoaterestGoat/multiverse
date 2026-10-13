"""The harnesses tree compaction supports: each family's compaction source, its builder settings, and agents.

A source implements engine.protocol.CompactionSource for one harness family;
SOURCES names its package and class. Everything else in the plugin reaches a
harness through here.
"""
from importlib import import_module

from multiverse.runtime import native

from .engine import config, pipeline

# Each harness family with a compaction source: its package (in sources/) and the class.
SOURCES = {'claude': ('.sources.claude', 'ClaudeCompaction')}


def _module(family):
    found = SOURCES.get(family)
    return (import_module(found[0], __package__), found[1]) if found else (None, None)


def builder_support(family):
    """(builder defaults, check_builder) of a harness family's compaction source, or None without one."""
    module, _ = _module(family)
    if module is None:
        return None
    return dict(module.BUILDER_DEFAULTS), module.check_builder


def state(root, harness, family, loaded=None):
    """(settings, None) for a harness in the universe at `root`, or (None, why its settings cannot be used).

    `loaded` checks parsed settings (config.Config) before they are written
    instead of the file on disk. A family without a compaction source gives
    (None, None). Launches, capabilities and `mv2 doctor` use it; `mv2
    compaction` refuses loudly instead.
    """
    support = builder_support(family)
    if support is None:
        return None, None
    defaults, check = support
    try:
        settings = (loaded or config.load(root)).for_harness(harness, defaults)
    except ValueError as error:
        return None, str(error)
    try:
        check(settings.builder)
    except ValueError as error:
        return None, f'{config.path(root)}: harnesses.{harness}.builder: {error}'
    return settings, None


def source(adapter, session):
    """The compaction source for this exact session, or CompactionError when its harness has none."""
    family = getattr(adapter, 'family', '') or ''
    module, name = _module(family)
    if module is None:
        raise pipeline.CompactionError(f'The {session.harness} harness does not support compaction')
    return getattr(module, name)(adapter, session)


def open_agent(root, reference):
    """An agent's compaction context (pipeline.Agent), found by name, ID or directory name."""
    directory, adapter, session, _ = native.context(root, reference)
    return pipeline.open_agent(root, directory, session.harness, source(adapter, session))
