"""Recover terminal ownership from launch history when runtime.json is missing.

Only exact pane tags authorize closing a terminal. Historical requests locate
servers and the generation-private relay; they never supply a guessed child PID
or authority to contact a shared native server directly.
"""
from pathlib import Path
from uuid import UUID
import os
import re

from .. import harnesses
from ..harnesses.types import NativeResult
from ..store import lock, now, read, write
from ..terminal.tmux import Tmux, matches
from ..universe import agents, mailbox, records
from . import attachments
from .native import NativeExecutor


NOTICE = 'runtime.json was missing; no running terminal was found, so this starts a new launch'


def launches(directory):
    return sorted(path for path in (directory / 'launches').glob('*') if path.is_dir())


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _problem(directory, reason):
    return records.RecordError(directory / 'runtime.json', reason +
        '. Restore the runtime record from a verified backup, or use mv2 kill to close the verified terminal before retrying')


def damage(directory, facts):
    """Check launch authority before treating unmatched terminal tags as absence.

    Healthy terminal records need no filesystem or process probes. A genuinely
    empty, never-launched agent and the pre-terminal preparation stage remain
    valid; missing authority after startup must use independent discovery.
    """
    if agents.preparing(facts):
        return None  # Preparation's separate lock/evidence check owns this stage.
    reason = None
    if not facts:
        if launches(directory):
            reason = ('is missing' if not (directory / 'runtime.json').exists()
                      else 'launch_id is missing')
    elif not _uuid(facts.get('launch_id')):
        reason = 'launch_id must be a canonical UUID'
    elif facts.get('state') not in ('starting', 'running', 'ending', 'stopped', 'dormant', 'failed'):
        reason = 'state is missing or invalid'
    elif 'tmux' not in facts:
        launch = directory / 'launches' / facts['launch_id']
        # Registration and the waiting runner's cleanup journal can precede
        # terminal publication. A failed preparation with only those sidecars
        # is a real historical record, not missing terminal authority. Cleanup
        # ownership is still checked before its next launch.
        if (facts.get('state') in ('running', 'ending') or facts.get('pid') or facts.get('runner_pid')
                or (launch / 'request.json').exists() or (launch / 'ready').exists()):
            reason = 'tmux terminal identity is missing'
    else:
        terminal = facts['tmux']
        if not isinstance(terminal, dict):
            reason = 'tmux terminal identity must be an object'
        elif ('socket' not in terminal or terminal['socket'] is not None
              and (not isinstance(terminal['socket'], str) or not terminal['socket']
                   or not Path(terminal['socket']).is_absolute())):
            reason = 'tmux.socket must name an absolute socket path or the default server'
        else:
            for field, pattern in (('pane', r'%[0-9]+'), ('session', r'\$[0-9]+')):
                if not isinstance(terminal.get(field), str) or not re.fullmatch(pattern, terminal[field]):
                    reason = f'tmux.{field} is missing or invalid'
                    break
    if reason is None:
        for key in ('pid', 'runner_pid'):
            if key in facts and (type(facts[key]) is not int or facts[key] <= 0):
                reason = f'{key} must be a positive process ID'
                break
        for key in ('pid_started', 'runner_started'):
            if key in facts and facts[key] is not None and attachments._start_time(facts[key]) is None:
                reason = f'{key} must be a process start token or null'
                break
        if 'exit_code' in facts and type(facts['exit_code']) is not int:
            reason = 'exit_code must be an integer'
    if reason is None:
        return None
    error = _problem(directory, reason + '; terminal ownership must be checked before another launch')
    error.recover = 'kill'
    return error


def discover(directory, properties, socket_path=None, *, facts=None):
    """One snapshot per recorded/current server. No name or PID based adoption."""
    root = directory.parents[2]
    if (properties.get('id') != directory.name
            or properties.get('universe_id') != read(root / 'universe.json').get('id')):
        raise _problem(directory, 'saved agent/universe identity does not match its directory')
    sockets = {None}
    terminal = (facts or {}).get('tmux')
    if isinstance(terminal, dict):
        saved = terminal.get('socket')
        if isinstance(saved, str) and saved and Path(saved).is_absolute():
            sockets.add(saved)
    requests = {}
    for launch in launches(directory):
        if not _uuid(launch.name):
            raise _problem(directory, f'cannot inspect invalid launch folder {launch}')
        path = launch / 'request.json'
        request = records.properties(path)
        requests[launch.name] = request
        env = request.get('env', {})
        if not isinstance(env, dict):
            raise _problem(directory, f'invalid environment in {path}')
        saved = env.get('MV2_TMUX_SOCKET')
        if saved is not None:
            if not isinstance(saved, str) or not saved or not Path(saved).is_absolute():
                raise _problem(directory, f'invalid recorded terminal socket in {path}')
            sockets.add(saved)
    for value in (socket_path, os.environ.get('MV2_TMUX_SOCKET')):
        if value is not None:
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                raise _problem(directory, 'current terminal socket is not an absolute path')
            sockets.add(value)
    found, seen = [], set()
    for address in sorted(sockets, key=lambda item: item or ''):
        backend = Tmux(address)
        panes, error = backend.snapshot()
        if error:
            raise _problem(directory, f'cannot inspect terminal server {address or "multiverse-v2"}: {error}')
        for pane in panes.values():
            if (pane.get('@mv2_agent_id') != properties['id']
                    or pane.get('@mv2_universe_id') != properties['universe_id']):
                continue
            identifier = pane.get('@mv2_launch_id')
            if not _uuid(identifier) or identifier not in requests:
                raise _problem(directory, 'a tagged terminal has no valid saved launch request')
            # Explicit and default clients can address the same server.
            key = (pane.get('pane_pid'), pane.get('pane_id'), identifier)
            if key in seen:
                continue
            seen.add(key)
            found.append((backend, {'socket': address, 'pane': pane['pane_id'], 'session': pane['session_id']},
                          identifier, requests[identifier]))
    return found


def require_stopped(directory, properties, socket_path=None, *, facts=None):
    if discover(directory, properties, socket_path, facts=facts):
        raise _problem(directory, 'a running terminal is still tagged for this agent; refusing a duplicate launch')
    if facts and (reason := attachments.recorded_process_problem(directory, facts)):
        raise _problem(directory, reason)
    for launch in launches(directory):
        if reason := attachments.problem(directory, {'launch_id': launch.name}):
            raise _problem(directory, reason)
    if facts or (directory / 'runtime.json').exists():
        return ['runtime.json had damaged launch authority; no running terminal or recorded process was found, '
                'so this starts a new launch']
    return [NOTICE] if launches(directory) else []


def _qualified_facts(root, directory, properties, terminal, launch_id, request, adapter):
    """Recover only metadata attested by the selected generation, never a service."""
    facts = {'launch_id': launch_id, 'tmux': terminal, 'state': 'ending',
             'status': 'unknown', 'ending_at': now()}
    env = request.get('env')
    expected = str(Path('/tmp') / ('mv2-native-' + launch_id) / 'rpc.sock')
    proof = {'MV2_CONTROL_SOCKET': expected, 'MV2_UNIVERSE': str(root), 'MV2_AGENT_ID': properties['id'],
             'MV2_AGENT_DIR': str(directory), 'MV2_LAUNCH_ID': launch_id}
    if not isinstance(env, dict) or any(env.get(key) != value for key, value in proof.items()):
        return facts
    mode, attachment = request.get('native_mode'), request.get('attachment')
    if isinstance(mode, dict):
        facts['native_mode'] = mode
    if isinstance(attachment, dict):
        facts['attachment'] = attachment
    proxy, endpoint = request.get('rpc_proxy'), request.get('control_endpoint')
    if (getattr(adapter, 'family', None) == 'codex'
            and isinstance(mode, dict) and mode.get('mode') == 'bridge'
            and isinstance(attachment, dict) and attachment.get('work_owner') == 'service'
            and isinstance(proxy, dict) and proxy.get('socket_path') == expected
            and proxy.get('binds_session') is True and request.get('shared_service') is True
            and isinstance(endpoint, dict) and endpoint.get('transport') == 'unix-websocket'
            and isinstance(endpoint.get('address'), str) and endpoint.get('address')
            and proxy.get('upstream_path') == endpoint['address']):
        # The raw endpoint is shared: UUID/workspace alone do not attest its home.
        facts['control_endpoint'] = {'transport': 'unix-websocket', 'address': expected}
    return facts


def kill(root, directory, *, attachment_only=False):
    """Close one verified pane; unavailable native cancellation is reported honestly."""
    with lock(directory / '.launch.lock', create_parent=False):
        previous = records.runtime(directory / 'runtime.json')
        if (directory / 'runtime.json').exists() and damage(directory, previous) is None:
            return None  # Caller retries its normal path; never replace fresh facts.
        condition = ('was missing' if not (directory / 'runtime.json').exists()
                     else 'had damaged launch authority')
        properties = records.properties(directory / 'properties.json')
        found = discover(directory, properties, facts=previous)
        if len(found) > 1:
            raise _problem(directory, 'multiple tagged terminals remain; refusing to choose a launch')
        if not found:
            require_stopped(directory, properties, facts=previous)
            return {'id': directory.name, 'state': 'dormant', 'native_cancellation': {
                'state': 'unknown', 'reason': f'runtime.json {condition}; no running terminal was found. Native cancellation is unconfirmed'}}
        backend, terminal, launch_id, request = found[0]
        adapter = harnesses.load(root, properties['harness'])
        facts = _qualified_facts(root, directory, properties, terminal, launch_id, request, adapter)
        # State ending blocks workers/new input. This is recovered tag/request
        # metadata only: no child PID, native grant or shared-service proof.
        if agents.runtime(directory) != previous:
            raise _problem(directory, 'runtime record changed during terminal discovery; retry with the current record')
        write(directory / 'runtime.json', facts)
        cancellation = NativeResult('unknown', f'runtime.json {condition}; native cancellation could not be verified')
        if attachment_only:
            cancellation = NativeResult('ok', f'runtime.json {condition}; only the verified terminal was closed; no native cancellation was requested')
        elif facts.get('control_endpoint'):
            try:
                from ..harnesses.types import SessionHandle
                class ExactStopExecutor(NativeExecutor):
                    def check_session(self):
                        super().check_session()
                        if (agents.runtime(directory).get('launch_id') != launch_id
                                or records.properties(directory / 'launches' / launch_id / 'request.json') != request):
                            raise ValueError('Selected launch request changed; refusing native cancellation')
                session = SessionHandle.from_records(properties, facts, directory)
                executor = ExactStopExecutor(properties['workspace'], properties, facts)
                executor.stopping = True
                cancellation = adapter.stop(session, executor)
                # Preserve partial native counters even when the operation did
                # not finish. Only returned exact mailbox IDs are bookkeeping.
                details = cancellation.value if isinstance(cancellation.value, dict) else {}
                for message_id in details.get('cancelled_message_ids', []):
                    try:
                        mailbox.message_record(directory, message_id)
                    except ValueError:
                        continue
                    mailbox.update_message(directory, message_id, submission_state='cancelled', cancelled_at=now(),
                        last_receipt={'state': 'unavailable', 'reason': 'Cancelled by explicit agent stop'})
            except Exception as error:
                cancellation = NativeResult('unknown', f'runtime.json {condition}; native cancellation unconfirmed: ' + str(error),
                                            cancellation.value)
        # Check exact generation/tags again after the potentially slow RPC.
        latest = agents.runtime(directory)
        panes, error = backend.snapshot()
        if (error or latest.get('launch_id') != launch_id
                or not matches(panes.get(terminal['pane']), properties, facts)):
            raise _problem(directory, 'terminal ownership changed before close; no replacement was closed')
        backend.run('kill-pane', '-t', terminal['pane'])
    # Runner SIGHUP cleanup owns its actual child and drains its native relays.
    attachments.wait(directory, launch_id, 10)
    with lock(directory / '.launch.lock', create_parent=False):
        latest = agents.runtime(directory)
        if latest.get('launch_id') != launch_id:
            raise _problem(directory, 'launch changed during terminal cleanup')
        if attachments.problem(directory, latest):
            raise _problem(directory, 'terminal cleanup has not finished')
        panes, error = backend.snapshot()
        if error or matches(panes.get(terminal['pane']), properties, facts):
            raise _problem(directory, 'cannot confirm that the tagged terminal was closed')
        agents.update_runtime(directory, launch_id, state='dormant', status='unknown', dormant_at=now(),
                              native_cancellation=cancellation.to_dict(),
                              error=cancellation.reason if cancellation.state != 'ok' else None)
        mailbox.end_startup_messages(directory, launch_id, 'Terminal closed after runtime.json was lost; saved for explicit retry')
    details = cancellation.value if isinstance(cancellation.value, dict) else {}
    result = {'state': cancellation.state, 'reason': cancellation.reason,
              **{key: value for key, value in details.items() if key != 'cancelled_message_ids'}}
    return {'id': directory.name, 'state': 'dormant', 'native_cancellation': result}
