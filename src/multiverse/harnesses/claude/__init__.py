"""Claude native terminal lifecycle; optional controls never assumed installed."""
from pathlib import Path
from ..base import ConfiguredAdapter
from ..types import NativeResult


class ClaudeAdapter(ConfiguredAdapter):
    package = Path(__file__).parent

    def spawn(self, context):
        return super().spawn(context)

    def send(self, session, message, executor):
        return NativeResult('unavailable', 'No verified Mods/channel bridge attached to this Claude terminal')
