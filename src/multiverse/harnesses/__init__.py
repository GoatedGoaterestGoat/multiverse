"""Source-owned built-ins and read-only compatibility with local schema-1 definitions."""
import json
from pathlib import Path

from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .legacy import LegacyAdapter, arguments
from .pi import PiAdapter
from .types import HarnessAdapter

PACKAGES = Path(__file__).parent
BUILTINS = {'codex': CodexAdapter, 'claude': ClaudeAdapter, 'pi': PiAdapter}


def load(root: Path, name: str) -> HarnessAdapter:
    # Avoid importing universe here: core depends on adapters, not vice versa.
    if not name or Path(name).name != name or name in ('.', '..'):
        raise ValueError('Invalid harness name')
    path = root / 'harnesses' / name / 'harness.json'
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError('Harness configuration must be an object')
    if config.get('schema') == 1:
        # A legacy file is authoritative; do not rewrite arguments, permissions or assets.
        return LegacyAdapter(name, config)
    if config.get('schema') != 2:
        raise ValueError('Unsupported harness schema')
    unknown = set(config) - {'schema', 'adapter', 'description', 'settings'}
    if unknown:
        raise ValueError(f'Unknown harness configuration fields: {sorted(unknown)}')
    adapter = config.get('adapter')
    if not isinstance(adapter, str) or adapter not in BUILTINS:
        raise ValueError(f'Unknown harness adapter: {adapter!r}')
    settings = config.get('settings', {})
    if not isinstance(settings, dict):
        raise ValueError('Harness settings must be an object')
    unknown = set(settings) - {'executable', 'model', 'args', 'spawn_args', 'resume_args', 'unset_env', 'inbox_bridge'}
    if unknown:
        raise ValueError(f'Unknown harness settings: {sorted(unknown)}')
    spec = json.loads((PACKAGES / adapter / 'defaults.json').read_text())
    for key in ('model', 'unset_env'):
        if key in settings:
            spec[key] = settings[key]
    executable = settings.get('executable', spec['spawn'][0])
    if not isinstance(executable, str) or not executable or '\0' in executable:
        raise ValueError('executable must be a nonempty string')
    bridge = settings.get('inbox_bridge', False)
    if not isinstance(bridge, bool) or (bridge and adapter != 'pi'):
        raise ValueError('inbox_bridge is a boolean supported only by Pi')
    for mode in ('spawn', 'resume'):
        argv = spec[mode]
        argv[0] = executable
        extra = [*arguments(settings.get('args', []), 'args'),
                 *arguments(settings.get(mode + '_args', []), mode + '_args')]
        if bridge:
            extra += ['--extension', str(PACKAGES / 'pi' / 'extension.ts')]
        position = argv.index('--') if '--' in argv else 1
        argv[position:position] = extra
    result = BUILTINS[adapter](name, spec)
    result.settings = settings
    return result
