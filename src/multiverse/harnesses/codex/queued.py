"""An ephemeral, read-only view of the bound thread's after-turn queue."""
from datetime import datetime, timezone
from functools import partial
from collections.abc import Mapping

from ..types import NativeResult, RpcNotification, RpcRequest, RpcSpec
from .plugins import server_version
from .protocol import _read_thread, result

# Independently qualified queue processor/storage/schema; never widen implicitly
# when a different feature's checked release changes.
VERSIONS = frozenset({'0.166.0', '0.167.0', '0.167.1', '0.167.2'})
LIMIT = 100
RESPONSE_BYTES = 64 << 20
SCOPE = ('Codex after-turn queue only; active-turn steers and terminal-local input '
         'are outside this report. Absence does not prove consumption.')
UNAVAILABLE = ('The Codex after-turn queue could not be verified within its read budget; '
               'queued follow-ups are unknown')


def available(session):
    versions = session.native_versions or {}
    return bool(isinstance(versions, Mapping) and isinstance(versions.get('cli'), str)
                and session.state == 'running' and session.native_id and session.launch_id and session.session_file
                and session.endpoint and session.endpoint.transport == 'unix-websocket'
                and isinstance(session.native_mode, Mapping) and session.native_mode.get('mode') == 'bridge'
                and isinstance(session.native_relay, Mapping) and session.native_relay.get('state') == 'ready'
                and versions.get('cli') in VERSIONS and versions.get('daemon') == versions.get('cli'))


def unavailable():
    return NativeResult('unavailable', UNAVAILABLE)


def _id(value, maximum):
    try:
        return isinstance(value, str) and bool(value) and '\0' not in value and len(value.encode('utf-8')) <= maximum
    except UnicodeError:
        return False


def _page(value):
    # Native has no revision cursor. A second offset page cannot repair a
    # concurrent snapshot, so retain positives from this page without paging.
    rows = value.get('data')
    complete = isinstance(rows, list) and len(rows) <= LIMIT and 'nextCursor' in value and value['nextCursor'] is None
    entries, seen = [], set()
    for row in rows[:LIMIT] if isinstance(rows, list) else ():
        if (not isinstance(row, dict) or not _id(row.get('clientUserMessageId'), 128)
                or not _id(row.get('id'), 512) or not isinstance(row.get('input'), list)):
            complete = False
            continue
        client = row['clientUserMessageId']
        if client in seen:
            complete = False  # Native permits duplicate client IDs; presence is still evidence.
            continue
        seen.add(client)
        entries.append({'message_id': client, 'native_id': row['id'], 'since': None})
    reason = SCOPE if complete else 'The native queue page was incomplete or malformed. ' + SCOPE
    return NativeResult('ok', reason, {'entries': entries, 'coverage': 'complete' if complete else 'unknown',
                                     'reason': reason, 'observed_at': datetime.now(timezone.utc).isoformat()})


def read(session):
    try:
        initialized = result((yield RpcRequest('initialize', {
            'clientInfo': {'name': 'multiverse', 'title': 'Multiverse queue observation', 'version': '1'},
            'capabilities': {'experimentalApi': True},
        }, on_success=(RpcNotification('initialized', {}),))))
        if (isinstance(initialized, NativeResult)
                or server_version(initialized.get('userAgent')) != tuple(map(int, session.native_versions['cli'].split('.')))):
            return NativeResult('unavailable', 'The native greeting did not confirm the checked Codex queue version')
        thread = yield from _read_thread(session, read_only=True)
        if isinstance(thread, NativeResult):
            return NativeResult('unavailable', 'The exact Codex queue thread and workspace could not be verified')
        observed = result((yield RpcRequest('thread/queue/list', {'threadId': session.native_id, 'limit': LIMIT})))
        return unavailable() if isinstance(observed, NativeResult) else _page(observed)
    except (ValueError, TypeError, KeyError, OSError):
        return unavailable()


def report(session, executor):
    if not available(session):
        return NativeResult('unavailable', 'Queued prompts need a live verified Codex bridge on matching checked '
                            '0.166.0, 0.167.0, 0.167.1 or 0.167.2 CLI and daemon releases')
    try:
        # Do not call capabilities(executor): its separate control probe has a
        # five-second budget. Initialization, identity and list share this one.
        observed = executor.rpc(RpcSpec(session.endpoint.address, partial(read, session), max_requests=3,
                                        max_response_bytes=RESPONSE_BYTES), timeout=2)
        return observed if observed.state in ('ok', 'unavailable') else unavailable()
    except Exception:
        # A native error/transport exception can contain someone else's queued
        # input. Only fixed diagnostics leave this boundary.
        return unavailable()
