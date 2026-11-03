"""Save and deliver a message to the exact native session."""
import os
from pathlib import Path
import sys
from ..plugins import access
from .. import universe, display
from ..runtime import native, missing_runtime
from ..terminal.recovery import Unreachable
from ..store import lock, read
from ..universe import agents, focus, records, mailbox
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
        json=False, _present=None, _present_error=None, _note_for=None):
    """`_note_for` is a focus id: mv2's own note about that focus's held messages, sent only while no newer
    focus has begun."""
    access.require('act', f'send to {agent}')
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
            result, receipt = _send_one(root, directory, text, sender, delivery, queue_only, _note_for)
            running = bool(_present and receipt.get('state') in ('error', 'unavailable') and _running(directory))
            if result['state'] == 'held':
                line = f"{name} is currently focused. Your message has been delivered as an inbox message instead."
            elif result['state'] == 'skipped':
                line = f"Not sent to {name}: {result['reason']}."
            elif result['delivery'] == 'inbox-only':
                line = f"Saved to {name}'s inbox; it sees it at its next check."
            else:
                line = display.with_notices(delivery_line(name, receipt, delivery=delivery, text=text, running=running), receipt)
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


def _skipped(sender, text, delivery, reason):
    return ({'from': sender, 'text': text, 'delivery': delivery, 'state': 'skipped', 'saved': False, 'ok': True,
             'reason': reason}, {})


def _send_one(root, directory, text, sender, delivery, queue_only, note_for=None):
    with lock(directory / '.send.lock'):
        if queue_only:
            delivery = 'inbox-only'
        # A focus starts only under the send lock, so a newer one cannot begin between this check and the send.
        if note_for is not None and (focus.current(directory) or (focus.last(directory) or {}).get('id') != note_for):
            return _skipped(sender, text, delivery, 'a newer focus began')
        held = focus.hold(directory, lambda focus_id: append_message(directory, text, sender, 'inbox-only', intended=delivery,
                                                                     focus=focus_id)) if delivery != 'inbox-only' and note_for is None else None
        if held:
            # Saved as inbox-only, so nothing reaches the harness while the agent waits on the user.
            record, message = held
            return ({**{key: message[key] for key in ('at', 'from', 'text', 'delivery', 'intended')}, 'state': 'held',
                     'saved': True, 'ok': True, 'focus': record,
                     'reason': 'Focused, so saved as an inbox-only message'}, {})
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
                    facts = agents.runtime(directory)
                    from ..runtime.navigation import pending
                    # Preserve the saved target while send_or_wake independently
                    # discovers terminal ownership; damaged facts cannot reconcile.
                    if (not pending(facts) and missing_runtime.damage(directory, facts) is None
                            and missing_runtime.mismatch(directory, facts, native.observe(properties, facts)) is None):
                        properties, _ = native.session_snapshot(root, directory, properties, facts,
                                                                launch_locked=True)
                except access.Denied:
                    raise
                except Exception as error:
                    binding_error = error
            target = properties.get('native_session_id')
            if note_for is not None:
                # Under the focus lock, the note is saved and then the report committed to it: a note that is never
                # saved claims nothing, and a listener that reported first keeps it unsaid. The two are ordered
                # writes, not one. After a crash between them, the saved note, which names its focus, becomes the
                # report (focus.reconcile_locked) before another note is saved or a listener reports; it is never
                # sent again.
                # Counted under the launch lock too, which mv2 inbox and mv2 held take to read, so the note names
                # only what is still unread when it is saved.
                with focus.locked(directory):
                    if (focus.reconcile_locked(directory, note_for) or {}).get('report'):
                        return _skipped(sender, text, delivery, 'the agent was told already')
                    count = len(mailbox.held(directory, note_for, peek=True)['messages'])
                    if not count:
                        return _skipped(sender, text, delivery, 'its held messages were read')
                    message = append_message(directory, focus.note(count), sender, delivery, target_session=target,
                                             focus_note=note_for)
                    focus.commit_note(directory, note_for, message['id'])
            else:
                message = append_message(directory, text, sender, delivery, target_session=target)
        result = {key: message[key] for key in ('at', 'from', 'text', 'delivery')}
        if delivery == 'inbox-only':
            return ({**result, "state": "queued", "saved": True,
                    "reason": "Saved for explicit inbox reading"}, {})
        if preflight_error:
            reason = (preflight_error if str(directory) in preflight_error else
                      f"{directory / 'inbox-state.json'}: {preflight_error}")
            mailbox.record_unsubmitted(directory, message['id'], reason)
            receipt = {'state': 'error', 'ok': False, 'reason': reason,
                       'value': {'pre_submission_failure': True}}
            return ({**result, 'state': 'error', 'saved': True, 'ok': False,
                     'native_delivery': delivery_summary(receipt)}, receipt)
        if binding_error:
            receipt = {'state': 'error', 'reason': str(binding_error),
                       'value': {'repair_terminal': True} if isinstance(binding_error, Unreachable) else None}
            update_message(directory, message['id'], submission_state='failed', attempts=[receipt])
            return ({**result, 'state': 'error', 'saved': True, 'ok': False,
                     'native_delivery': delivery_summary(receipt)}, receipt)
        try:
            receipt = native.send_or_wake(root, directory.name, message['id'], new_message=True)
        except access.Denied:
            raise
        except Exception as error:
            receipt = {"state": "error", "reason": str(error),
                       'value': {'repair_terminal': True} if isinstance(error, Unreachable) else None}
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
