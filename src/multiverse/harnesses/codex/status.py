"""Passive exact-thread status protocol; no thread loads or runtime writes.

ThreadStatusChanged is a server-wide broadcast, unlike turn/item events that
require thread/resume. Filter it locally rather than resuming for observation.
"""
from pathlib import Path

from ..types import NativeResult, RpcRequest, RpcNotification
from .protocol import object_field, result


def status(value):
    if not isinstance(value, dict):
        return 'unknown'
    if value.get('type') == 'idle':
        return 'idle'
    if value.get('type') == 'active':
        flags = value.get('activeFlags')
        if not isinstance(flags, list):
            return 'unknown'
        if any(flag in ('waitingOnApproval', 'waitingOnUserInput') for flag in flags):
            return 'waiting'
        if not flags:
            return 'working'
    return 'unknown'


def snapshot(session):
    initialized = result((yield RpcRequest('initialize', {
        'clientInfo': {'name': 'multiverse-status', 'title': 'Multiverse status', 'version': '1'},
        'capabilities': {'experimentalApi': True},
    }, on_success=(RpcNotification('initialized', {}),))))
    if isinstance(initialized, NativeResult):
        return initialized
    observed = result((yield RpcRequest('thread/read', {'threadId': session.native_id, 'includeTurns': False})))
    if isinstance(observed, NativeResult):
        return observed
    thread = object_field(observed, 'thread')
    if thread.get('id') != session.native_id:
        return NativeResult('error', 'Native server returned a different thread ID')
    if (not isinstance(thread.get('cwd'), str) or not thread['cwd']
            or Path(thread['cwd']).resolve() != Path(session.workspace).resolve()):
        return NativeResult('error', 'Native thread workspace differs from the saved agent workspace')
    return NativeResult('ok', value=status(thread.get('status')))


def notification(session, value):
    # Never answer requests (including approvals), even if their method matches.
    if 'id' in value or value.get('method') != 'thread/status/changed':
        return None
    params = value.get('params')
    if not isinstance(params, dict) or params.get('threadId') != session.native_id:
        return None
    return status(params.get('status'))
