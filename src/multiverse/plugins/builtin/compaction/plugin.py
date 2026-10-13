"""Tree compaction as a bundled plugin (docs/plugins.md, "Moving compaction into a plugin").

The plugin's own code: engine/ (harness-neutral: settings, schedulers, trees,
building, compaction, the helper actions), sources/ (each harness family's
compaction source), support.py (which harnesses it supports), command.py
(`mv2 compaction`), screens.py (its settings under the dashboard's P) and
claude/ (its Claude plugin, which answers /compact). register() wires them to
mv2 and keeps the API object in support.MV; nothing here imports mv2 beyond
multiverse.plugins.api and multiverse.plugins.ui. Until a universe is migrated, its compaction.json (the manifest's
settings.legacy) keeps the plugin on there.
"""
from datetime import datetime
import json
from pathlib import Path
import re

from multiverse.plugins import api

from . import command, support
from .engine import actions, config
from .support import builder_support, state

# Claude's own compaction is refused unless a subagent asked for it (exit 2 blocks
# it). Runs with the agent's Python, like the mod's reports; a missing one blocks too.
GUARD = '"${MV2_PYTHON:-python3}" -m multiverse.plugins run compaction guard || exit 2'
# The helper actions harness integrations run (`python -m multiverse.plugins run compaction ACTION`).
ACTIONS = ('install', 'background', 'status', 'worker', 'guard')


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
        return api.Capability('unavailable', f'Off for {info.harness} in {config.path(info.universe).relative_to(info.universe)}')
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


def check_settings(mv, settings, root):
    """plugin-settings/compaction.json: schema 2, every section valid, and each harness's builder settings its own checks accept."""
    loaded = config.parse(settings)
    if loaded.schema != 2:
        raise ValueError('plugin-settings/compaction.json is "schema": 2')
    for manifest in sorted((Path(root) / 'harnesses').glob('*/harness.json')):
        try:
            info = mv.harness_info(root, manifest.parent.name)
        except api.Error:
            continue
        problem = state(root, info.name, info.family, loaded)[1]
        if problem:
            raise ValueError(problem)


def migrate(data, source):
    """compaction.json (schema 1) as plugin-settings/compaction.json, and whether the plugin is on.

    It is on where the old file left compaction on for some harness; the
    settings keep everything but `enabled`, except `harnesses.<name>.enabled:
    false` for a harness that was off while another was on. A harness the old
    file did not name follows the plugin's switch from now on.
    """
    if source != 'legacy':
        raise ValueError(f'no migration from compaction settings version {source}')
    old = config.parse(data)
    names = sorted({'claude', *old.harnesses})
    enabled = {name: old.for_harness(name).enabled for name in names}
    on = any(enabled.values())
    settings = {'schema': 2}
    if 'default' in data:
        settings['default'] = data['default']
    harnesses = {}
    for name, section in (data.get('harnesses') or {}).items():
        kept = {key: value for key, value in section.items() if key != 'enabled'}
        if on and not enabled[name]:
            kept = {'enabled': False, **kept}
        if kept:
            harnesses[name] = kept
    if harnesses:
        settings['harnesses'] = harnesses
    return settings, on


def register(mv: api.Plugin):
    support.MV = mv  # command, screens and the helper actions reach mv2 through it
    mv.launch.contribute('claude', launch)
    mv.capabilities.add('compaction', lambda info: capability(mv, info))
    mv.usage.adjust(usage)
    mv.doctor.check(lambda universe: doctor(mv, universe))
    mv.settings.check(lambda settings, root: check_settings(mv, settings, root))
    mv.settings.migrate(migrate)
    mv.cli.command('compaction', command.configure, command.run)
    for name in ACTIONS:
        mv.helper.action(name, lambda arguments, name=name: actions.main([name, *arguments]))
    # Its settings under the dashboard's P screen: its own screen, which also opens each agent's scheduler.
    mv.dashboard.settings(screen='screens:CompactionScreen')
