"""Discover operation support for a configured harness or exact session."""
from dataclasses import asdict

from .. import harnesses, universe
from ..runtime import native
from ..harnesses.types import SessionHandle


def configure(parser):
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--agent')
    choice.add_argument('--harness')
    parser.add_argument('--probe', action='store_true', help='Run bounded, read-only native availability checks')
    parser.add_argument('--json', action='store_true', help='Print every capability record as JSON')


WORDS = {'session_name': 'session name', 'send_steer': 'steer', 'send_follow_up': 'follow-up', 'get_usage': 'usage',
         'get_delivery': 'delivery receipts', 'wake': 'wake with a message'}


def cli_run(root=None, *, agent=None, harness=None, probe=False, json=False):
    from ..display import clean
    found = run(root, agent=agent, harness=harness, probe=probe)
    if json:
        return found
    lines = []
    for name, value in found.items():
        state, reason = value.get('state'), ' '.join(clean(str(value.get('reason') or '')).split())
        word = WORDS.get(name, name.replace('_', ' '))
        lines.append(f'{word}: {state}' + (f': {reason}' if reason and state != 'supported' else ''))
    return '\n'.join(lines)


def run(root=None, *, agent=None, harness=None, probe=False, json=False):  # `json`: presentation only
    root = universe.resolve(root)
    if agent:
        return native.capabilities(root, agent, probe)
    adapter = harnesses.load(root, harness)
    session = SessionHandle('', harness, str(universe.workspace(root)))
    missing = missing_executable(adapter) if probe else None
    executor = native.NativeExecutor(session.workspace) if probe and not missing else None
    found = {name: asdict(value) for name, value in adapter.capabilities(session, executor).items()}
    if missing:
        # As spawn refuses: nothing this harness does can run until its executable is found.
        found = {name: {**value, 'state': 'unavailable', 'reason': f'Harness executable not found: {missing}',
                        'mechanism': None} for name, value in found.items()}
    return found


def missing_executable(adapter):
    """The harness's executable when spawn's lookup (`shutil.which`) can't find it, else None."""
    import shutil
    argv = (getattr(adapter, 'spec', None) or {}).get('spawn') or []
    return argv[0] if argv and isinstance(argv[0], str) and not shutil.which(argv[0]) else None


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
