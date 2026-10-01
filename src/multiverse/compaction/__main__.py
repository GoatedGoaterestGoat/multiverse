"""Entry points harness integrations run: `python -m multiverse.compaction ACTION --agent-dir DIR`.

install     compact the session now: read the install trigger's request on
            stdin; write progress, then each message, then the result as JSON
            lines on stdout
background  note that a turn ended (--turn-ended MARKER) and start the background
            tree builder unless one is running; returns at once
worker      the background builder itself
guard       a harness's last line of defence: refuse its own compaction (exit 2)

These are not `mv2` commands; people use `mv2 compaction` (tools/compaction.py).
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from .build import Build, Cancelled, Reporter, json_lines
from .pipeline import CompactionError, compact, open_agent
from .store import Store

# A turn whose background build failed is not retried in the background for this long;
# a compaction that needs it retries it at once.
BACKGROUND_RETRY_AFTER = 3600


def _agent(directory: str):
    directory = Path(directory).resolve()
    root = directory.parents[2] if len(directory.parents) > 2 else None
    if root is None or directory.parent.name != 'agents' or not (root / 'universe.json').is_file():
        raise ValueError(f'Not an agent directory: {directory}')
    return open_agent(root, directory.name)


def install(directory: str, request: dict, emit) -> int:
    def fail(message):
        emit({'event': 'failed', 'message': message})
        try:
            Store(directory).log(f'compaction failed: {message}')
        except OSError:
            pass
        return 1

    try:
        agent = _agent(directory)
    except (OSError, ValueError, KeyError, CompactionError) as error:
        return fail(str(error))
    if not agent.settings.enabled:
        return fail(f'compaction is no longer enabled for the {agent.harness} harness in this universe; '
                    'restart the agent to use its native compaction')
    store = agent.store
    held = store.install_lock()
    if held is None:
        return fail('another compaction of this session is already running')
    cancel = threading.Event()

    def stop(signum, frame):
        cancel.set()
        agent.source.cancel()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    reporter = Reporter(store, emit)
    with held:
        store.set_activity({'phase': 'compacting', 'trigger': request.get('trigger')})
        try:
            payload, stats = compact(agent, request.get('offered'), reporter, cancel)
            install_id, path = store.save_install({**payload, 'stats': stats, 'trigger': request.get('trigger')})
        except Cancelled:
            return fail('cancelled')
        except CompactionError as error:
            return fail(str(error))
        except Exception as error:  # report everything; the harness must never fall back silently
            import traceback
            store.log(f'compaction crashed:\n{traceback.format_exc()}')
            return fail(f'unexpected error: {error}')
        finally:
            store.set_activity(None)
    store.log(f'compaction rendered {payload.get("count")} rows: {stats["tokens_before"]} -> {stats["tokens_after"]} '
              f'estimated tokens over {stats["turns"]} turns')
    for message in payload.get('messages', []):
        emit({'event': 'message', 'message': message})
    emit({'event': 'result', 'path': str(path), 'install_id': install_id, 'count': payload.get('count'), 'stats': stats})
    return 0


def background(directory: str, marker: str | None = None) -> int:
    """Start the builder in its own session so it outlives the caller; never wait for it.

    `marker` is the harness's turn-end signal: where the finished turn ended. A
    builder already running takes it up on its next pass.
    """
    agent = _agent(directory)
    if not (agent.settings.enabled and agent.settings.builder.background):
        return 0
    store = agent.store
    if marker:
        store.mark_turn_end(marker)
    if store.worker_holder() is not None:
        return 0
    store.directory.mkdir(parents=True, exist_ok=True)
    with (store.directory / 'build.log').open('a') as log:
        subprocess.Popen([sys.executable, '-m', 'multiverse.compaction', 'worker', '--agent-dir', str(agent.directory)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    return 0


def worker(directory: str) -> int:
    agent = _agent(directory)
    store = agent.store
    held = store.worker_lock()
    if held is None:
        return 0
    cancel = threading.Event()

    def stop(signum, frame):
        cancel.set()
        agent.source.cancel()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    with held:
        reporter = Reporter(store)
        # A turn may finish while we build: look again until nothing new is left.
        for _ in range(5):
            turns = agent.source.completed(agent.source.turns(), store.turn_end())
            build = Build(agent.source, store, agent.settings.builder, reporter, cancel)
            skip = lambda turn: store.recent_failure(turn.id, BACKGROUND_RETRY_AFTER) is not None
            try:
                build.turns(turns, wait=False, skip=skip)
            except Cancelled:
                return 0
            if build.built == 0:
                break
    return 0


def guard(stdin_text: str) -> int:
    """Claude-style PreCompact hook: allow a subagent's own compaction, refuse the session's."""
    try:
        event = json.loads(stdin_text or '{}')
    except ValueError:
        event = {}
    if isinstance(event, dict) and event.get('agent_id'):
        return 0
    where = os.environ.get('MV2_AGENT_DIR')
    log = f' See {Path(where) / "compaction" / "build.log"}.' if where else ''
    print('mv2 compaction is enabled for this session, but it did not install a compaction, so the '
          f"harness's own compaction was blocked.{log}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m multiverse.compaction', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('install', 'background', 'worker', 'guard'))
    parser.add_argument('--agent-dir', default=os.environ.get('MV2_AGENT_DIR'))
    parser.add_argument('--turn-ended', help="background: the harness's marker for where the finished turn ended")
    args = parser.parse_args(argv)
    if args.action == 'guard':
        return guard(sys.stdin.read())
    if not args.agent_dir:
        parser.error('--agent-dir is required outside an agent session')
    if args.action == 'install':
        emit = json_lines()
        try:
            request = json.loads(sys.stdin.read() or '{}')
            if not isinstance(request, dict):
                raise ValueError('the request must be an object')
        except ValueError as error:
            emit({'event': 'failed', 'message': f'unreadable install request: {error}'})
            return 1
        return install(args.agent_dir, request, emit)
    try:
        if args.action == 'background':
            return background(args.agent_dir, args.turn_ended)
        return worker(args.agent_dir)
    except (OSError, ValueError, KeyError, CompactionError) as error:
        print(f'mv2 compaction {args.action}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
