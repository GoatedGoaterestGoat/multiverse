"""Discover operation support for a configured harness or exact session."""
from dataclasses import asdict

from .. import harnesses, native, universe
from ..harnesses.types import SessionHandle


def configure(parser):
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--agent')
    choice.add_argument('--harness')
    parser.add_argument('--probe', action='store_true', help='Run bounded, read-only native availability checks')


def run(root=None, *, agent=None, harness=None, probe=False):
    root = universe.resolve(root)
    if agent:
        return native.capabilities(root, agent, probe)
    adapter = harnesses.load(root, harness)
    session = SessionHandle('', harness, str(universe.workspace(root)))
    executor = native.NativeExecutor(session.workspace) if probe else None
    return {name: asdict(value) for name, value in adapter.capabilities(session, executor).items()}


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
