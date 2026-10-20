"""Check uncertain native deliveries or retry a recipient's definite failures."""
import sys
from .. import display, store, universe
from ..runtime import native
from ..universe import agents, mailbox
from ._common import delivery_summary
from ._messaging import delivery_line


def configure(parser):
    parser.add_argument('agent')
    parser.add_argument('--json', action='store_true', help='Return the original delivery evidence as JSON')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true',
                      help='Report definite failures and reconcile pending, uncertain or accepted deliveries, without submitting')
    mode.add_argument('--retry', action='store_true', help='Retry definite failures; accepted or uncertain submissions are never resent')


def run(root=None, *, agent, retry=False, check=False, json=False, _present=None, _present_error=None):
    if retry == check:
        raise ValueError('Choose exactly one of --check or --retry')
    root = universe.resolve(root)
    directory = agents.directory(root, agent)
    name = store.read(directory / 'properties.json')['name']
    launch_id = agents.runtime(directory).get('launch_id')
    native_id = store.read(directory / 'properties.json').get('native_session_id')
    results = []
    finalized = 0
    for message in mailbox.inbox(directory, limit=None, all_messages=True)['messages']:
        if message.get('schema') != 2 or message.get('delivery') not in ('steer', 'follow-up'):
            continue
        tracking = message['tracking']
        state = tracking['submission_state']
        # An accepted submission can gain receipt evidence while the launch that
        # took it runs; check it until consumption is confirmed. A queue acceptance
        # receipt is not transcript evidence. Accepted resume prompts have no
        # native receipt ID; failed-startup uncertainty has its own evidence check.
        attempts = tracking.get('attempts') or [{}]
        evidence = tracking.get('native_receipt')
        confirmed = isinstance(evidence, dict) and evidence.get('transcript_confirmed') is True
        accepted = (not retry and state == 'accepted' and not confirmed
                    and tracking.get('delivery_method') != 'resume_prompt'
                    and (attempts[-1].get('launch_id') == launch_id
                         or isinstance(evidence, dict) and evidence.get('queued_submission_id')
                         and native_id and attempts[-1].get('native_session_id') == native_id
                         and (tracking.get('target_session') or message.get('target_session')) == native_id))
        if not accepted and state not in (('failed', 'launch-only') if retry else ('failed', 'pending', 'submitting', 'unknown', 'cancelled')):
            continue
        try:
            final = native.finalize_launch_only(root, directory, message['id']) if state in ('failed', 'pending') else None
            if final is not None and retry:
                finalized += 1
                continue
            if final is not None:
                receipt = final
            elif retry:
                with store.lock(directory / '.send.lock'):
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
            receipt = {'state': 'error', 'reason': str(error), 'ok': False}
        summary = delivery_summary(receipt)
        notices = display.notice_lines(receipt.get('notices'))
        if notices:
            summary['notices'] = notices
        if accepted and receipt.get('state') != 'error':
            # Missing evidence for an accepted message is not a failure.
            summary['ok'] = True
        results.append({**{key: message[key] for key in ('at', 'text', 'delivery') if key in message}, **summary})
        if _present:
            _, current = mailbox.message_record(directory, message['id'])
            present = _present_error if summary.get('ok') is False and _present_error else _present
            present(display.with_notices(delivery_line(name, receipt, delivery=message['delivery'], check=check, accepted=accepted,
                                  wake=current.get('delivery_method') == 'resume_prompt', text=message['text']), receipt))
    if _present and finalized:
        _present(f'{name}: {finalized} earlier {"refusal is" if finalized == 1 else "refusals are"} final' +
                 ('; nothing to retry.' if not results else '.'))
    elif _present and not results:
        _present(f'{name}: no messages to {"retry" if retry else "check"}.')
    return {'messages': results, 'ok': all(result.get('ok', True) for result in results),
            **({'finalized': finalized} if finalized else {})}


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
