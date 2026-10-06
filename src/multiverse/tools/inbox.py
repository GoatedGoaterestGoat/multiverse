"""Read messages as sender/content blocks; use --json for scripts."""
from datetime import datetime, timedelta, timezone
import json as json_module
import re
import time
from ..universe import agents
from .. import store
from ..universe import mailbox
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--all", dest="all_messages", action="store_true", help="Browse full history, including native delivery")
    parser.add_argument("--after", type=int, help="Page --all --json history after this byte cursor")
    parser.add_argument("--peek", action="store_true", help="Show unread messages without marking them read")
    parser.add_argument("--json", action="store_true", help="Return machine-readable JSON")
    parser.add_argument("--last", type=int, help="Show the newest N messages from --all, oldest first")
    parser.add_argument("--since", help="Filter --all by ISO timestamp or duration such as 30m or 2h")
    parser.add_argument("--wait", action="store_true", help="Wait for an unread message")
    parser.add_argument("--timeout", help="Limit --wait (seconds, or a duration such as 30s or 2m)")


def since_time(value):
    if value is None:
        return None
    try:
        relative = re.fullmatch(r'(\d+(?:\.\d+)?)([smhdw])', value.strip())
        if relative:
            seconds = float(relative[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[relative[2]]
            return datetime.now(timezone.utc) - timedelta(seconds=seconds)
        timestamp = datetime.fromisoformat(value.strip())
        return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError('--since must be an ISO timestamp or duration such as 30m or 2h') from error


def run(root=None, *, agent=None, after=None, all_messages=False, peek=False,
        json=False, last=None, since=None, limit=50, wait=False, timeout=None, _present=None):
    if wait and (all_messages or after is not None or last is not None or since is not None):
        raise ValueError('--wait reads unread messages; history options cannot be combined with it')
    if timeout is not None and not wait:
        raise ValueError('--timeout requires --wait')
    seconds = timeout_seconds(timeout)
    deadline = time.monotonic() + seconds if seconds is not None else None
    root, directory, launch_id = callback(root, agent)
    def public_result(result):
        if all_messages:
            return result
        return {**result, 'messages': [{key: message[key] for key in ('at', 'from', 'text', 'delivery') if key in message}
                                      for message in result['messages']]}
    cutoff = since_time(since)
    previous = None
    result = {'messages': [], 'cursor': 0}
    class NoUnread(Exception):
        pass
    def present(value):
        nonlocal result
        result = public_result(value)
        if wait and not result['messages']:
            raise NoUnread()
        if _present:
            _present(root, result)
    def signature():
        stamps = []
        for name in ('inbox.jsonl', 'inbox.cursor'):
            try:
                facts = (directory / name).stat()
                stamps.append((facts.st_size, facts.st_mtime_ns))
            except FileNotFoundError:
                stamps.append(None)
        return tuple(stamps)
    while True:
        # Serialize output/read position with launch replacement, but release
        # both locks between polls so senders and resume can make progress.
        with store.lock(directory / '.launch.lock'):
            if launch_id and agents.runtime(directory).get('launch_id') != launch_id:
                raise ValueError('Outdated launch')
            if wait and not (directory / 'properties.json').is_file():
                raise ValueError('Agent is no longer available at this location')
            current = signature() if wait else None
            if not wait or current != previous:
                try:
                    value = mailbox.inbox(directory, limit=limit, after=after, all_messages=all_messages, peek=peek,
                                        last=last, since=cutoff, present=present)
                except NoUnread:
                    previous = current
                else:
                    return public_result(value)
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            return {**result, 'timed_out': True, 'ok': False}
        time.sleep(min(.2, remaining) if remaining is not None else .2)


def timeout_seconds(value):
    if value is None:
        return None
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([smhdw]?)', str(value).strip())
    if match is None:
        raise ValueError('--timeout must be seconds or a duration such as 30s or 2m')
    seconds = float(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[match[2]]
    if not seconds < float('inf'):
        raise ValueError('--timeout is too large')
    return seconds


def readable(root, result):
    from ..runtime.native import framed_text
    blocks = []
    for message in result['messages']:
        sender = message.get('from') or {}
        if not sender.get('agent_id'):
            sender = {**sender, 'name': 'user'}
        blocks.append(framed_text(root, {**message, 'from': sender}))
    return '\n\n'.join(blocks) if blocks else 'No unread messages.'


def cli_run(root=None, *, agent=None, after=None, all_messages=False, peek=False,
            json=False, last=None, since=None, wait=False, timeout=None):
    if after is not None and not (json and all_messages):
        raise ValueError('--after requires --all --json')
    def present(root, result):
        text = json_module.dumps(result, indent=2, ensure_ascii=False) if json else readable(root, result)
        print(text, flush=True)
    result = run(root, agent=agent, after=after, all_messages=all_messages, peek=peek,
        last=last, since=since, limit=None if last is not None or (all_messages and not json) else 50,
        wait=wait, timeout=timeout, _present=present)
    if result.get('timed_out'):
        print(json_module.dumps(result, indent=2, ensure_ascii=False) if json else
              'Timed out waiting for unread messages.', flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
