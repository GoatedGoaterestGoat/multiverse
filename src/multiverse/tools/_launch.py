"""Presentation of a launch refused before the normal result was returned."""
from ..runtime import lifecycle
from .. import universe
from ..universe import agents


def failure_record(root, reference, error):
    """Read saved context if available; never mutate it or observe a terminal."""
    record = {'name': reference}
    try:
        root = universe.resolve(root)
        directory = agents.directory(root, reference)
        record = lifecycle.launch_result(root, directory)
    except (OSError, ValueError, TypeError, KeyError):
        pass  # Failure to enrich the result cannot hide the original refusal.
    return {**record, 'ok': False, 'ready': False, 'error': str(error), 'reason': str(error)}
