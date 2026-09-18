"""Claude native TUI lifecycle. Mods are not required or assumed installed."""
from pathlib import Path
from ..base import ConfiguredAdapter
from ..types import Capability


class ClaudeAdapter(ConfiguredAdapter):
    package = Path(__file__).parent

    def native_capabilities(self, context):
        result = super().native_capabilities(context)
        for name in ('steer', 'follow_up', 'native_queue', 'wake', 'activity', 'usage', 'interrupt', 'ask'):
            result[name] = Capability('unavailable', 'No verified Mods/channel bridge attached to this native TUI',
                                      required=name in ('steer', 'follow_up'))
        return result
