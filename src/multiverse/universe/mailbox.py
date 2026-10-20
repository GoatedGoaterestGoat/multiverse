"""Each agent's message log: appended messages, the read position and per-message delivery tracking."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from ..store import lock, now, read, uid, write
from . import records


def append_message(agent_dir, text, sender, delivery="inbox-only", *, target_session=None):
    if not text.strip():
        raise ValueError("Message cannot be empty")
    if delivery not in ("steer", "follow-up", "inbox-only"):
        raise ValueError("Unknown native delivery mode")
    message = {"schema": 2, "target_session": target_session, "delivery": delivery, "id": uid(), "at": now(), "from": sender, "text": text}
    agent_dir = Path(agent_dir)
    with lock(agent_dir / ".inbox.lock"):
        path = agent_dir / "inbox.jsonl"
        with path.open("ab+") as out:
            if out.tell():
                out.seek(-1, os.SEEK_END)
                if out.read(1) != b"\n":
                    raise ValueError(f"Incomplete inbox tail; repair before appending: {path}")
            out.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            out.flush()
            os.fsync(out.fileno())
    sender = {key: sender.get(key) for key in ("name", "agent_id", "plugin") if isinstance(sender, dict) and key in sender}
    _event(agent_dir, "message.saved", {"id": message["id"], "delivery": delivery, "length": len(text), "from": sender})
    return message


def _event(agent_dir, kind, data):
    """A message event in the recipient's universe; never its text. Best effort, never raises."""
    try:
        from ..plugins import events
        events.for_agent(agent_dir, kind, None, data)
    except Exception:
        pass


def _cursor(agent_dir):
    path = agent_dir / "inbox.cursor"
    return int(path.read_text()) if path.exists() else 0


def _validate_cursor(data, cursor):
    if cursor < 0 or cursor > len(data) or (cursor and data[cursor - 1:cursor] != b"\n"):
        raise ValueError("Invalid inbox cursor; refusing to skip messages")


def _messages(agent_dir):
    path = agent_dir / 'inbox.jsonl'
    data = path.read_bytes() if path.exists() else b''
    cursor = _cursor(agent_dir)
    _validate_cursor(data, cursor)
    offset = 0
    messages = []
    for line in data.splitlines(keepends=True):
        if not line.endswith(b'\n'):
            break
        offset += len(line)
        messages.append((offset, json.loads(line)))
    return data, cursor, messages


def _ledger_damage(path, reason):
    return ValueError(f'{path}: {reason}; back up and repair or reset the file, '
                      'then check delivery before retrying')


def _ledger(path):
    try:
        return records.runtime(path)
    except records.RecordError as error:
        raise _ledger_damage(error.file, error.reason) from error


def _states(agent_dir):
    path = agent_dir / 'inbox-state.json'
    states = _ledger(path)
    for identifier, state in states.items():
        if (not isinstance(state, dict)
                or ('submission_state' in state and (not isinstance(state['submission_state'], str)
                    or state['submission_state'] not in ('pending', 'submitting', 'accepted', 'unknown', 'failed', 'cancelled', 'manual', 'launch-only')))
                or (state.get('native_receipt') is not None and not isinstance(state['native_receipt'], dict))
                or ('attempts' in state and (not isinstance(state['attempts'], list)
                    or any(not isinstance(attempt, dict) for attempt in state['attempts'])))):
            raise _ledger_damage(path, f'invalid delivery tracking for {identifier!r}')

    # Retired per-message read markers have no effect on delivery or read position.
    states = {identifier: {key: value for key, value in state.items() if key != 'acknowledged_at'}
              for identifier, state in states.items()}
    states = {identifier: state for identifier, state in states.items() if state}
    legacy = agent_dir / 'native-delivery.json'
    if legacy.exists():
        for identifier, attempts in _ledger(legacy).items():
            if (not isinstance(attempts, list) or any(not isinstance(attempt, dict) for attempt in attempts)
                    or any(attempt.get('state') not in ('native_queued', 'ok', 'error', 'unavailable', 'unknown', 'pending')
                           for attempt in attempts)):
                raise _ledger_damage(legacy, f'invalid delivery attempts for {identifier!r}')
            if identifier not in states and attempts:
                last = attempts[-1]
                states[identifier] = {'attempts': attempts, 'target_session': last.get('native_session_id'),
                    'submission_state': {'native_queued': 'accepted', 'error': 'unknown'}.get(last['state'], 'failed')}
    for state in states.values():
        last = state.get('last_receipt') or (state.get('attempts') or [{}])[-1]
        value = last.get('value') if isinstance(last, dict) else None
        final = isinstance(value, dict) and value.get('launch_only') is True and value.get('final') is True
        if state.get('submission_state') == 'failed' and final:
            state.update(submission_state='launch-only', final_reason='launch-only')
        elif state.get('submission_state') == 'launch-only' and not final:
            # Earlier writers also finalized temporary fallbacks. Re-evaluate
            # those definite refusals against current policy, without a write.
            state['submission_state'] = 'failed'
            state.pop('final_reason', None)
    for proof in (agent_dir / 'inbox-unsubmitted').glob('*.json'):
        failed = _ledger(proof)
        identifier = failed.get('message_id')
        if (failed.get('schema') != 1 or identifier != proof.stem
                or not isinstance(identifier, str) or not isinstance(failed.get('reason'), str)):
            raise _ledger_damage(proof, 'invalid pre-submission failure record')
        previous = states.get(identifier, {})
        if previous.get('submission_state', 'pending') in ('pending', 'failed'):
            receipt = {'state': 'error', 'ok': False, 'reason': failed['reason'],
                       'value': {'pre_submission_failure': True}}
            states[identifier] = {**previous, 'submission_state': 'failed',
                                  'attempts': previous.get('attempts') or [receipt]}
    return states


def delivery_problem(agent_dir, name, *, is_final=None):
    """Count definite failures in real inbox messages, without native queries.

    Healthy empty ledgers need no history scan. Damage stays an inbox diagnostic,
    rather than making the agent's identity or terminal record unusable.
    """
    if not any((agent_dir / entry).exists() for entry in ('inbox-state.json', 'native-delivery.json', 'inbox-unsubmitted')):
        return None
    with lock(agent_dir / '.inbox.lock'):
        states = _states(agent_dir)
        failed = {identifier for identifier, state in states.items()
                  if state.get('submission_state') == 'failed'}
        if not failed:
            return None
        _, _, messages = _messages(agent_dir)
        count = sum(message.get('schema') == 2 and message.get('delivery') in ('steer', 'follow-up')
                    and message.get('id') in failed
                    and not (is_final and is_final(message, states[message['id']]))
                    for _, message in messages)
    return f'{count} {"message" if count == 1 else "messages"} not delivered: mv2 deliver {name} --retry' if count else None


def preflight_delivery(agent_dir):
    """No native request until its delivery ledger is readable."""
    with lock(agent_dir / '.inbox.lock'):
        _states(agent_dir)


def record_unsubmitted(agent_dir, message_id, reason):
    """Durable negative proof for a newly saved message whose preflight failed.

    Never inferred for old messages. Removed durably before any later native
    submission, so deleting inbox-state.json cannot revive this proof afterward.
    """
    with lock(agent_dir / '.inbox.lock'):
        _, _, messages = _messages(agent_dir)
        if not any(message.get('id') == message_id for _, message in messages):
            raise ValueError('Message does not belong to this recipient inbox')
        write(agent_dir / 'inbox-unsubmitted' / (message_id + '.json'),
              {'schema': 1, 'message_id': message_id, 'at': now(), 'reason': reason})


def inbox_problem(agent_dir):
    """Doctor reports damage without consuming or rewriting the message log."""
    try:
        with lock(agent_dir / '.inbox.lock'):
            data, _, _ = _messages(agent_dir)
            _states(agent_dir)
            if data and not data.endswith(b'\n'):
                return (f"{agent_dir / 'inbox.jsonl'}: incomplete final line; sends cannot append. "
                        "Back up the file, then repair or remove only the incomplete tail; keep complete messages")
    except (OSError, ValueError, TypeError, KeyError) as error:
        return f"{agent_dir}: inbox cannot be read: {error}"
    return None


def _manual(message):
    # Existing logs retain their old mailbox semantics; no live migration.
    return message.get('schema') != 2 or message['delivery'] == 'inbox-only'


def inbox(agent_dir, limit=50, *, after=None, all_messages=False, peek=False,
          last=None, since=None, present=None):
    """Read unread manual messages; peek and full history do not consume them."""
    if peek and (all_messages or after is not None):
        raise ValueError('--peek cannot be combined with --all or --after')
    if (last is not None or since is not None) and not all_messages:
        raise ValueError('--last and --since require --all')
    if last is not None and (last <= 0 or after is not None):
        raise ValueError('--last must be positive and cannot be combined with --after')
    if limit is not None and limit <= 0:
        raise ValueError('Inbox limit must be positive')
    agent_dir = Path(agent_dir)
    with lock(agent_dir / '.inbox.lock'):
        data, read_cursor, records = _messages(agent_dir)
        states = _states(agent_dir)
        if after is not None:
            _validate_cursor(data, after)
            if not all_messages and after > read_cursor:
                raise ValueError('Ordinary inbox reads advance automatically; use --all to browse ahead')
        start = (after or 0) if all_messages else read_cursor
        def recent(message):
            if since is None:
                return True
            try:
                timestamp = datetime.fromisoformat(message.get('at', ''))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                return timestamp >= since
            except (ValueError, TypeError):
                return False  # Undated legacy records cannot match a time filter.
        if last is not None:
            records = [(offset, message) for offset, message in records if recent(message)][-last:]
        cursor = start
        messages, cursors = [], []
        for offset, message in records:
            if offset <= start:
                continue
            if limit is not None and len(messages) >= limit:
                break
            cursor = offset
            if not recent(message):
                continue
            state = states.get(message['id'], {})
            if all_messages:
                messages.append({**message, 'tracking': {'submission_state': 'manual' if _manual(message) else 'pending', **state}})
            elif _manual(message):
                messages.append(message)
            else:
                continue
            cursors.append(offset)
        result = {'messages': messages, 'cursor': cursor}
        if after is not None:
            result.update(start_cursor=start, read_cursor=read_cursor, message_cursors=cursors,
                          has_more=b'\n' in data[cursor:])
        # A CLI reader presents and flushes while holding the inbox lock. Failed
        # output leaves the page unread; concurrent consumers cannot overtake it.
        if present is not None:
            present(result)
        consumed = not all_messages and not peek and cursor > read_cursor
        if consumed:
            write(agent_dir / 'inbox.cursor', cursor)
    if consumed and messages:
        # Inbox-only messages read and marked read, after a successful present.
        _event(agent_dir, "message.read", {"count": len(messages)})
    return result


def message_record(agent_dir, message_id):
    agent_dir = Path(agent_dir)
    with lock(agent_dir / '.inbox.lock'):
        _, _, records = _messages(agent_dir)
        message = next((m for _, m in records if m['id'] == message_id), None)
        if message is None:
            raise ValueError('Message does not belong to this recipient inbox')
        return message, _states(agent_dir).get(message_id, {})


def acceptance_contexts(agent_dir, native_id, home, body):
    """Snapshot acceptances and their exact receipts together before matching transcript rows."""
    agent_dir = Path(agent_dir)
    with lock(agent_dir / '.inbox.lock'):
        cohort = []
        _, _, messages = _messages(agent_dir)
        bodies = {message['id']: message.get('text') for _, message in messages}
        states = _states(agent_dir)
        for identifier, state in states.items():
            attempts = state.get('attempts') or []
            if (state.get('submission_state') != 'accepted'
                    and not any(attempt.get('state') in ('ok', 'native_queued') for attempt in attempts)):
                continue
            saved = state.get('delivery_context')
            target = (saved.get('native_id') if isinstance(saved, dict) else state.get('target_session')
                      or next((attempt.get('native_session_id') for attempt in reversed(attempts)
                               if attempt.get('state') in ('ok', 'native_queued')), None))
            if target is not None and target != native_id:
                continue
            if target is None or not isinstance(saved, dict) or not all(isinstance(saved.get(key), str)
                                                      for key in ('home', 'text', 'since')):
                other_body = bodies.get(identifier)
                if isinstance(other_body, str) and other_body.strip() != body.strip():
                    continue  # Equal framed text requires equal bodies even before snapshots existed.
                cohort.append((identifier, None, ''))  # Unknown framing blocks text proof, never exact-ID proof.
                continue
            if saved['home'] == home:
                cohort.append((identifier, saved['text'], saved['since']))
        receipts = {identifier: receipt for identifier, _, _ in cohort
                    if isinstance(receipt := states[identifier].get('native_receipt'), dict)
                    and receipt.get('transcript_confirmed') is True}
        return tuple(cohort), receipts


def update_message(agent_dir, message_id, **changes):
    """Merge delivery evidence under the recipient inbox lock."""
    agent_dir = Path(agent_dir)
    with lock(agent_dir / '.inbox.lock'):
        _, _, records = _messages(agent_dir)
        if not any(m['id'] == message_id for _, m in records):
            raise ValueError('Message does not belong to this recipient inbox')
        states = _states(agent_dir)
        before = states.get(message_id, {})
        states[message_id] = after = {**before, **changes}
        write(agent_dir / 'inbox-state.json', states)
        if after.get('submission_state') in ('submitting', 'accepted', 'unknown', 'cancelled', 'launch-only'):
            proof = agent_dir / 'inbox-unsubmitted' / (message_id + '.json')
            if proof.exists():
                proof.unlink()
                # Do not let a crash resurrect a negative proof after submission.
                descriptor = os.open(proof.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    # Transitions only, each once: accepted, then (maybe later) confirmed in the transcript; or a definite failure.
    if after.get('submission_state') == 'accepted' and before.get('submission_state') != 'accepted':
        _event(agent_dir, "message.delivered", {"id": message_id, "stage": "accepted"})
    if after.get('transcript_confirmed_at') and not before.get('transcript_confirmed_at'):
        _event(agent_dir, "message.delivered", {"id": message_id, "stage": "confirmed"})
    if after.get('submission_state') == 'failed' and before.get('submission_state') != 'failed':
        attempts = after.get('attempts') or [{}]
        reason = attempts[-1].get('reason') if isinstance(attempts[-1], dict) else None
        _event(agent_dir, "message.failed", {"id": message_id, "reason": reason})
    return after


def claim_cancelled_report(agent_dir, message_id):
    """Claim one final cancellation report even when checkers read the same snapshot."""
    agent_dir = Path(agent_dir)
    with lock(agent_dir / '.inbox.lock'):
        states = _states(agent_dir)
        current = states.get(message_id, {})
        if current.get('submission_state') != 'cancelled' or current.get('cancelled_reported_at'):
            return None
        states[message_id] = claimed = {**current, 'cancelled_reported_at': now()}
        write(agent_dir / 'inbox-state.json', states)
        return claimed


def finish_wake(directory, launch_id, message_id, started, reason='', *, uncertain=False, prompt_context=None):
    """A CLI prompt has no native receipt ID; record process-start evidence only."""
    if not message_id:
        return
    with lock(directory / '.delivery.lock'):
        _, tracking = message_record(directory, message_id)
        if (tracking.get('wake_launch_id') != launch_id or
                tracking.get('submission_state') not in ('submitting', 'unknown')):
            return
        if uncertain:
            reason += '\nStarting prompt consumption is unconfirmed; no automatic resend'
        receipt = {'state': 'unknown' if uncertain else 'ok' if started else 'error', 'ok': started, 'at': now(),
                   'reason': ('Started the resumed session with this message as its interactive prompt; '
                              'consumption is unconfirmed') if started else reason,
                   'launch_id': launch_id, 'value': {'transcript_confirmed': None}}
        update_message(directory, message_id, submission_state='unknown' if uncertain else 'accepted' if started else 'failed',
                       attempts=[*tracking.get('attempts', [])[:-1], receipt], native_receipt=receipt['value'],
                       **({'wake_prompt_context': prompt_context} if prompt_context is not None else {}))


def end_startup_messages(directory, launch_id, reason, cancelled=False):
    """Caller holds .launch.lock; these messages have never reached a harness."""
    if launch_id is None:
        return 0
    count = 0
    for message in inbox(directory, all_messages=True, limit=None)['messages']:
        tracking = message['tracking']
        if tracking.get('deferred_launch_id') == launch_id and tracking['submission_state'] == 'pending':
            receipt = {'state': 'unavailable', 'ok': False, 'reason': reason, 'at': now()}
            update_message(directory, message['id'], submission_state='cancelled' if cancelled else 'failed',
                           attempts=[*tracking.get('attempts', []), receipt], last_receipt=receipt)
            count += 1
    return count
