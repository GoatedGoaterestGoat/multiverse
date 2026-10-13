"""The harnesses tree compaction supports: each family's compaction source, its builder settings, and agents.

A source implements engine.protocol.CompactionSource for one harness family;
SOURCES names its package and class. Everything else in the plugin reaches a
harness through here.
"""
from importlib import import_module

from .engine import config, pipeline

# The api.Plugin that register() received; set before anything calls into the plugin.
MV = None

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


def source(harness, session):
    """The compaction source for this exact session (api.HarnessInfo, api.SessionInfo), or CompactionError."""
    module, name = _module(harness.family)
    if module is None:
        raise pipeline.CompactionError(f'The {session.harness} harness does not support compaction')
    return getattr(module, name)(harness, session)


def open_agent(root, reference):
    """An agent's compaction context (pipeline.Agent), found by name, ID or directory name."""
    agent = MV.universe(root).agent(reference)
    session = agent.session()
    harness = MV.harness_info(agent.universe.path, agent.harness)
    return pipeline.open_agent(agent.universe.path, agent.directory, agent.harness, source(harness, session))
