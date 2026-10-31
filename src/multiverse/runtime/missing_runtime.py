"""Recover terminal ownership from launch history when runtime.json is missing.

Only exact pane tags authorize closing a terminal. Historical requests locate
servers and the generation-private relay; they never supply a guessed child PID
or authority to contact a shared native server directly.
"""
from pathlib import Path
from uuid import UUID
import os
import json
import re

from . import effects
from .. import harnesses
from ..harnesses.types import NativeResult
from ..store import lock, now, read, write
from ..terminal.tmux import Tmux, matches, observe
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


def _problem(directory, reason, *, recommend_kill=True):
    remedy = '. Restore the runtime record from a verified backup'
    if recommend_kill:
        remedy += ', or use mv2 kill to close the verified terminal'
    return records.RecordError(directory / 'runtime.json', reason + remedy + ' before retrying. '
        'If independently verified that this agent\'s terminal, runner and child are all gone, remove the damaged '
        'runtime.json and retry')


def _request_problem(path, reason, *, unreadable=False):
    remedy = 'restore read access or repair' if unreadable else 'repair'
    return records.RecordError(path, f'{reason}; {remedy} {path} from a verified backup '
        'before retrying. Its recorded terminal server must be readable to check for a surviving launch')



def recovery_snapshot(directory):
    """Readable malformed bytes are lost authority; unreadable bytes still refuse recovery."""
    path = directory / 'runtime.json'
    try:
        return records.runtime(path), None
    except records.RecordError:
        try:
            with path.open('rb') as stream:
                raw = stream.read(records.MAX_BYTES + 1)
        except FileNotFoundError:
            return {}, None
        except OSError as error:
            raise _problem(directory, f'cannot safely read {path}: {error}', recommend_kill=False) from error
        if len(raw) > records.MAX_BYTES:
            raise _problem(directory, f'{path} is too large to inspect safely', recommend_kill=False)
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict):
            return data, None  # A writer repaired it between the two reads.
        return {}, raw


def _unchanged(directory, facts, malformed):
    if recovery_snapshot(directory) != (facts, malformed):
        raise _problem(directory, 'runtime record changed during terminal discovery; retry with the current record')


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
            server = terminal.get('server')
            if server is not None and (not isinstance(server, dict)
                    or type(server.get('pid')) is not int or server['pid'] <= 0
                    or not attachments.valid_start(server.get('started'))
                    or not isinstance(server.get('socket'), str)
                    or not isinstance(terminal.get('socket'), str)
                    or server['socket'] != str(Path(terminal['socket']).resolve())):
                reason = 'tmux.server must name this socket and a valid server process identity'
    if reason is None:
        for key in ('pid', 'runner_pid'):
            if key in facts and (type(facts[key]) is not int or facts[key] <= 0):
                reason = f'{key} must be a positive process ID'
                break
        for key in ('pid_started', 'runner_started'):
            if key in facts and facts[key] is not None and not attachments.valid_start(facts[key]):
                reason = f'{key} must be a process start token or null'
                break
        if 'exit_code' in facts and type(facts['exit_code']) is not int:
            reason = 'exit_code must be an integer'
    if reason is None:
        return None
    error = _problem(directory, reason + '; terminal ownership must be checked before another launch')
    error.recover = 'kill'
    return error


def mismatch(directory, facts, observation):
    """An absent owned pane contradicts a positively attested active process.

    Reuse the caller's terminal observation. Healthy panes, unknown servers and
    normal teardown never add process queries to listing or launch checks.
    """
    if (observation['live'] != 'dormant' or not facts.get('tmux')
            or facts.get('state') not in ('starting', 'running')
            or 'exit_code' in facts or facts.get('ending_at') or facts.get('stopped_at')):
        return None
    if reason := attachments.attested_running(facts):
        error = _problem(directory, 'recorded terminal has no owned pane, but the ' + reason +
                         '; terminal identity is inconsistent')
        error.recover = 'kill'
        return error
    return None


def discover(directory, properties, socket_path=None, *, facts=None, orphan=False, requestless=False):
    """One snapshot per recorded/current server. No name or PID based adoption."""
    root = directory.parents[2]
    if (properties.get('id') != directory.name
            or properties.get('universe_id') != read(root / 'universe.json').get('id')):
        raise _problem(directory, 'saved agent/universe identity does not match its directory')
    from ..terminal.recovery import address as terminal_address
    sockets = {str(terminal_address(Tmux())), str(terminal_address(Tmux(legacy=True)))}
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
        try:
            path.stat()
        except FileNotFoundError:
            continue  # Preparation can fail before publishing a native request.
        except OSError as error:
            raise _request_problem(path, f'cannot inspect request: {error}', unreadable=True) from error
        try:
            request = records.properties(path)
        except records.RecordError as error:
            raise _request_problem(path, error.reason, unreadable=error.reason.startswith('cannot be read:')) from error
        requests[launch.name] = request
        env = request.get('env', {})
        if not isinstance(env, dict):
            raise _request_problem(path, 'invalid environment')
        saved = env.get('MV2_TMUX_SOCKET')
        if saved is not None:
            if not isinstance(saved, str) or not saved or not Path(saved).is_absolute():
                raise _request_problem(path, 'invalid recorded terminal socket')
            sockets.add(saved)
    for value in (socket_path, os.environ.get('MV2_TMUX_SOCKET')):
        if value is not None:
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                raise _problem(directory, 'current terminal socket is not an absolute path')
            sockets.add(value)
    found, seen = [], set()
    for address in sorted(sockets, key=lambda item: item or ''):
        backend = Tmux(address)
        # A malformed terminal cannot lend its runner to a different candidate
        # address. Saved requests still let independent discovery find its pane.
        qualified = (isinstance(terminal, dict) and isinstance(address, str)
                     and address == terminal.get('socket')
                     and (not isinstance(terminal.get('server'), dict)
                          or terminal['server'].get('socket') == str(Path(address).resolve())))
        panes, error = backend.snapshot(facts=facts if qualified else None)
        if error:
            from ..terminal.recovery import Unreachable, UnreachableReason
            if isinstance(error, UnreachableReason):
                raise Unreachable(error)
            raise _problem(directory, f'cannot inspect terminal server {address or "multiverse-v2"}: {error}')
        for pane in panes.values():
            if (pane.get('@mv2_agent_id') != properties['id']
                    or pane.get('@mv2_universe_id') != properties['universe_id']):
                continue
            if orphan:
                raise records.RecordError(directory,
                    f'tagged pane {pane.get("pane_id")} on terminal server {address or "multiverse-v2"} '
                    'still belongs to this folder; verify and close that terminal before removing the folder')
            identifier = pane.get('@mv2_launch_id')
            if not _uuid(identifier) or identifier not in requests and not requestless:
                raise _problem(directory, 'a tagged terminal has no valid saved launch request')
            # Explicit and default clients can address the same server.
            key = (pane.get('pane_pid'), pane.get('pane_id'), identifier)
            if key in seen:
                continue
            seen.add(key)
            recovered = {'socket': address, 'pane': pane['pane_id'], 'session': pane['session_id']}
            owner = Tmux.for_terminal(recovered, properties, {'launch_id': identifier})
            recovered['server_identity'] = dict(owner.identity)
            found.append((owner, recovered, identifier, requests.get(identifier, {})))
    return found


def require_stopped(directory, properties, socket_path=None, *, facts=None, operation=None, orphan=False):
    if discover(directory, properties, socket_path, facts=facts, orphan=orphan):
        refusal = f'refusing to {operation} this agent' if operation else 'refusing a duplicate launch'
        terminal = (facts or {}).get('tmux')
        inconsistent = (isinstance(terminal, dict) and isinstance(terminal.get('server'), dict)
                        and isinstance(terminal.get('socket'), str)
                        and terminal['server'].get('socket') != str(Path(terminal['socket']).resolve()))
        raise _problem(directory, ('terminal identity is inconsistent; ' if inconsistent else '') +
                       'a running terminal is still tagged for this agent; ' + refusal,
                       recommend_kill=operation != 'kill')
    if facts and (reason := attachments.recorded_process_problem(directory, facts)):
        raise _problem(directory, reason, recommend_kill=False)
    for launch in launches(directory):
        if reason := attachments.problem(directory, {'launch_id': launch.name}):
            raise _problem(directory, reason, recommend_kill=False)
    if facts or (directory / 'runtime.json').exists():
        return ['runtime.json had damaged launch authority; no running terminal or recorded process was found, '
                'so this starts a new launch']
    return [NOTICE] if launches(directory) else []


def _qualified_facts(root, directory, properties, terminal, launch_id, request, adapter):
    """Recover only metadata attested by the selected generation, never a service."""
    facts = {'launch_id': launch_id, 'tmux': terminal, 'state': 'ending',
             'status': 'unknown', 'ending_at': now()}
    env = request.get('env')
    from .paths import launch_socket
    if not isinstance(env, dict):
        return facts
    try:
        expected = str(launch_socket(launch_id, env))
    except ValueError:
        return facts
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
        previous, malformed = recovery_snapshot(directory)
        missing_properties = agents.properties_missing(directory)
        properties = ({'id': directory.name, 'name': directory.name,
                       'universe_id': read(root / 'universe.json')['id']} if missing_properties
                      else records.properties(directory / 'properties.json'))
        if (not missing_properties and malformed is None and (directory / 'runtime.json').exists() and damage(directory, previous) is None
                and mismatch(directory, previous, observe(properties, previous)) is None):
            return None  # Caller retries its normal path; never replace fresh facts.
        condition = ('was missing' if not (directory / 'runtime.json').exists()
                     else 'had damaged launch authority')
        found = discover(directory, properties, facts=previous, requestless=missing_properties)
        saved_launch = previous.get('launch_id')
        if _uuid(saved_launch) and any(row[2] != saved_launch for row in found):
            raise _problem(directory, 'tagged terminal belongs to a different saved launch; refusing to adopt it')
        if len(found) > 1:
            raise _problem(directory, 'multiple tagged terminals remain; refusing to choose a launch')
        if not found:
            require_stopped(directory, properties, facts=previous, operation='kill')
            _unchanged(directory, previous, malformed)
            return {'id': directory.name, 'state': 'dormant', 'native_cancellation': {
                'state': 'unknown', 'reason': f'runtime.json {condition}; no running terminal was found. Native cancellation is unconfirmed'}}
        backend, terminal, launch_id, request = found[0]
        adapter = None if missing_properties else harnesses.load(root, properties['harness'])
        facts = _qualified_facts(root, directory, properties, terminal, launch_id, request, adapter)
        if missing_properties:
            # Tags authorize closing this pane, never native cancellation. Keep
            # surviving PID/start evidence for the cleanup barrier after close.
            facts = {**previous, **facts}
            facts.pop('control_endpoint', None)
        # State ending blocks workers/new input. This is recovered tag/request
        # metadata only: no child PID, native grant or shared-service proof.
        _unchanged(directory, previous, malformed)
        if missing_properties and not agents.properties_missing(directory):
            raise _problem(directory, 'properties record changed during terminal discovery; retry')
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
                cancellation = effects.call(adapter, 'stop', session, executor)
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
                or missing_properties and not agents.properties_missing(directory)
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
