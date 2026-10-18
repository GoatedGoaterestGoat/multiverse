"""Save and deliver a message to the exact native session."""
import os
from pathlib import Path
import sys
from .. import universe, display
from ..runtime import native
from ..store import read, lock
from ..universe import agents, records, mailbox
from ..universe.mailbox import append_message, message_record, update_message
from ._common import delivery_summary
from ._messaging import delivery_line, failure_reason

def configure(parser):
    parser.add_argument("agent", help="Agent name, or comma-separated names")
    parser.add_argument("--json", action="store_true", help="Return the original delivery records as JSON")
    message = parser.add_mutually_exclusive_group()
    message.add_argument("--text")
    message.add_argument("--file", type=Path)
    message.add_argument("--stdin", action="store_true", help="Read stdin (automatic for piped or redirected input)")
    parser.add_argument("--queue-only", action="store_true", help="Save for manual inbox reading; do not submit natively")
    parser.add_argument("--delivery", choices=("steer", "follow-up", "inbox-only"), default="steer",
                        help="Native timing: next safe boundary or the next turn")


def run(root=None, *, agent, text=None, file=None, stdin=False, sender=None, delivery="steer", queue_only=False,
        json=False, _present=None, _present_error=None):
    if delivery not in ('steer', 'follow-up', 'inbox-only'):
        raise ValueError('Delivery must be steer, follow-up, or inbox-only')
    root = universe.resolve(root)
    if file is not None:
        text = Path(file).read_text()
    elif stdin or (text is None and not sys.stdin.isatty()):
        text = sys.stdin.read()
    if text is None:
        raise ValueError("Specify --text, --file or --stdin, or pipe a message to stdin")
    if not text.strip():
        raise ValueError("Message text must not be empty")
    if sender is None:
        from ..runtime.agent_identity import sender_actor
        caller = sender_actor()
        sender = caller.sender() if caller else {"agent_id": os.environ.get("MV2_AGENT_ID"), "universe": os.environ.get("MV2_UNIVERSE"),
                  "name": "agent" if os.environ.get("MV2_AGENT_ID") else "user"}
    references = [reference.strip() for reference in agent.split(',')]
    if not all(references):
        raise ValueError('Specify an agent name between each comma')
    results, seen = [], set()
    for reference in references:
        try:
            directory = agents.directory(root, reference)
        except ValueError as error:
            damaged = isinstance(error, (records.RecordError, agents.AmbiguousAgent))
            if len(references) == 1:
                if damaged:
                    raise  # The target is known; preserve its named record error.
                raise ValueError('Could not find one matching agent. Run mv2 list and use its name.') from None
            name = display.clean(reference) if damaged else failure_reason(reference)
            reason = display.clean(str(error)) if damaged else 'Could not find one matching agent'
            result = {'ok': False, 'saved': False, 'reason': reason}
            line = (f'Not sent to {name}: {reason}.' if damaged else
                    f'Not sent to {name}: could not find one matching agent. Run mv2 list and use its name.')
        else:
            if directory in seen:
                continue  # An alias of the same recipient must not duplicate a prompt.
            seen.add(directory)
            name = read(directory / 'properties.json')['name']
            result, receipt = _send_one(root, directory, text, sender, delivery, queue_only)
            running = bool(_present and receipt.get('state') in ('error', 'unavailable') and _running(directory))
            line = (f"Saved to {name}'s inbox; it sees it at its next check." if result['delivery'] == 'inbox-only'
                    else display.with_notices(delivery_line(name, receipt, delivery=delivery, text=text, running=running), receipt))
        results.append({'agent': name, **result})
        present = _present_error if result.get('ok') is False and _present_error else _present
        if present:
            present(line)
    if len(references) == 1:
        return {key: value for key, value in results[0].items() if key != 'agent'}
    return {'recipients': results, 'ok': all(result.get('ok', True) for result in results)}


def _running(directory):
    """Observe only for readable failure advice; uncertainty never changes delivery evidence."""
    try:
        return native.observe(read(directory / 'properties.json'), agents.runtime(directory))['live'] == 'alive'
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def _send_one(root, directory, text, sender, delivery, queue_only):
    with lock(directory / '.send.lock'):
        if queue_only:
            delivery = 'inbox-only'
        binding_error = None
        preflight_error = None
        if delivery != 'inbox-only':
            try:
                mailbox.preflight_delivery(directory)
            except (OSError, ValueError, TypeError) as error:
                preflight_error = str(error)
        with lock(directory / '.launch.lock'):
            properties = read(directory / 'properties.json')
            if delivery != 'inbox-only':
                try:
                    properties, _ = native.session_snapshot(root, directory, properties, agents.runtime(directory),
                                                            launch_locked=True)
                except Exception as error:
                    binding_error = str(error)
            target = properties.get('native_session_id')
            message = append_message(directory, text, sender, delivery, target_session=target)
        result = {key: message[key] for key in ('at', 'from', 'text', 'delivery')}
        if delivery == 'inbox-only':
            return ({**result, "state": "queued", "saved": True,
                    "reason": "Saved for explicit inbox reading"}, {})
        if preflight_error:
            reason = f"{directory / 'inbox-state.json'}: {preflight_error}"
            mailbox.record_unsubmitted(directory, message['id'], reason)
            receipt = {'state': 'error', 'ok': False, 'reason': reason,
                       'value': {'pre_submission_failure': True}}
            return ({**result, 'state': 'error', 'saved': True, 'ok': False,
                     'native_delivery': delivery_summary(receipt)}, receipt)
        if binding_error:
            receipt = {'state': 'error', 'reason': binding_error}
            update_message(directory, message['id'], submission_state='failed', attempts=[receipt])
            return ({**result, 'state': 'error', 'saved': True, 'ok': False,
                     'native_delivery': delivery_summary(receipt)}, receipt)
        try:
            receipt = native.send_or_wake(root, directory.name, message['id'])
        except Exception as error:
            receipt = {"state": "error", "reason": str(error)}
            try:
                _, tracking = message_record(directory, message['id'])
                if tracking.get('submission_state', 'pending') == 'pending':
                    update_message(directory, message['id'], submission_state='failed',
                                   attempts=[*tracking.get('attempts', []), receipt])
            except (OSError, ValueError, TypeError) as damaged:
                # A later ledger failure cannot establish whether the native
                # operation ran. Save the named error, never fabricate retry proof.
                receipt = {'state': 'unknown', 'ok': False,
                           'reason': f'{directory / "inbox-state.json"}: {damaged}; submission is unconfirmed'}
        return ({**result, "state": receipt['state'], "saved": True, "native_delivery": delivery_summary(receipt),
                "ok": receipt['state'] not in ('error', 'unavailable', 'unknown'),
                **({"notices": receipt["notices"]} if receipt.get("notices") else {})}, receipt)


def cli_run(root=None, *, json=False, **options):
    result = run(root, **options, _present=None if json else lambda line: print(line, flush=True),
                 _present_error=None if json else lambda line: print(line, file=sys.stderr, flush=True))
    if json:
        return result
    if result.get('ok') is False:
        raise SystemExit(1)



if __name__ == "__main__":
    from ._common import main
    main(configure, run)
