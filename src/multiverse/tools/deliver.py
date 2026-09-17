"""Attempt native follow-up delivery of an existing unread durable message; never ack it."""
from .. import native, universe


def configure(parser):
    parser.add_argument('agent')
    parser.add_argument('--message-id', required=True)
    parser.add_argument('--retry', action='store_true', help='Retry explicitly; native acceptance can produce duplicates')


def run(root=None, *, agent, message_id, retry=False, mode=None):
    return native.deliver(universe.resolve(root), agent, message_id, retry, mode)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
