"""Read optional native observations; unavailable and failed observations are explicit."""
from .. import native, universe


def configure(parser):
    parser.add_argument('agent')
    parser.add_argument('--operation', choices=('activity', 'usage', 'interrupt', 'ask'), default='usage')


def run(root=None, *, agent, operation='usage'):
    return native.control(universe.resolve(root), agent, operation)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
