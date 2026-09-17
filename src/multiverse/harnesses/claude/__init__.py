"""Claude native TUI lifecycle. Mods are not required or assumed installed."""
from ..legacy import LegacyAdapter
from ..types import Capability


class ClaudeAdapter(LegacyAdapter):
    def capabilities(self, context):
        result = super().capabilities(context)
        for name in ('steer', 'follow_up', 'native_queue', 'wake', 'activity', 'usage', 'interrupt', 'ask'):
            result[name] = Capability('unavailable', 'No verified Mods bridge attached to this native TUI')
        return result
