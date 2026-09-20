"""Recovery: retry native delivery of a previously saved message."""
from .. import native, universe


def configure(parser):
    parser.add_argument('agent')
    parser.add_argument('--message-id', required=True)
    parser.add_argument('--check', action='store_true', help='Reconcile native receipt without submitting')
    parser.add_argument('--retry', action='store_true', help='Retry a definite failure; accepted or uncertain submissions are never blindly resent')


def run(root=None, *, agent, message_id, retry=False, check=False):
    return native.deliver(universe.resolve(root), agent, message_id, retry, check)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
