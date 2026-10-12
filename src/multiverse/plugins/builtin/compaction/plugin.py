"""Tree compaction as a bundled plugin (docs/plugins.md, "Moving compaction into a plugin").

While the move is under way, register() points at the existing modules
(multiverse.compaction and each harness's compaction source); core's direct
calls are replaced one extension point at a time. The plugin is on wherever
the universe's compaction.json is (the manifest's settings.legacy), so a
universe behaves as before until it is migrated.
"""
from datetime import datetime
from importlib import import_module
import json
from pathlib import Path
import re

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
    # MV2_COMPACTION_PLUGIN: this launch loads the plugin's own Claude directory (claude/), so the
    # multiverse mod's transition copy of the hooks stands aside.
    return api.Contribution(environment={'MV2_COMPACTION': '1', 'MV2_COMPACTION_PLUGIN': '1'},
                            claude_settings={'hooks': {'PreCompact': [{'hooks': [{'type': 'command', 'command': GUARD}]}]}})


def capability(mv, info):
    """Whether this session compacts with trees, and why not; every harness answers, as core's own capabilities do."""
    if builder_support(info.family) is None:
        return api.Capability('unavailable', 'This harness has no tree compaction')
    if not mv.harness_info(info.universe, info.harness).bridge:
        return api.Capability('unavailable', 'Needs the multiverse mod: remove "inbox_bridge": false from harness.json')
    settings, problem = state(info.universe, info.harness, info.family)
    if problem:
        return api.Capability('error', f'{problem}; launches run without compaction')
    if not settings.enabled:
        return api.Capability('unavailable', f'Off for {info.harness} in compaction.json')
    fallback = (info.native_mode or {}).get('fallback_reason')
    if info.state == 'running' and fallback:
        return api.Capability('error', f'{fallback}; restart the agent to use compaction')
    return api.Capability('supported', mechanism='claude-mod-session-compact')


_compactions = {}


def latest_compaction(agent_dir):
    """The newest compaction the session has installed: {'install', 'at', 'tokens'} (tokens estimated), or None.

    The estimate is the compaction's own count of the conversation it left
    (`stats.tokens_after`: prompts, summaries and raw leaves at about four
    characters a token, so no system prompt or tools, and well under the real
    count); read again only when the install log changes.
    """
    directory = Path(agent_dir) / 'compaction'
    try:
        log = directory / 'installed.jsonl'
        status = log.stat()
        key = (status.st_mtime_ns, status.st_size)
        cached = _compactions.get(log)
        if cached and cached[0] == key:
            return cached[1]
        record = None
        for line in log.read_text().splitlines():
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, dict) and re.fullmatch(r'[0-9a-f-]{36}', str(found.get('install', ''))) and found.get('at'):
                record = found
        result = None
        if record:
            # installed.jsonl's `at` is when the record was derived from the transcript, possibly much later;
            # the install file is written just before the rows are handed to the harness.
            payload = json.loads((directory / 'installs' / f"{record['install']}.json").read_text())
            tokens, created = payload.get('stats', {}).get('tokens_after'), payload.get('created_at')
            if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 and created:
                result = {'install': record['install'], 'at': created, 'tokens': tokens}
        _compactions[log] = (key, result)
        return result
    except (OSError, ValueError, AttributeError):
        return None


def _moment(text):
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')) if isinstance(text, str) else None
    except ValueError:
        return None


def usage(info, value):
    """After our compaction, its own count of the conversation stands in for the context until a response comes.

    The harness's `context.responded_at` says when its latest main-thread
    response came; a response newer than the compaction has the real size.
    """
    compaction = latest_compaction(info.directory)
    if compaction is None:
        return None
    context = value.get('context') if isinstance(value.get('context'), dict) else {}
    responded, installed = _moment(context.get('responded_at')), _moment(compaction['at'])
    if installed is None or (responded and responded > installed):
        return None
    context = {key: item for key, item in context.items() if key not in ('tokens', 'percent')}
    context.update(tokens=compaction['tokens'], estimated=True, estimated_reason='after compaction: conversation only',
                   estimated_at=compaction['at'], estimated_from=compaction['install'])
    return {**value, 'context': context}


def doctor(mv, universe):
    """Why this universe's compaction.json cannot be used, by the file or by a harness that compacts."""
    from multiverse.compaction import config
    root = universe.path
    try:
        config.load(root)
    except ValueError as error:
        return [str(error)]
    found = []
    for manifest in sorted((root / 'harnesses').glob('*/harness.json')):
        try:
            info = mv.harness_info(root, manifest.parent.name)
        except api.Error:
            continue
        problem = state(root, info.name, info.family)[1]
        if problem:
            found.append(problem)
    return found


def register(mv: api.Plugin):
    mv.launch.contribute('claude', launch)
    mv.capabilities.add('compaction', lambda info: capability(mv, info))
    mv.usage.adjust(usage)
    mv.doctor.check(lambda universe: doctor(mv, universe))
    from multiverse.tools import compaction as command
    mv.cli.command('compaction', command.configure, command.run)
    mv.dashboard.key('c', 'Compaction', screen='screens:CompactionScreen')
