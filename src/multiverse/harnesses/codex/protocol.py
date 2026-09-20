"""Exact, already-loaded thread control using the native app-server protocol.

Never starts/resumes a thread. A rejected steer stays rejected: racing turn
completion is not permission to submit the message to a different turn.
"""
from pathlib import Path

from ..types import NativeResult, RpcRequest


def object_field(value, name):
    field = value.get(name)
    if not isinstance(field, dict):
        raise ValueError(f'Native RPC {name} must be an object')
    return field


def records(value):
    data = value.get('data')
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError('Native RPC data must be a list of objects')
    return data


def result(response):
    if 'error' in response:
        error = object_field(response, 'error')
        if type(error.get('code')) is not int or not isinstance(error.get('message'), str):
            raise ValueError('Native RPC error requires an integer code and text message')
        state = 'unavailable' if error.get('code') == -32601 else 'error'
        return NativeResult(state, f"Native RPC {error.get('code')}: {error.get('message')}", error.get('data'))
    value = response.get('result')
    if not isinstance(value, dict):
        raise ValueError('Native RPC result must be an object')
    return value


def _thread(session):
    native_id = session.native_id
    initialized = result((yield RpcRequest('initialize', {
        'clientInfo': {'name': 'multiverse', 'title': 'Multiverse native delivery', 'version': '1'},
        'capabilities': {'experimentalApi': True},
    })))
    if isinstance(initialized, NativeResult):
        return initialized
    observed = result((yield RpcRequest('thread/read', {'threadId': native_id, 'includeTurns': False})))
    if isinstance(observed, NativeResult):
        return observed
    thread = object_field(observed, 'thread')
    if thread.get('id') != native_id:
        return NativeResult('error', 'Native server returned a different thread ID')
    if not isinstance(thread.get('cwd'), str) or not thread['cwd'] or Path(thread['cwd']).resolve() != Path(session.workspace).resolve():
        return NativeResult('error', 'Native thread workspace differs from the saved agent workspace')
    if thread.get('canAcceptDirectInput') is False:
        return NativeResult('unavailable', 'Native thread does not accept direct input')
    state = object_field(thread, 'status').get('type')
    if not isinstance(state, str):
        raise ValueError('Native RPC thread status type must be text')
    if state not in ('idle', 'active'):
        return NativeResult('unavailable', f'Exact native thread is not controllable on this server ({state}); no resume attempted')
    return thread


def check_connection(session):
    thread = yield from _thread(session)
    if isinstance(thread, NativeResult):
        return thread
    for method, params in (
        ('thread/turns/list', {'threadId': session.native_id, 'limit': 1, 'itemsView': 'notLoaded'}),
        ('thread/queue/list', {'threadId': session.native_id, 'limit': 1}),
    ):
        observed = result((yield RpcRequest(method, params)))
        if isinstance(observed, NativeResult):
            return observed
        records(observed)
    return NativeResult('ok', 'Exact loaded thread and native queue observed')


def send_message(session, message, mode):
    thread = yield from _thread(session)
    if isinstance(thread, NativeResult):
        return thread
    if not message.id:
        return NativeResult('error', 'A durable message ID is required for native input')
    params = {'threadId': session.native_id, 'clientUserMessageId': message.id,
              'input': [{'type': 'text', 'text': message.prompt(), 'text_elements': []}]}
    if mode == 'follow-up' or thread['status']['type'] == 'idle':
        accepted = result((yield RpcRequest('thread/queue/add', params)))
        if isinstance(accepted, NativeResult):
            return accepted
        item = object_field(accepted, 'queuedSubmission')
        if not isinstance(item.get('id'), str) or not item['id'] or item.get('clientUserMessageId') != message.id:
            return NativeResult('unknown', 'Native queue receipt does not correlate with this message')
        return NativeResult('native_queued', 'Accepted by native after-turn queue; consumption unconfirmed',
                            {'queued_submission_id': item['id'], 'client_user_message_id': message.id})
    turns = result((yield RpcRequest('thread/turns/list', {
        'threadId': session.native_id, 'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded'})))
    if isinstance(turns, NativeResult):
        return turns
    data = records(turns)
    if len(data) != 1 or data[0].get('status') != 'inProgress' or not isinstance(data[0].get('id'), str) or not data[0]['id']:
        return NativeResult('error', 'Active turn changed before steering; retry explicitly against current state')
    turn_id = data[0]['id']
    accepted = result((yield RpcRequest('turn/steer', {**params, 'expectedTurnId': turn_id})))
    if isinstance(accepted, NativeResult):
        return accepted
    if accepted.get('turnId') != turn_id:
        return NativeResult('unknown', 'Native steer receipt has a different turn ID')
    return NativeResult('native_queued', 'Native safe-boundary steering accepted; consumption unconfirmed',
                        {'turn_id': turn_id, 'client_user_message_id': message.id})


def get_delivery(session, message_id):
    """Bounded receipt lookup; absence never establishes non-delivery."""
    thread = yield from _thread(session)
    if isinstance(thread, NativeResult):
        return thread
    queued = result((yield RpcRequest('thread/queue/list', {'threadId': session.native_id, 'limit': 100})))
    if isinstance(queued, NativeResult):
        return queued
    for item in records(queued):
        if item.get('clientUserMessageId') == message_id:
            return NativeResult('native_queued', 'Message remains in native queue',
                                {'queued_submission_id': item.get('id'), 'transcript_confirmed': False})
    turns = result((yield RpcRequest('thread/turns/list', {
        'threadId': session.native_id, 'limit': 20, 'itemsView': 'full', 'sortDirection': 'desc'})))
    if isinstance(turns, NativeResult):
        return turns
    for turn in records(turns):
        items = turn.get('items')
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError('Native RPC turn items must be objects')
        for item in items:
            if item.get('type') == 'userMessage' and item.get('clientId') == message_id:
                return NativeResult('native_queued', 'Message observed in native thread history',
                                    {'turn_id': turn.get('id'), 'transcript_confirmed': True})
    return NativeResult('unknown', 'No receipt found in bounded native queue/history lookup; no resend attempted')
