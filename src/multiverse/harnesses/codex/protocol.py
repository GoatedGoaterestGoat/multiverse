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


def _thread(session, *, allow_unloaded=False):
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
    state = object_field(thread, 'status').get('type')
    if not isinstance(state, str):
        raise ValueError('Native RPC thread status type must be text')
    if allow_unloaded and state == 'notLoaded':
        return thread
    if thread.get('canAcceptDirectInput') is False:
        return NativeResult('unavailable', 'Native thread does not accept direct input')
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


def recovery_check(session):
    """Read-only proof that restarting this exact thread's TUI is safe."""
    thread = yield from _thread(session)
    if isinstance(thread, NativeResult):
        return thread
    if thread['status']['type'] != 'idle':
        return NativeResult('unavailable', 'Native thread is active or waiting for approval/input')
    for method, params, reason in (
        ('thread/turns/list', {'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded'},
         'Native turn is still running or its state is unknown'),
        ('thread/queue/list', {'limit': 1}, 'Native messages are queued'),
        ('thread/backgroundTerminals/list', {'limit': 1}, 'Native background processes are running'),
    ):
        observed = result((yield RpcRequest(method, {'threadId': session.native_id, **params})))
        if isinstance(observed, NativeResult):
            return observed
        data = records(observed)
        blocked = (any(turn.get('status') not in ('completed', 'interrupted', 'failed') for turn in data)
                   if method == 'thread/turns/list' else bool(data) or bool(observed.get('nextCursor')))
        if blocked:
            return NativeResult('unavailable', reason)
    # Recheck after the inventory: activity racing the earlier read is unsafe.
    observed = result((yield RpcRequest('thread/read', {'threadId': session.native_id, 'includeTurns': False})))
    if isinstance(observed, NativeResult):
        return observed
    final = object_field(observed, 'thread')
    if (final.get('id') != session.native_id or final.get('cwd') != thread['cwd']
            or object_field(final, 'status').get('type') != 'idle'
            or final.get('canAcceptDirectInput') is False):
        return NativeResult('unavailable', 'Native thread changed during the recovery check')
    return NativeResult('ok', 'Exact native thread is idle, with no queued messages or background processes')


def _start_idle_submission(session, item, receipt):
    """Interrupt can pause native auto-draining. Start only our sole queued item."""
    try:
        queued = result((yield RpcRequest('thread/queue/list', {'threadId': session.native_id, 'limit': 2})))
        if isinstance(queued, NativeResult):
            raise ValueError(queued.reason)
        items = records(queued)
        if not items:
            return receipt  # Native auto-draining already took it.
        if (len(items) != 1 or queued.get('nextCursor') or items[0].get('id') != item['id']
                or items[0].get('clientUserMessageId') != item['clientUserMessageId']):
            return NativeResult('native_queued',
                'Accepted by native queue; other queued work exists, so no explicit start attempted. '
                'Consumption unconfirmed; an interrupted queue may need native continuation.', receipt.value)
        started = result((yield RpcRequest('thread/queue/start', {
            'threadId': session.native_id, 'queuedSubmissionId': item['id']})))
        if isinstance(started, NativeResult):
            raise ValueError(started.reason)
        turn = object_field(started, 'turn')
        if not isinstance(turn.get('id'), str) or not turn['id']:
            raise ValueError('Native queue start returned no turn ID')
        return NativeResult('native_queued', 'Accepted and started from the native idle queue; consumption unconfirmed',
                            {**receipt.value, 'turn_id': turn['id']})
    except ValueError as error:
        # Acceptance is already established. A racing automatic start, an active
        # turn, or an unsupported method must never make this message retryable.
        return NativeResult('native_queued',
            f'Accepted by native queue; explicit idle start was not confirmed: {error}. '
            'Use deliver AGENT --check; do not resend.', {**receipt.value, 'idle_start_error': str(error)})


def send_message(session, message, mode):
    thread = yield from _thread(session)
    if isinstance(thread, NativeResult):
        return thread
    if not message.id:
        return NativeResult('error', 'A durable message ID is required for native input')
    params = {'threadId': session.native_id, 'clientUserMessageId': message.id,
              'input': [{'type': 'text', 'text': message.framed or message.text, 'text_elements': []}]}
    if mode == 'follow-up' or thread['status']['type'] == 'idle':
        accepted = result((yield RpcRequest('thread/queue/add', params)))
        if isinstance(accepted, NativeResult):
            return accepted
        item = object_field(accepted, 'queuedSubmission')
        if not isinstance(item.get('id'), str) or not item['id'] or item.get('clientUserMessageId') != message.id:
            return NativeResult('unknown', 'Native queue receipt does not correlate with this message')
        receipt = NativeResult('native_queued', 'Accepted by native after-turn queue; consumption unconfirmed',
                               {'queued_submission_id': item['id'], 'client_user_message_id': message.id})
        if thread['status']['type'] == 'idle':
            return (yield from _start_idle_submission(session, item, receipt))
        return receipt
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


def environment_exclusions(workspace):
    """Read only the effective exclusion list; never change native config."""
    initialized = result((yield RpcRequest('initialize', {
        'clientInfo': {'name': 'multiverse', 'version': '1'},
        'capabilities': {'experimentalApi': True},
    })))
    if isinstance(initialized, NativeResult):
        return initialized
    observed = result((yield RpcRequest('config/read', {'cwd': workspace, 'includeLayers': False})))
    if isinstance(observed, NativeResult):
        return observed
    policy = object_field(observed, 'config').get('shell_environment_policy') or {}
    excludes = policy.get('exclude') or []
    if not isinstance(excludes, list) or any(not isinstance(v, str) for v in excludes):
        return NativeResult('error', 'Native shell environment exclusions are malformed')
    return NativeResult('ok', value=excludes)


def stop_session(session):
    """Explicit stop cancels only the exact thread; never shuts down its server."""
    cancelled = []
    counts = {'cancelled_submissions': 0, 'interrupted_turns': 0,
              'background_terminal_cleanup_requested': False}
    def failure(observed):
        return NativeResult(observed.state, observed.reason, {'cancelled_message_ids': cancelled, **counts})
    thread = yield from _thread(session, allow_unloaded=True)
    if isinstance(thread, NativeResult):
        return failure(thread)
    if thread['status']['type'] == 'notLoaded':
        return NativeResult('ok', 'Exact native thread is already unloaded; nothing was running',
                            {'cancelled_message_ids': [], **counts})
    # Bound work before any mutation. Core holds the recipient's launch lock,
    # so concurrent Multiverse sends cannot replenish this queue during stop.
    queued = result((yield RpcRequest('thread/queue/list', {'threadId': session.native_id, 'limit': 128})))
    if isinstance(queued, NativeResult):
        return failure(queued)
    items = records(queued)
    if queued.get('nextCursor') or len(items) > 128:
        return NativeResult('error', 'Native queue exceeds bounded stop limit (128); terminal left attached')
    for item in items:
        if not isinstance(item.get('id'), str) or not item['id']:
            return failure(NativeResult('error', 'Malformed native queue ID; stop incomplete'))
        deleted = result((yield RpcRequest('thread/queue/delete', {
            'threadId': session.native_id, 'queuedSubmissionId': item['id']})))
        if isinstance(deleted, NativeResult):
            return failure(deleted)
        if deleted.get('deleted') is True:
            counts['cancelled_submissions'] += 1
            if isinstance(item.get('clientUserMessageId'), str):
                cancelled.append(item['clientUserMessageId'])
    turns = result((yield RpcRequest('thread/turns/list', {
        'threadId': session.native_id, 'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded'})))
    if isinstance(turns, NativeResult):
        return failure(turns)
    for turn in records(turns):
        if turn.get('status') == 'inProgress':
            if not isinstance(turn.get('id'), str) or not turn['id']:
                return failure(NativeResult('error', 'Malformed active turn ID'))
            interrupted = result((yield RpcRequest('turn/interrupt', {
                'threadId': session.native_id, 'turnId': turn['id']})))
            if isinstance(interrupted, NativeResult):
                return failure(interrupted)
            counts['interrupted_turns'] += 1
    cleaned = result((yield RpcRequest('thread/backgroundTerminals/clean', {'threadId': session.native_id})))
    if isinstance(cleaned, NativeResult):
        return failure(cleaned)
    counts['background_terminal_cleanup_requested'] = True
    return NativeResult('ok', 'Native turn cancellation and background-terminal cleanup requested',
                        {'cancelled_message_ids': cancelled, **counts})
