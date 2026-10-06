"""Ask an isolated side question without changing the main conversation."""
from ..runtime import native
from .. import universe


def configure(parser):
    parser.add_argument("agent")
    parser.add_argument("--question", required=True)


def run(root=None, *, agent, question):
    return native.operation(universe.resolve(root), agent, "ask", question=question)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
