"""Save and deliver a message to the exact native session."""
import os
from pathlib import Path
import sys
from .. import universe
from ..runtime import native
from ..store import read, lock
from ..universe import agents
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
        json=False, _present=None):
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
        sender = {"agent_id": os.environ.get("MV2_AGENT_ID"), "universe": os.environ.get("MV2_UNIVERSE"),
                  "name": "agent" if os.environ.get("MV2_AGENT_ID") else "user"}
    references = [reference.strip() for reference in agent.split(',')]
    if not all(references):
        raise ValueError('Specify an agent name between each comma')
    results, seen = [], set()
    for reference in references:
        try:
            directory = agents.directory(root, reference)
        except ValueError:
            if len(references) == 1:
                raise ValueError('Could not find one matching agent. Run mv2 list and use its name.') from None
            name = failure_reason(reference)
            result = {'ok': False, 'saved': False, 'reason': 'Could not find one matching agent'}
            line = f'Not sent to {name}: could not find one matching agent. Run mv2 list and use its name.'
        else:
            if directory in seen:
                continue  # An alias of the same recipient must not duplicate a prompt.
            seen.add(directory)
            name = read(directory / 'properties.json')['name']
            result, receipt = _send_one(root, directory, text, sender, delivery, queue_only)
            line = (f"Saved to {name}'s inbox; it sees it at its next check." if result['delivery'] == 'inbox-only'
                    else delivery_line(name, receipt, delivery=delivery, text=text))
        results.append({'agent': name, **result})
        if _present:
            _present(line)
    if len(references) == 1:
        return {key: value for key, value in results[0].items() if key != 'agent'}
    return {'recipients': results, 'ok': all(result.get('ok', True) for result in results)}


def _send_one(root, directory, text, sender, delivery, queue_only):
    with lock(directory / '.send.lock'):
        if queue_only:
            delivery = 'inbox-only'
        with lock(directory / '.launch.lock'):
            target = read(directory / 'properties.json').get('native_session_id')
            message = append_message(directory, text, sender, delivery, target_session=target)
        result = {key: message[key] for key in ('at', 'from', 'text', 'delivery')}
        if delivery == 'inbox-only':
            return ({**result, "state": "queued", "saved": True,
                    "reason": "Saved for explicit inbox reading"}, {})
        try:
            receipt = native.send_or_wake(root, directory.name, message['id'])
        except Exception as error:
            receipt = {"state": "error", "reason": str(error)}
            _, tracking = message_record(directory, message['id'])
            if tracking.get('submission_state', 'pending') == 'pending':
                update_message(directory, message['id'], submission_state='failed',
                               attempts=[*tracking.get('attempts', []), receipt])
        return ({**result, "state": receipt['state'], "saved": True, "native_delivery": delivery_summary(receipt),
                "ok": receipt['state'] not in ('error', 'unavailable', 'unknown')}, receipt)


def cli_run(root=None, *, json=False, **options):
    result = run(root, **options, _present=None if json else lambda line: print(line, flush=True))
    if json:
        return result
    if result.get('ok') is False:
        raise SystemExit(1)



if __name__ == "__main__":
    from ._common import main
    main(configure, run)
