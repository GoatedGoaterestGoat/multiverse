"""Tree compaction as a bundled plugin (docs/plugins.md, "Moving compaction into a plugin").

While the move is under way, register() points at the existing modules
(multiverse.compaction and each harness's compaction source); core's direct
calls are replaced one extension point at a time. The plugin is on wherever
the universe's compaction.json is (the manifest's settings.legacy), so a
universe behaves as before until it is migrated.
"""
from importlib import import_module
from pathlib import Path

from multiverse.plugins import api

# Each harness family with a compaction source: the module with its builder defaults and checks.
SOURCES = {'claude': 'multiverse.harnesses.claude.compaction'}
# Claude's own compaction is refused unless a subagent asked for it (exit 2 blocks
# it). Runs with the agent's Python, like the mod's reports; a missing one blocks too.
GUARD = '"${MV2_PYTHON:-python3}" -m multiverse.compaction guard || exit 2'


def builder_support(family):
    """(builder defaults, check_builder) of a harness family's compaction source, or None without one."""
    module = SOURCES.get(family)
    if module is None:
        return None
    source = import_module(module)
    return dict(source.BUILDER_DEFAULTS), source.check_builder


def state(root, harness, family, loaded=None):
    """(settings, None) for a harness in the universe at `root`, or (None, why its compaction.json cannot be used).

    `loaded` checks a parsed file (config.Config) before it is written instead
    of the one on disk. A family without a compaction source gives (None, None).
    Launches, capabilities and `mv2 doctor` use it; `mv2 compaction` refuses
    loudly instead.
    """
    from multiverse.compaction import config
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
        return None, f'{Path(root) / config.FILE}: harnesses.{harness}.builder: {error}'
    return settings, None


def launch(info):
    """A bridged Claude launch: the mod's switch and the guard while compaction is on, or why it is not.

    An unusable compaction.json never stops a launch: it runs without
    compaction, without the guard, and says why. A plain launch has no mod.
    """
    if info.universe is None or not info.bridge:
        return None
    settings, problem = state(info.universe, info.harness, info.family)
    if problem:
        return api.Contribution(problem=f'Launched without compaction: {problem}', mode='without compaction')
    if settings is None or not settings.enabled:
        return None
    return api.Contribution(environment={'MV2_COMPACTION': '1'},
                            claude_settings={'hooks': {'PreCompact': [{'hooks': [{'type': 'command', 'command': GUARD}]}]}})


def register(mv: api.Plugin):
    mv.launch.contribute('claude', launch)
