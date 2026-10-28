"""Core dispatch enforces the adapter protocol's declared effects.

The access context is set by the plugin API/contribution boundary. Calls from
core without that context retain their existing authority. Pure evidence reads
and verified reconciliation of core records remain observe operations.
"""
from ..harnesses.types import EFFECTS
from ..plugins import access


def require(method, agent):
    access.require(EFFECTS[method], f'{method} for {agent}')


def call(adapter, method, *args, **kwargs):
    """Check at the protocol boundary too, including future dispatch callers."""
    target = args[0] if args else None
    agent = getattr(target, 'agent_id', None) or getattr(adapter, 'name', 'agent')
    require(method, agent)
    return getattr(adapter, method)(*args, **kwargs)
