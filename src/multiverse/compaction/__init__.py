"""What is left of tree compaction in mv2 itself: the guard of Claude launches from before it became a plugin.

Tree compaction is the `compaction` checkout plugin (plugins/compaction, GOAT).
A Claude agent launched before that still carries the PreCompact hook
`"${MV2_PYTHON}" -m multiverse.compaction guard || exit 2` in its --settings,
so `python -m multiverse.compaction` (__main__.py) stays for a release: its guard
lets Claude compact, and `mv2 doctor` lists the running agents that still use it
(old_guard). Remove this package once doctor names none.
"""
import json
from pathlib import Path

GUARD = '-m multiverse.compaction '


def old_guard(directory, runtime) -> bool:
    """Whether the agent at `directory` is running a launch whose PreCompact hook calls this module.

    Only the hook commands in the launch's --settings count: its prompt or
    messages may well mention the module. A launch record that cannot be read
    or has another shape counts as not.
    """
    runtime = runtime if isinstance(runtime, dict) else {}
    if runtime.get('state') != 'running' or not isinstance(runtime.get('launch_id'), str):
        return False
    try:
        request = json.loads((Path(directory) / 'launches' / runtime['launch_id'] / 'request.json').read_text())
    except (OSError, ValueError):
        return False
    argv = request.get('argv') if isinstance(request, dict) else None
    if not isinstance(argv, list):
        return False
    for flag, value in zip(argv, argv[1:]):
        if flag == '--settings' and isinstance(value, str) and any(GUARD in command for command in _precompact(value)):
            return True
    return False


def _precompact(settings_text):
    """The PreCompact hook commands of a --settings value, skipping anything of another shape."""
    try:
        settings = json.loads(settings_text)
    except ValueError:
        return []
    hooks = settings.get('hooks') if isinstance(settings, dict) else None
    groups = hooks.get('PreCompact') if isinstance(hooks, dict) else None
    commands = []
    for group in groups if isinstance(groups, list) else []:
        entries = group.get('hooks') if isinstance(group, dict) else None
        for hook in entries if isinstance(entries, list) else []:
            if isinstance(hook, dict) and isinstance(hook.get('command'), str):
                commands.append(hook['command'])
    return commands
