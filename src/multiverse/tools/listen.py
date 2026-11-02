"""Wait in a background shell for unread inbox-only messages, without reading them."""
import time

from .. import universe
from ..plugins import access
from ..runtime.agent_identity import sender_actor
from ..runtime.run_files import ended
from ..store import existing_lock
from ..universe import agents, mailbox
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
    for name in ('inbox.jsonl', 'inbox.cursor'):
        try:
            info = (directory / name).stat()
            stamps.append((info.st_ino, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            stamps.append(None)
    return tuple(stamps)


def run(root=None, *, timeout=None):
    access.require('observe', 'listen for unread inbox messages')
    seconds = timeout_seconds(timeout)
    deadline = time.monotonic() + seconds if seconds is not None else None
    caller = sender_actor(acting='listen')
    if caller is None:
        raise ValueError('Run listen inside an agent session')
    if root is not None and universe.resolve(root).resolve() != caller.root.resolve():
        raise ValueError('An agent can listen only to its own inbox')
    directory = caller.directory
    # Capture before any lock wait: stable conversation authority may survive a
    # restart, but this listener must never adopt the replacement launch.
    launch_id = agents.runtime(directory).get('launch_id')
    if not launch_id:
        raise ValueError('No running launch to listen for')
    previous = None
    next_process_check = 0
    while True:
        facts = _current(directory, launch_id)
        signature = _signature(directory)
        changed = signature != previous
        # A cached observation keeps quiet polling cheap, but never authorizes
        # success: every prospective inbox read gets fresh process proof.
        if changed or time.monotonic() >= next_process_check:
            _processes(facts)
            next_process_check = time.monotonic() + 1
        # Neither lock can wait here. Inbox contention releases the launch lock
        # before the bounded sleep; launch/process checks also run on contention.
        try:
            with existing_lock(directory / '.launch.lock', blocking=False):
                current = _current(directory, launch_id)
                sender_actor(acting='listen')
                same_processes = all(current.get(key) == facts.get(key)
                    for key in ('pid', 'pid_started', 'runner_pid', 'runner_started'))
                if changed and same_processes:
                    messages = mailbox.inbox(directory, limit=None, peek=True, blocking=False)['messages']
                    previous = signature
                    if messages:
                        if len(messages) > 1:
                            return {'ok': True, 'line': f'{len(messages)} unread inbox messages'}
                        sender = messages[0].get('from')
                        name = sender.get('name') if isinstance(sender, dict) else None
                        name = ' '.join(name.split()) if isinstance(name, str) and name.strip() else 'unknown sender'
                        return {'ok': True, 'line': f'inbox message from {name}'}
        except BlockingIOError:
            pass
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            return {'ok': False, 'line': 'Timed out waiting for unread inbox messages.'}
        time.sleep(min(.2, remaining) if remaining is not None else .2)


def format_output(result):
    return result['line']


def cli_run(root=None, *, timeout=None):
    try:
        result = run(root, timeout=timeout)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    print(format_output(result), flush=True)
    if not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
