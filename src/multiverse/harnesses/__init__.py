"""Source-owned built-ins and read-only compatibility with local schema-1 definitions."""
import json
from pathlib import Path

from .claude import ClaudeAdapter, LegacyClaudeAdapter
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
        return (LegacyClaudeAdapter if name == 'claude' else LegacyAdapter)(name, config)
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
    return BUILTINS[adapter].configured(name, settings)
