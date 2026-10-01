"""Shared shell output and callback identity checks; no action registry."""
import argparse
import json
import os
import subprocess
import sys

from .. import agents, universe


def add_universe(parser):
    parser.add_argument("-u", "--universe", dest="root", help="Universe directory or workspace (otherwise env/current directory)")


def add_agent(parser):
    parser.add_argument("--agent", default=os.environ.get("MV2_AGENT_ID"))


def callback(root, agent):
    root = universe.resolve(root)
    if not agent:
        raise ValueError("Specify --agent outside an agent session")
    directory = agents.directory(root, agent)
    launch_id = os.environ.get("MV2_LAUNCH_ID")
    if launch_id and agents.runtime(directory).get("launch_id") != launch_id:
        raise ValueError("Outdated launch")
    return root, directory, launch_id


def delivery_summary(receipt):
    """Expose delivery evidence without provider or core message identifiers."""
    result = {key: receipt[key] for key in ('state', 'reason', 'ok', 'reused') if key in receipt}
    value = receipt.get('value')
    if isinstance(value, dict) and 'transcript_confirmed' in value:
        result['transcript_confirmed'] = value['transcript_confirmed']
    return result


def output(call, **values):
    try:
        result = call(**values)
        if result is not None:
            print(result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False))
        if isinstance(result, dict) and result.get("ok") is False:
            sys.exit(1)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"mv2: {error}", file=sys.stderr)
        sys.exit(1)


def main(configure, run):
    parser = argparse.ArgumentParser(description=sys.modules[run.__module__].__doc__)
    add_universe(parser)
    configure(parser)
    output(run, **vars(parser.parse_args()))
