"""Discover native harness capabilities; optional probes inspect installed command help."""
from dataclasses import asdict

from .. import harnesses, native, universe
from ..harnesses.types import NativeContext


def configure(parser):
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--agent')
    choice.add_argument('--harness')
    parser.add_argument('--probe', action='store_true', help='Run bounded, non-model native help probes')


def run(root=None, *, agent=None, harness=None, probe=False):
    root = universe.resolve(root)
    if agent:
        return native.capabilities(root, agent, probe)
    adapter = harnesses.load(root, harness)
    probes = native.probe(adapter, universe.workspace(root)) if probe else {}
    return {name: asdict(value) for name, value in adapter.capabilities(NativeContext({}, probes=probes)).items()}


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
