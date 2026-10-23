"""Shared shell output and callback identity checks; no action registry."""
import argparse
import json
import os
import subprocess
import sys

from ..universe import agents
from .. import universe


def add_universe(parser):
    parser.add_argument("-u", "--universe", dest="root", help="Universe directory or workspace (otherwise env/current directory)")


def add_agent(parser):
    parser.add_argument("--agent", default=os.environ.get("MV2_AGENT_ID"))


def add_wait(parser):
    parser.add_argument('--wait', action='store_true', help='Wait until the launch is ready or failed')
    parser.add_argument('--wait-timeout', type=float, default=60,
                        help='Readiness limit with --wait, in seconds (default: 60)')


def callback(root, agent, *, agent_scoped=False):
    root = universe.resolve(root)
    if not agent:
        raise ValueError("Specify --agent outside an agent session")
    directory = agents.directory(root, agent, allow_damaged=agent_scoped)
    if agent_scoped:
        from ..universe import records
        records.properties(directory / 'properties.json', harness_optional=True)
    from ..runtime.agent_identity import actor
    caller = actor(data=agent_scoped)
    if caller is not None:
        if not agent_scoped:
            raise ValueError('This integration callback requires the exact terminal launch; agent identity cannot authorize it')
        if caller.directory.resolve() != directory.resolve():
            raise ValueError('Agent identity cannot report or read another agent\'s inbox')
        return root, directory, None
    launch_id = os.environ.get('MV2_LAUNCH_ID')
    if agent_scoped and launch_id and launch_id != agents.runtime(directory).get('launch_id'):
        from .. import harnesses
        from ..runtime.native import adapter_hook
        from ..harnesses.types import SessionHandle
        try:
            from ..store import read
            properties = read(directory / 'properties.json')
            adapter = harnesses.load(root, properties['harness'])
            describe = adapter_hook(adapter, 'agent_identity_problem')
            problem = describe(SessionHandle.from_records(properties, agents.runtime(directory), directory), os.environ) if describe else None
        except (OSError, KeyError, ValueError, TypeError):
            problem = None
        if problem:
            raise ValueError(problem)
    if launch_id and agents.runtime(directory).get("launch_id") != launch_id:
        raise ValueError("Outdated launch")
    return root, directory, launch_id


def delivery_summary(receipt):
    """Expose delivery evidence without provider or core message identifiers."""
    result = {key: receipt[key] for key in ('state', 'reason', 'ok', 'reused') if key in receipt}
    value = receipt.get('value')
    if isinstance(value, dict) and 'transcript_confirmed' in value:
        result['transcript_confirmed'] = value['transcript_confirmed']
    if isinstance(value, dict) and value.get('paused') is True:
        result['paused'] = True
    return result


def output(call, *, render=None, **values):
    try:
        result = call(**values)
        if result is not None:
            display = render(result) if render else result
            print(display if isinstance(display, str) else json.dumps(display, indent=2, ensure_ascii=False))
        if isinstance(result, dict) and result.get("ok") is False:
            sys.exit(1)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"mv2: {error}", file=sys.stderr)
        sys.exit(1)


def main(configure, run):
    parser = argparse.ArgumentParser(description=sys.modules[run.__module__].__doc__)
    add_universe(parser)
    configure(parser)
    module = sys.modules[run.__module__]
    render = None if hasattr(module, 'cli_run') else getattr(module, 'format_output', None)
    output(getattr(module, 'cli_run', run), render=render, **vars(parser.parse_args()))
