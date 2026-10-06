"""Check uncertain native deliveries or retry a recipient's definite failures."""
from .. import store, universe
from ..runtime import native
from ..universe import agents, mailbox
from ._common import delivery_summary
from ._messaging import delivery_line


def configure(parser):
    parser.add_argument('agent')
    parser.add_argument('--json', action='store_true', help='Return the original delivery evidence as JSON')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true',
                      help='Reconcile pending or uncertain deliveries and record receipts for accepted ones, without submitting')
    mode.add_argument('--retry', action='store_true', help='Retry definite failures; accepted or uncertain submissions are never resent')


def run(root=None, *, agent, retry=False, check=False, json=False, _present=None):
    if retry == check:
        raise ValueError('Choose exactly one of --check or --retry')
    root = universe.resolve(root)
    directory = agents.directory(root, agent)
    name = store.read(directory / 'properties.json')['name']
    launch_id = agents.runtime(directory).get('launch_id')
    results = []
    for message in mailbox.inbox(directory, limit=None, all_messages=True)['messages']:
        if message.get('schema') != 2 or message.get('delivery') not in ('steer', 'follow-up'):
            continue
        tracking = message['tracking']
        state = tracking['submission_state']
        # An accepted submission can gain receipt evidence while the launch that
        # took it runs; check it until one is recorded. A resume prompt has no
        # receipt to correlate.
        attempts = tracking.get('attempts') or [{}]
        accepted = (not retry and state == 'accepted' and tracking.get('native_receipt') is None
                    and tracking.get('delivery_method') != 'resume_prompt'
                    and attempts[-1].get('launch_id') == launch_id)
        if not accepted and state not in (('failed',) if retry else ('pending', 'submitting', 'unknown')):
            continue
        try:
            if retry:
                with store.lock(directory / '.send.lock'):
                    receipt = native.send_or_wake(root, directory.name, message['id'], retry=True)
            else:
                receipt = native.deliver(root, directory.name, message['id'], check=True)
        except (OSError, ValueError, RuntimeError) as error:
            receipt = {'state': 'error', 'reason': str(error), 'ok': False}
        summary = delivery_summary(receipt)
        if accepted and receipt.get('state') != 'error':
            # Missing evidence for an accepted message is not a failure.
            summary['ok'] = True
        results.append({**{key: message[key] for key in ('at', 'text', 'delivery') if key in message}, **summary})
        if _present:
            _, current = mailbox.message_record(directory, message['id'])
            _present(delivery_line(name, receipt, delivery=message['delivery'], check=check, accepted=accepted,
                                  wake=current.get('delivery_method') == 'resume_prompt', text=message['text']))
    if _present and not results:
        _present(f'{name}: no messages to {"retry" if retry else "check"}.')
    return {'messages': results, 'ok': all(result.get('ok', True) for result in results)}


def cli_run(root=None, *, json=False, **options):
    result = run(root, **options, _present=None if json else lambda line: print(line, flush=True))
    if json:
        return result
    if not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
