"""Wait in a background shell for unread inbox-only messages, without reading them."""
from contextlib import ExitStack
from datetime import datetime, timezone
import time

from .. import universe
from ..plugins import access
from ..runtime.agent_identity import sender_actor
from ..runtime.run_files import ended
from ..store import existing_lock
from ..universe import agents, focus, mailbox
from .inbox import timeout_seconds


def configure(parser):
    parser.add_argument('--timeout', help='Stop waiting after seconds or a duration such as 30s or 2m')


def _current(directory, launch_id):
    facts = agents.runtime(directory)
    if facts.get('launch_id') != launch_id:
        raise ValueError('Listening launch ended or was replaced')
    if (facts.get('state') in ('ending', 'stopped', 'dormant', 'failed') or facts.get('ending_at')
            or facts.get('stopped_at') or facts.get('exit_code') is not None):
        raise ValueError('Listening launch has ended')
    return facts


def _processes(facts):
    # Observe the frontend and its runner, never the shared native daemon. A
    # listener must also end if a crash left runtime.json saying "running".
    for pid, started in (('pid', 'pid_started'), ('runner_pid', 'runner_started')):
        gone = ended({'pid': facts.get(pid), 'started': facts.get(started)})
        if gone is True:
            raise ValueError('Listening launch has ended')
        if gone is None:
            raise ValueError('Cannot verify that the listening launch is still running')


def _signature(directory):
    stamps = []
    for name in ('inbox.jsonl', 'inbox.cursor', 'inbox-state.json', 'inbox-read.json'):
        try:
            info = (directory / name).stat()
            stamps.append((info.st_ino, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            stamps.append(None)
    return tuple(stamps)


def run(root=None, *, timeout=None, _present=None):
    """Wait for unread inbox-only messages. A focus that begins meanwhile holds the report, and the timeout,
    until it ends.

    `_present(result)` prints the line. It runs under the focus lock, after the last check that no focus is
    in effect, so a focus that starts meanwhile waits until the line is out. A report of held messages is
    claimed only once that print returned, so a print that failed leaves the turn-end note to be sent.
    """
    access.require('observe', 'listen for unread inbox messages')
    seconds = timeout_seconds(timeout)
    deadline = time.monotonic() + seconds if seconds is not None else None
    caller = sender_actor(acting='listen')
    if caller is None:
        raise ValueError('Run listen inside an agent session')
    if root is not None and universe.resolve(root).resolve() != caller.root.resolve():
        raise ValueError('An agent can listen only to its own inbox')
    directory = caller.directory
    # Before the check: any focus that is not refused here began after this.
    started = datetime.now(timezone.utc)
    if focus.current(directory):
        raise ValueError('You are focused; end focus mode first')
    # Capture before any lock wait: stable conversation authority may survive a
    # restart, but this listener must never adopt the replacement launch.
    launch_id = agents.runtime(directory).get('launch_id')
    if not launch_id:
        raise ValueError('No running launch to listen for')
    with focus.listening(directory) as registration:
        return _wait(directory, launch_id, deadline, started, registration, _present or (lambda result: None))


def _wait(directory, launch_id, deadline, started, registration, present):
    previous = None
    # A focus was seen since the last read: what it held is read, with fresh process proof, before any timeout.
    owed = False
    next_process_check = 0
    while True:
        facts = _current(directory, launch_id)
        registration.register()
        # Only a polling hint: the focus lock below always takes a fresh retrying read before output.
        focused = focus.hint(directory) is not None
        owed = owed or focused
        # Nothing is read during a focus, so `previous` keeps the last read from before it: whatever arrived
        # or was read during the focus counts as a change once it ends.
        wanted = not focused and (owed or _signature(directory) != previous)
        # A cached observation keeps quiet polling cheap, but never authorizes
        # success: every prospective inbox read gets fresh process proof.
        if wanted or time.monotonic() >= next_process_check:
            _processes(facts)
            next_process_check = time.monotonic() + 1
        expired = deadline is not None and time.monotonic() >= deadline
        # No lock can wait here: contention falls through to the bounded sleep, holding nothing, and the
        # launch, identity and process checks run again. The focus lock is held through the output, so no focus
        # begins between the last check that there is none and the line, a single short write.
        try:
            with existing_lock(directory / '.launch.lock', blocking=False):
                current = _current(directory, launch_id)
                sender_actor(acting='listen')
                same_processes = all(current.get(key) == facts.get(key)
                    for key in ('pid', 'pid_started', 'runner_pid', 'runner_started'))
                if wanted and same_processes:
                    with ExitStack() as fence:
                        try:
                            fence.enter_context(focus.locked(directory, blocking=False))
                        except BlockingIOError:
                            pass  # maybe a focus starting: the read stays owed
                        else:
                            if focus.current(directory):
                                owed = True
                            else:
                                signature = _signature(directory)
                                if owed or signature != previous:
                                    messages = mailbox.inbox(directory, limit=None, peek=True, blocking=False)['messages']
                                    report = _report(directory, messages, started) if messages else None
                                    # Only a read that got this far counts: a busy lookup in _report reads again.
                                    previous, owed = signature, False
                                    if report:
                                        result, claim = report
                                        present(result)
                                        if claim:
                                            focus.claim_locked(directory, claim, 'listener')
                                        return result
        except BlockingIOError:
            # A busy launch or inbox lock: the read after the focus was tried. It comes on a later poll, since
            # `previous` is still from before the focus, and meanwhile an expired timeout may finish.
            owed = owed and not wanted
        if expired and not owed:
            # A timeout that runs out during a focus waits for it to end and for one try at reading after it;
            # a busy focus lock may be a focus starting. Busy launch or inbox locks never hold it back.
            try:
                with focus.locked(directory, blocking=False):
                    if focus.current(directory):
                        owed = True
                    else:
                        result = {'ok': False, 'line': 'Timed out waiting for unread inbox messages.'}
                        present(result)
                        return result
            except BlockingIOError:
                pass
        remaining = deadline - time.monotonic() if deadline is not None else None
        time.sleep(min(.2, remaining) if remaining is not None and remaining > 0 else .2)


def _report(directory, messages, started):
    """The result for unread messages, naming those the latest focus held, and the focus whose report to claim
    once it is printed; None when nothing is left to report. The caller holds the focus lock."""
    latest = focus.last(directory)
    held = [message for message in messages
            if latest and message.get('focus') == latest['id'] and message.get('intended')]
    if held and not latest.get('report'):
        # A note saved without its claim is still the report; the inbox lock is not waited on.
        latest = focus.reconcile_locked(directory, latest['id'], blocking=False) or latest
    began = focus.began(latest) if latest else None
    if held and latest.get('report') in ('note', 'listener') and began and began >= started:
        # The turn-end note, or another listener that waited through the same focus, already told the agent.
        messages = [message for message in messages if message not in held]
        held = []
    if not messages:
        return None
    if len(messages) > 1:
        line = f'{len(messages)} unread inbox messages'
        if held:
            line += (f' (including {len(held)} steer/follow-up {"message" if len(held) == 1 else "messages"}'
                     ' held during focus mode)')
    else:
        sender = messages[0].get('from')
        name = sender.get('name') if isinstance(sender, dict) else None
        name = ' '.join(name.split()) if isinstance(name, str) and name.strip() else 'unknown sender'
        line = f'inbox message from {name}' + (' (a steer/follow-up message held during focus mode)' if held else '')
    return {'ok': True, 'line': line}, (latest['id'] if held else None)


def format_output(result):
    return result['line']


def cli_run(root=None, *, timeout=None):
    try:
        result = run(root, timeout=timeout, _present=lambda result: print(format_output(result), flush=True))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    if not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
