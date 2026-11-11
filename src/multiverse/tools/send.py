"""Save and deliver a message to the exact native session."""
import os
from pathlib import Path
import sys
from ..plugins import access
from .. import universe, display
from ..runtime import native, missing_runtime, queued_prompts
from ..terminal.recovery import Unreachable
from ..store import lock, read, retry_missing
from ..universe import agents, blocked, focus, records, mailbox
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
    parser.add_argument("--queue-only", action="store_true", help="Save inbox-only; becomes a follow-up if the recipient reports being blocked on you")
    parser.add_argument("--delivery", choices=("steer", "follow-up", "inbox-only"), default="steer",
                        help="Native timing: next safe boundary or the next turn")
    parser.add_argument("--force", action="store_true",
                        help="Send even when the recipient's follow-ups are queued for you")


def run(root=None, *, agent, text=None, file=None, stdin=False, sender=None, delivery="steer", queue_only=False,
        force=False, json=False, _present=None, _present_error=None, _note_for=None):
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
    caller = None
    if sender is None:
        from ..runtime.agent_identity import sender_actor
        caller = sender_actor()
        sender = caller.sender() if caller else {"agent_id": os.environ.get("MV2_AGENT_ID"), "universe": os.environ.get("MV2_UNIVERSE"),
                  "name": "agent" if os.environ.get("MV2_AGENT_ID") else "user"}
    references = [reference.strip() for reference in agent.split(',')]
    if not all(references):
        raise ValueError('Specify an agent name between each comma')
    targets = []
    for reference in references:
        try:
            targets.append((reference, agents.directory(root, reference), None))
        except ValueError as error:
            targets.append((reference, None, error))
    queue_check, queued_senders, unconfirmed_senders = None, frozenset(), None
    if caller is not None and not force and not queue_only and delivery != 'inbox-only' and _note_for is None:
        queue_check, queued_senders, unconfirmed_senders = queued_prompts.for_send(caller.root, caller.identity)
    unread_senders = None
    results, seen = [], set()
    for reference, directory, error in targets:
        if error is not None:
            damaged = isinstance(error, (records.RecordError, agents.AmbiguousAgent))
            detailed = damaged or isinstance(error, agents.AgentNotFound) and bool(error.related_hint)
            if len(references) == 1:
                if detailed:
                    raise error  # Preserve record errors, ambiguity and related-universe advice.
                raise ValueError('Could not find one matching agent. Run mv2 list and use its name.') from None
            name = display.clean(reference) if detailed else failure_reason(reference)
            reason = display.clean(str(error)) if detailed else 'Could not find one matching agent'
            result = {'ok': False, 'saved': False, 'reason': reason}
            line = (f'Not sent to {name}: {reason.rstrip(".")}.' if detailed else
                    f'Not sent to {name}: could not find one matching agent. Run mv2 list and use its name.')
        else:
            if directory in seen:
                continue  # An alias of the same recipient must not duplicate a prompt.
            seen.add(directory)
            properties = read(directory / 'properties.json')
            name = properties['name']
            if directory.name in queued_senders and not force:
                clean_name = display.clean(name)
                reason = (f'Your message to {clean_name} is not delivered because you have unread messages from '
                          f'{clean_name} queued as follow-ups. You will see them once you end this turn. '
                          'It is recommended that you read '
                          f"{clean_name}'s messages first before messaging them. If you still want to message "
                          f'{clean_name}, resend with --force.')
                results.append({'agent': name, 'ok': False, 'saved': False, 'reason': reason,
                                'blocked_recipients': [clean_name]})
                if _present_error or _present:
                    (_present_error or _present)(reason)
                continue
            if unread_senders is None:
                unread_senders = _unread_senders(caller) if _present or _present_error else set()
            result, receipt = _send_one(root, directory, text, sender, delivery, queue_only, _note_for)
            running = bool(_present and result.get('delivery') != 'inbox-only'
                           and receipt.get('state') in ('error', 'unavailable') and _running(directory))
            if result.get('upgrade_damaged'):
                reason = failure_reason(receipt.get('reason'), result['text'], preserve_details=True)
                line = f'Original text is saved, but its inbox cannot be read: {reason}. Repair the inbox ledger before reading it.'
            elif result['state'] == 'held':
                line = f"{name} is currently focused. Your message has been delivered as an inbox message instead."
            elif result['state'] == 'skipped':
                line = f"Not sent to {name}: {result['reason']}."
            elif result['delivery'] == 'inbox-only':
                line = f"Saved to {name}'s inbox; it sees it at its next check."
            else:
                line = display.with_notices(delivery_line(name, receipt, delivery=result['delivery'], text=result['text'], running=running), receipt)
            if result.get('upgraded_from'):
                if result['state'] in ('held', 'pending'):
                    prefix = 'Your message was scheduled as a follow-up instead'
                elif receipt.get('state') in ('ok', 'native_queued') and receipt.get('ok') is not False:
                    prefix = 'Your message was delivered as a follow-up instead'
                else:
                    prefix = 'Tried a follow-up'
                line = f"{prefix} because {name}'s reported status is blocked on you. " + line
            elif result.get('upgrade_unconfirmed'):
                prefix = ('Your message was scheduled as a follow-up instead' if receipt.get('state') == 'pending'
                          else 'Tried a follow-up')
                line = (f"{prefix} because {name}'s reported status is blocked on you. "
                        "Delivery is not confirmed; it may also arrive as a follow-up. " + line)
            elif result.get('upgrade_failed'):
                why = failure_reason(receipt.get('reason'), result['text'])
                line = (f"Tried a follow-up because {name}'s reported status is blocked on you. "
                        f"The follow-up failed: {why}. " + line)
            elif result.get('upgrade_unavailable'):
                line = (f"{name} is blocked on you, but cannot take a follow-up in its current session. " + line)
            if properties.get('id', directory.name) in unread_senders:
                line += ('\n' if '\n' in line else ' ') + f'Reminder: you have unread messages from {name}.'
            if (queue_check is not None and (queue_check.state != 'ok' or queue_check.coverage != 'complete')
                    and (unconfirmed_senders is None or directory.name in unconfirmed_senders)):
                reason = display.clean(queue_check.reason or 'The harness queue is unknown').rstrip('.')
                note = f"mv2 couldn't check whether {name}'s follow-ups to you are still queued: {reason}."
                result['queue_check_note'] = note
                line += '\n' + note
        results.append({'agent': name, **result})
        present = _present_error if result.get('ok') is False and _present_error else _present
        if present:
            present(line)
    if len(references) == 1:
        return {key: value for key, value in results[0].items() if key != 'agent'}
    blocked_names = [name for result in results for name in result.get('blocked_recipients', [])]
    return {'recipients': results, 'ok': all(result.get('ok', True) for result in results),
            **({'blocked_recipients': blocked_names} if blocked_names else {})}


def _unread_senders(caller):
    """Optional output advice: peek once before sending, outside every delivery lock."""
    return mailbox.unread_senders(caller.directory) if caller is not None else set()


def _running(directory):
    """Observe only for readable failure advice; uncertainty never changes delivery evidence."""
    try:
        properties = retry_missing(lambda: read(directory / 'properties.json'))
        return native.observe(properties, agents.runtime(directory))['live'] == 'alive'
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def _skipped(sender, text, delivery, reason):
    return ({'from': sender, 'text': text, 'delivery': delivery, 'state': 'skipped', 'saved': False, 'ok': True,
             'reason': reason}, {})


def _send_one(root, directory, text, sender, delivery, queue_only, note_for=None):
    with lock(directory / '.send.lock'):
        if queue_only:
            delivery = 'inbox-only'
        upgraded = (delivery == 'inbox-only' and note_for is None
                    and blocked.matches(records.properties(directory / 'properties.json'), sender))
        if upgraded:
            delivery = 'follow-up'
        result, receipt = _deliver_one(root, directory, text, sender, delivery, note_for,
                                       upgraded=upgraded)
        if upgraded and result.get('saved') and (result['delivery'] == 'follow-up' or result['state'] == 'held'):
            result['upgraded_from'] = 'inbox-only'
        return result, receipt



def _upgrade_unavailable(root, directory, properties):
    """A read-only, optional classification under the launch lock; failure keeps manual input safe."""
    try:
        facts = agents.runtime(directory)
        if missing_runtime.damage(directory, facts) is not None:
            return True
        observation = native.observe(properties, facts)
        if (observation['live'] not in ('alive', 'stopped', 'dormant')
                or missing_runtime.mismatch(directory, facts, observation) is not None):
            return True
        unavailable = native.launch_only_filter(root, directory, properties, facts, observation['live'])
        return unavailable({'delivery': 'follow-up'}, {})
    except access.Denied:
        raise
    except Exception:
        return True  # No native attempt happened; keep the original inbox-only message.


def _deliver_one(root, directory, text, sender, delivery, note_for, *, upgraded=False):
    """The send lock covers the dependency decision, focus hold, save and native admission."""
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
    original_text = text
    binding_error = None
    preflight_error = None
    if delivery != 'inbox-only':
        try:
            mailbox.preflight_delivery(directory)
        except (OSError, ValueError, TypeError) as error:
            preflight_error = str(error)
    upgrade_unavailable = False
    with lock(directory / '.launch.lock'):
        properties = read(directory / 'properties.json')
        if upgraded:
            if preflight_error or _upgrade_unavailable(root, directory, properties):
                # No native attempt: keep the original inbox-only message readable. A stopped
                # launch-only agent with a supported wake still takes the normal follow-up path.
                delivery = 'inbox-only'
                upgrade_unavailable = True
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
            message = append_message(directory, text, sender, delivery, target_session=target,
                                     inbox_only_text=original_text if upgraded and delivery == 'follow-up' else None)
    result = {key: message[key] for key in ('at', 'from', 'text', 'delivery')}
    if upgrade_unavailable:
        result['upgrade_unavailable'] = True
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
        return _completed(directory, message, receipt, upgraded=upgraded)
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
    return _completed(directory, message, receipt, upgraded=upgraded)


def _completed(directory, message, receipt, *, upgraded):
    result = {key: message[key] for key in ('at', 'from', 'text', 'delivery')}
    if upgraded:
        try:
            _, tracking = message_record(directory, message['id'])
        except (OSError, ValueError, TypeError) as error:
            receipt = {'state': 'unknown', 'ok': False, 'reason': str(error)}
            return ({**result, 'state': 'unknown', 'saved': True, 'ok': False, 'upgrade_damaged': True,
                     'native_delivery': delivery_summary(receipt)}, receipt)
        if (receipt.get('state') in ('error', 'unavailable')
                and tracking.get('submission_state') in ('accepted', 'unknown', 'submitting')):
            receipt = {**receipt, 'state': 'unknown', 'ok': False,
                       'reason': 'The follow-up may have been submitted before the failure; check delivery, do not resend'}
        if mailbox.inbox_copy(message, tracking):
            flag = 'upgrade_failed' if mailbox.inbox_fallback(message, tracking) else 'upgrade_unconfirmed'
            return ({**result, 'text': message['inbox_only_text'], 'delivery': 'inbox-only', 'state': 'queued',
                     'saved': True, 'ok': True, flag: True,
                     'native_delivery': delivery_summary(receipt)}, receipt)
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
