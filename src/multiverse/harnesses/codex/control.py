"""Exact, already-loaded thread control using the native app-server protocol.

Never starts/resumes a thread. A rejected steer stays rejected: racing turn
completion is not permission to submit the message to a different turn.
"""
from pathlib import Path

from ..types import NativeResult, RpcRequest


def result(response):
    if 'error' in response:
        error = response['error']
        state = 'unavailable' if error.get('code') == -32601 else 'error'
        return NativeResult(state, f"Native RPC {error.get('code')}: {error.get('message')}", error.get('data'))
    value = response.get('result')
    if not isinstance(value, dict):
        raise ValueError('Native RPC result must be an object')
    return value


def plan(context, operation=None, text=''):
    native_id = context.properties['native_session_id']
    initialized = result((yield RpcRequest('initialize', {
        'clientInfo': {'name': 'multiverse', 'title': 'Multiverse native delivery', 'version': '1'},
        'capabilities': {'experimentalApi': True},
    })))
    if isinstance(initialized, NativeResult):
        return initialized
    observed = result((yield RpcRequest('thread/read', {'threadId': native_id, 'includeTurns': False})))
    if isinstance(observed, NativeResult):
        return observed
    thread = observed.get('thread', {})
    if thread.get('id') != native_id:
        return NativeResult('error', 'Native server returned a different thread ID')
    if not thread.get('cwd') or Path(thread['cwd']).resolve() != Path(context.properties['workspace']).resolve():
        return NativeResult('error', 'Native thread workspace differs from the saved agent workspace')
    if thread.get('canAcceptDirectInput') is False:
        return NativeResult('unavailable', 'Native thread does not accept direct input')
    state = thread.get('status', {}).get('type')
    if state not in ('idle', 'active'):
        return NativeResult('unavailable', f'Exact native thread is not controllable on this server ({state}); no resume attempted')
    if operation is None:
        # Read-only feature discovery checks the installed protocol and queue service.
        for method, params in (
            ('thread/turns/list', {'threadId': native_id, 'limit': 1, 'itemsView': 'notLoaded'}),
            ('thread/queue/list', {'threadId': native_id, 'limit': 1}),
        ):
            observed = result((yield RpcRequest(method, params)))
            if isinstance(observed, NativeResult):
                return observed
        return NativeResult('ok', 'Exact loaded thread and native queue observed')
    if not context.message_id:
        return NativeResult('error', 'A durable message ID is required for native input')
    params = {'threadId': native_id, 'clientUserMessageId': context.message_id,
              'input': [{'type': 'text', 'text': text, 'text_elements': []}]}
    # Queue dispatch is native start-if-idle: it never steers a busy turn.
    if operation in ('follow_up', 'native_queue') or state == 'idle':
        accepted = result((yield RpcRequest('thread/queue/add', params)))
        if isinstance(accepted, NativeResult):
            return accepted
        item = accepted.get('queuedSubmission', {})
        if not item.get('id') or item.get('clientUserMessageId') != context.message_id:
            return NativeResult('error', 'Native queue receipt does not correlate with this message; acceptance is unknown')
        return NativeResult('native_queued', 'Native after-turn queue accepted the message; read and answer unconfirmed',
                            {'queued_submission_id': item['id'], 'client_user_message_id': context.message_id})
    turns = result((yield RpcRequest('thread/turns/list', {
        'threadId': native_id, 'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded'})))
    if isinstance(turns, NativeResult):
        return turns
    data = turns.get('data', [])
    if len(data) != 1 or data[0].get('status') != 'inProgress' or not data[0].get('id'):
        return NativeResult('error', 'Active turn changed before steering; retry explicitly against current state')
    turn_id = data[0]['id']
    accepted = result((yield RpcRequest('turn/steer', {**params, 'expectedTurnId': turn_id})))
    if isinstance(accepted, NativeResult):
        return accepted
    if accepted.get('turnId') != turn_id:
        return NativeResult('error', 'Native steer receipt has a different turn ID; acceptance is unknown')
    return NativeResult('native_queued', 'Native safe-boundary steering accepted; read and answer unconfirmed',
                        {'turn_id': turn_id, 'client_user_message_id': context.message_id})
