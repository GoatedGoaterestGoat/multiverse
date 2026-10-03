"""Read unread inbox-only messages and advance the read position."""
from .. import agents, store
from ._common import add_agent, callback

def configure(parser):
    add_agent(parser)
    parser.add_argument("--all", dest="all_messages", action="store_true", help="Browse raw full history, including native delivery")
    parser.add_argument("--after", type=int, help="Browse --all history after this byte cursor")
    parser.add_argument("--peek", action="store_true", help="Show unread messages without marking them read")


def run(root=None, *, agent=None, after=None, all_messages=False, peek=False):
    root, directory, launch_id = callback(root, agent)
    # Serialize the read-position update with launch replacement. A reader
    # validated before resume must not consume the replacement launch's inbox.
    with store.lock(directory / '.launch.lock'):
        if launch_id and agents.runtime(directory).get('launch_id') != launch_id:
            raise ValueError('Outdated launch')
        result = store.inbox(directory, after=after, all_messages=all_messages, peek=peek)
    if not all_messages:
        result['messages'] = [{key: message[key] for key in ('at', 'from', 'text', 'delivery') if key in message}
                              for message in result['messages']]
    return result


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
