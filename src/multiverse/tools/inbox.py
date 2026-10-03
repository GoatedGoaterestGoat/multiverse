"""Read messages as sender/content blocks; use --json for scripts."""
from datetime import datetime, timedelta, timezone
import json as json_module
import re
from .. import agents, store
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--all", dest="all_messages", action="store_true", help="Browse full history, including native delivery")
    parser.add_argument("--after", type=int, help="Page --all --json history after this byte cursor")
    parser.add_argument("--peek", action="store_true", help="Show unread messages without marking them read")
    parser.add_argument("--json", action="store_true", help="Return machine-readable JSON")
    parser.add_argument("--last", type=int, help="Show the newest N messages from --all, oldest first")
    parser.add_argument("--since", help="Filter --all by ISO timestamp or duration such as 30m or 2h")


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
        json=False, last=None, since=None, limit=50, _present=None):
    root, directory, launch_id = callback(root, agent)
    def public_result(result):
        if all_messages:
            return result
        return {**result, 'messages': [{key: message[key] for key in ('at', 'from', 'text', 'delivery') if key in message}
                                      for message in result['messages']]}
    # Serialize the read-position update with launch replacement. A reader
    # validated before resume must not consume the replacement launch's inbox.
    with store.lock(directory / '.launch.lock'):
        if launch_id and agents.runtime(directory).get('launch_id') != launch_id:
            raise ValueError('Outdated launch')
        result = store.inbox(directory, limit=limit, after=after, all_messages=all_messages, peek=peek,
                             last=last, since=since_time(since),
                             present=(lambda value: _present(root, public_result(value))) if _present else None)
    return public_result(result)


def readable(root, result):
    from ..native import framed_text
    blocks = []
    for message in result['messages']:
        sender = message.get('from') or {}
        if not sender.get('agent_id'):
            sender = {**sender, 'name': 'user'}
        blocks.append(framed_text(root, {**message, 'from': sender}))
    return '\n\n'.join(blocks) if blocks else 'No unread messages.'


def cli_run(root=None, *, agent=None, after=None, all_messages=False, peek=False,
            json=False, last=None, since=None):
    if after is not None and not (json and all_messages):
        raise ValueError('--after requires --all --json')
    def present(root, result):
        text = json_module.dumps(result, indent=2, ensure_ascii=False) if json else readable(root, result)
        print(text, flush=True)
    run(root, agent=agent, after=after, all_messages=all_messages, peek=peek,
        last=last, since=since, limit=None if last is not None or (all_messages and not json) else 50,
        _present=present)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
