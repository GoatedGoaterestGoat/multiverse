"""Check deliveries, retry definite failures or dismiss them without resending."""
import sys
from .. import display, harnesses, store, universe
from ..runtime import native
from ..universe import agents, focus, mailbox
from ._common import delivery_summary
from ._messaging import delivery_line


def configure(parser):
    parser.add_argument('agent')
    parser.add_argument('--json', action='store_true', help='Return the original delivery evidence as JSON')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true',
                      help='Report definite failures and reconcile pending, uncertain or accepted deliveries, without submitting')
    mode.add_argument('--dismiss', action='store_true', help='Make definite delivery failures final without resending; keep them in history')
    mode.add_argument('--retry', action='store_true', help='Retry definite failures; accepted or uncertain submissions are never resent')


def run(root=None, *, agent, retry=False, check=False, dismiss=False, json=False, _present=None, _present_error=None):
    if sum(bool(value) for value in (retry, check, dismiss)) != 1:
        raise ValueError('Choose exactly one of --check, --retry or --dismiss')
    root = universe.resolve(root)
    directory = agents.directory(root, agent)
    properties = store.read(directory / 'properties.json')
    name = properties['name']
    if dismiss:
        with store.lock(directory / '.send.lock'), store.lock(directory / '.launch.lock'), store.lock(directory / '.delivery.lock'):
            dismissed = mailbox.dismiss_failures(directory)
        if _present:
            _present(f'{name}: {len(dismissed)} delivery failures dismissed; kept in history. Nothing resent.'
                     if dismissed else f'{name}: no delivery failures to dismiss.')
        return {'messages': [], 'dismissed': len(dismissed), 'ok': True}
    launch_id = agents.runtime(directory).get('launch_id')
    native_id = properties.get('native_session_id')
    history = mailbox.inbox(directory, limit=None, all_messages=True)['messages']
    held = focus.current(directory) if retry else None
    if held and any(message.get('schema') == 2 and message.get('delivery') in ('steer', 'follow-up')
                    and not message['tracking'].get('uncheckable_reported_at') and not message['tracking'].get('dismissed_at')
                    and message['tracking']['submission_state'] in ('failed', 'launch-only') for message in history):
        return _focused(name, held, [], _present_error or _present)
    results = []
    finalized = 0
    counts = {}
    details = []
    for message in history:
        if message.get('schema') != 2 or message.get('delivery') not in ('steer', 'follow-up'):
            continue
        tracking = message['tracking']
        if tracking.get('dismissed_at'):
            finalized += int(retry)
            continue
        final_lineage = bool(tracking.get('uncheckable_reported_at'))
        state = tracking['submission_state']
        if retry and final_lineage:
            finalized += 1
            continue
        if check and state == 'cancelled' and tracking.get('cancelled_reported_at'):
            continue
        if check and tracking.get('uncheckable_reported_at'):
            continue
        accepted = False
        outside_lineage = False
        try:
            # Revisit older acceptances only where the harness can still settle
            # them: a native queue receipt, or evidence for the same conversation.
            attempts = tracking.get('attempts') or [{}]
            evidence = tracking.get('native_receipt')
            confirmed = isinstance(evidence, dict) and evidence.get('transcript_confirmed') is True
            target = tracking.get('target_session') or message.get('target_session')
            outside_lineage = bool(state != 'cancelled' and not confirmed and target and target != native_id
                                   and not native._session_changes(directory, properties, target))
            accepted = check and state == 'accepted' and not confirmed and tracking.get('delivery_method') != 'resume_prompt'
            if accepted:
                same_session = bool(native_id and target == native_id)
                queued = same_session and isinstance(evidence, dict) and evidence.get('queued_submission_id') and attempts[-1].get('native_session_id') == native_id
                old_evidence = False
                if same_session and attempts[-1].get('launch_id') != launch_id and not queued:
                    adapter = harnesses.load(root, properties['harness'])
                    old_evidence = native.adapter_hook(adapter, 'accepted_message_evidence') is not None
                accepted = bool(attempts[-1].get('launch_id') == launch_id or queued or old_evidence or outside_lineage)
            if not outside_lineage and not accepted and state not in (('failed', 'launch-only') if retry else ('failed', 'pending', 'submitting', 'unknown', 'cancelled')):
                continue
            final = native.finalize_launch_only(root, directory, message['id']) if state in ('failed', 'pending') and not outside_lineage and not final_lineage else None
            if final is not None and retry:
                finalized += 1
                continue
            if outside_lineage:
                # Recheck the pointer under the launch lock before claiming a
                # once-only diagnostic; this history snapshot can predate a rebind.
                receipt = native.deliver(root, directory.name, message['id'], check=True)
            elif check and state == 'cancelled':
                claimed = mailbox.claim_cancelled_report(directory, message['id'])
                if claimed is None:
                    continue
                receipt = {**(claimed.get('last_receipt') or {}),
                           'state': 'cancelled', 'ok': False, 'reused': True}
            elif final is not None:
                receipt = final
            elif retry:
                with store.lock(directory / '.send.lock'):
                    # A focus takes the send lock to start, so one that started after the check above stops
                    # the retry here, before anything more is submitted.
                    held = focus.current(directory)
                    if held:
                        return _focused(name, held, results, _present_error or _present)
                    if state == 'launch-only' and not native.reopen_launch_only(root, directory, message['id']):
                        finalized += 1
                        continue
                    receipt = native.send_or_wake(root, directory.name, message['id'], retry=True)
            elif state == 'failed':
                previous = attempts[-1]
                receipt = {**previous, 'state': 'error', 'ok': False,
                           'reason': previous.get('reason') or 'Message was not delivered',
                           'value': {**(previous.get('value') if isinstance(previous.get('value'), dict) else {}),
                                     'definite_failure': True}}
            else:
                receipt = native.deliver(root, directory.name, message['id'], check=True)
        except (OSError, ValueError, RuntimeError) as error:
            receipt = {'state': 'error', 'reason': str(error), 'ok': False, 'value': {'check_error': True,
                       **({'repair_journal': str(error.file)} if isinstance(error, agents.sessions.JournalError) else {})}}
        if check and state != 'cancelled' and receipt.get('state') == 'cancelled':
            # Another checker may have selected the same still-accepted snapshot.
            if mailbox.claim_cancelled_report(directory, message['id']) is None:
                continue
        uncheckable = isinstance(receipt.get('value'), dict) and receipt['value'].get('outside_lineage') is True
        if uncheckable and mailbox.claim_uncheckable_report(directory, message['id']) is None:
            continue
        summary = delivery_summary(receipt)
        if retry and uncheckable:
            # Reporting a final is not a failed submission. Keep its original
            # delivery evidence, but count it with previously reported finals.
            finalized += 1
            summary['ok'] = True
        notices = display.notice_lines(receipt.get('notices'))
        if notices:
            summary['notices'] = notices
        if accepted and not uncheckable and receipt.get('state') not in ('error', 'cancelled'):
            # Missing evidence for an accepted message is not a failure.
            summary['ok'] = True
        results.append({**{key: message[key] for key in ('at', 'text', 'delivery') if key in message}, **summary})
        category = _check_state(receipt, accepted=accepted) if check else None
        if check:
            counts[category] = counts.get(category, 0) + 1
        if _present and category != 'confirmed':
            _, current = mailbox.message_record(directory, message['id'])
            present = _present_error if summary.get('ok') is False and _present_error else _present
            line = display.with_notices(delivery_line(name, receipt, delivery=message['delivery'], check=check, retry=retry, accepted=accepted,
                                  wake=current.get('delivery_method') == 'resume_prompt', text=message['text']), receipt)
            if check:
                details.append((present, line))
            else:
                present(line)
    if _present and check and results:
        order = ('confirmed', 'accepted without confirmation', 'pending', 'not delivered', 'uncertain', 'could not check')
        _present(f'{name}: ' + ', '.join(f'{counts[state]} {state}' for state in order if counts.get(state)) + '.')
        for present, line in details:
            present(line)
    if _present and finalized:
        _present(f'{name}: {finalized} earlier {"message is" if finalized == 1 else "messages are"} final' +
                 ('; nothing to retry.' if not results else '.'))
    elif _present and not results:
        _present(f'{name}: no messages to {"retry" if retry else "check"}.')
    return {'messages': results, 'ok': all(result.get('ok', True) for result in results),
            **({'finalized': finalized} if finalized else {})}


def _check_state(receipt, *, accepted):
    value = receipt.get('value')
    value = value if isinstance(value, dict) else {}
    state = receipt.get('state')
    if value.get('transcript_confirmed') is True:
        return 'confirmed'
    if value.get('check_error') or value.get('outside_lineage') or (accepted and state in ('error', 'unavailable')):
        return 'could not check'
    if state in ('cancelled', 'error', 'unavailable'):
        return 'not delivered'
    if accepted or state in ('ok', 'native_queued'):
        return 'accepted without confirmation'
    if state == 'pending':
        return 'pending'
    return 'uncertain'


def _focused(name, held, results, present):
    """A retry refused, or stopped part way, because the recipient is focused."""
    reason = f'{name} is focused {focus.ends(held)} and takes only inbox-only messages; retry after it unfocuses.'
    if present:
        present(f'{"The rest were not retried" if results else "Not retried"}: {reason}')
    return {'messages': results, 'ok': False, 'state': 'focused', 'reason': reason, 'focus': held}


def cli_run(root=None, *, json=False, **options):
    result = run(root, **options, _present=None if json else lambda line: print(line, flush=True),
                 _present_error=None if json else lambda line: print(line, file=sys.stderr, flush=True))
    if json:
        return result
    if not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
