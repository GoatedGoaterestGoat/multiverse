"""Shared native controls. Core executes adapter specifications and owns receipts.

Durable message storage never depends on these best-effort operations.
"""
from dataclasses import asdict
import json
from pathlib import Path
import stat
import socket
import os
import subprocess
import time

from . import agents, harnesses
from .harnesses.types import NativeResult, CommandSpec, SessionHandle, Message, NativeEndpoint
from .store import lock, now, read, message_record, update_message
from .tmux import owned


def environment(command):
    return {k: v for k, v in os.environ.items() if not k.startswith('MV2_') and k not in command.unset_env}


def execute(command: CommandSpec, workspace, timeout=10):
    env = environment(command)
    # Never use a shell: message text and native IDs remain literal argv values.
    return subprocess.run(command.argv, cwd=workspace, env=env, text=True, capture_output=True, timeout=timeout)


def start_shared_service(specification, workspace, log_path, unset_env):
    """Ask the native owner to start/reuse its daemon. Never stop or restart it."""
    command = CommandSpec(specification.argv, unset_env)
    env = {**environment(command), **specification.environment}
    result = subprocess.run(command.argv, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    log_path.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f'Shared native service startup failed: {result.stderr.strip()}; see {log_path}')
    info = json.loads(result.stdout)
    address = info[specification.socket_field]
    if not isinstance(address, str) or not Path(address).is_absolute() or not stat.S_ISSOCK(Path(address).stat().st_mode):
        raise ValueError('Shared native service did not return an absolute Unix socket')
    versions = [info.get(key) for key in specification.version_fields]
    if any(not isinstance(v, str) or not v for v in versions) or versions[0] != versions[1]:
        raise RuntimeError('Native CLI/daemon version mismatch; update or restart explicitly when sessions are safe')
    return NativeEndpoint('unix-websocket', address)


def execute_rpc(spec, workspace, timeout=10):
    from .rpc import execute as run
    return run(spec, timeout)


class NativeExecutor:
    def __init__(self, workspace, properties=None, facts=None):
        self.workspace, self.properties, self.facts = workspace, properties, facts
        self.stopping = False

    def check_session(self):
        if self.properties is None or self.facts is None:
            raise ValueError('No running session selected')
        owned(self.properties, self.facts, states=('alive', 'ending', 'stopped') if self.stopping else ('alive',))

    def command(self, specification, timeout=10):
        self.check_session()
        return execute(specification, self.workspace, timeout)

    def rpc(self, specification, timeout=10):
        self.check_session()
        return execute_rpc(specification, self.workspace, timeout)

    def request(self, session, payload, timeout=10):
        self.check_session()
        if not session.endpoint or session.endpoint.transport != 'unix-json':
            return NativeResult('unavailable', 'No local extension socket')
        request = {**payload, 'launch_id': session.launch_id, 'native_id': session.native_id}
        wire = (json.dumps(request, ensure_ascii=False) + '\n').encode()
        if len(wire) > 1024 * 1024:
            return NativeResult('error', 'Native request exceeds 1 MiB')
        deadline = time.monotonic() + timeout
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(timeout)
            try:
                connection.connect(session.endpoint.address)
            except OSError as error:
                return NativeResult('error', f'Extension connection failed before submission: {error}')
            connection.sendall(wire)
            data = b''
            while b'\n' not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Extension request deadline exceeded; acceptance unknown')
                connection.settimeout(remaining)
                part = connection.recv(65536)
                if not part:
                    raise RuntimeError('Extension disconnected; acceptance unknown')
                data += part
                if len(data) > 1024 * 1024:
                    raise ValueError('Extension response exceeds 1 MiB')
            value = json.loads(data.split(b'\n', 1)[0])
            if (value.get('launch_id') != session.launch_id or value.get('native_id') != session.native_id
                    or value.get('message_id') != payload.get('message_id')):
                raise ValueError('Extension receipt identity mismatch; acceptance unknown')
            if value.get('state') not in ('ok', 'native_queued', 'unavailable', 'error', 'unknown'):
                raise ValueError('Invalid extension receipt state')
            return NativeResult(value['state'], value.get('reason', ''), value.get('value'))

    def write_lines(self, session, lines, timeout=10):
        """Submit JSON lines to a local inbox that sends no reply."""
        self.check_session()
        if not session.endpoint or session.endpoint.transport != 'unix-lines':
            return NativeResult('unavailable', 'No local inbox socket')
        wire = ''.join(line + '\n' for line in lines).encode()
        if len(wire) > 1024 * 1024:
            return NativeResult('error', 'Native request exceeds 1 MiB')
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(timeout)
            try:
                connection.connect(session.endpoint.address)
            except OSError as error:
                return NativeResult('error', f'Inbox connection failed before submission: {error}')
            connection.sendall(wire)
            connection.shutdown(socket.SHUT_WR)
            # The inbox closes its side once it has read every line.
            while connection.recv(65536):
                pass
        return NativeResult('ok', 'Written to the inbox')


def framed_text(root, message):
    """The recipient's text: who sent the message, then the message as written."""
    sender = message.get('from') or {}
    name = sender.get('name') or 'user'
    scope = sender.get('universe')
    if sender.get('agent_id') and scope:
        try:
            name = read(Path(scope) / 'state' / 'agents' / sender['agent_id'] / 'properties.json')['name']
        except (OSError, ValueError, KeyError):
            pass
    origin = ''
    if scope and Path(scope).resolve() != Path(root).resolve():
        try:
            origin = f' (in universe {read(Path(scope) / "universe.json")["name"]})'
        except (OSError, ValueError, KeyError):
            origin = f' (in universe {scope})'
    return f'Message from {name}{origin}:\n{message["text"]}'


def context(root, reference):
    directory = agents.directory(root, reference)
    properties = read(directory / 'properties.json')
    facts = agents.runtime(directory)
    return (directory, harnesses.load(root, properties['harness']),
            SessionHandle.from_records(properties, facts, directory),
            NativeExecutor(properties['workspace'], properties, facts))


def stop(root, reference):
    directory, adapter, session, executor = context(root, reference)
    executor.stopping = True
    result = adapter.stop(session, executor)
    for message_id in (result.value or {}).get('cancelled_message_ids', []):
        try:
            message_record(directory, message_id)
        except ValueError:
            continue  # Native input from another client is not a core inbox record.
        update_message(directory, message_id, submission_state='cancelled', cancelled_at=now(),
                       last_receipt={'state': 'unavailable', 'reason': 'Cancelled by explicit agent stop'})
    if result.state != 'ok':
        raise RuntimeError(f'Native stop incomplete; terminal left attached: {result.reason}')
    return result


def capabilities(root, reference, run_probe=False):
    _, adapter, session, executor = context(root, reference)
    return {name: asdict(value) for name, value in adapter.capabilities(session, executor if run_probe else None).items()}


def usage_snapshot(root, properties, facts):
    """Nonblocking snapshot: optional failures are data, never UI exceptions."""
    try:
        adapter = harnesses.load(root, properties['harness'])
        directory = root / 'state' / 'agents' / properties['id']
        session = SessionHandle.from_records(properties, facts, directory)
        return adapter.get_usage(session).to_dict()
    except Exception as error:
        return NativeResult('error', str(error)).to_dict()


def operation(root, reference, name, question=''):
    directory = agents.directory(root, reference)
    try:
        if name == 'ask' and not question.strip():
            raise ValueError('A side question is required')
        with lock(directory / '.launch.lock'):
            _, adapter, session, executor = context(root, directory.name)
            if name == 'get_usage':
                return adapter.get_usage(session, executor).to_dict()
            if name != 'ask':
                return NativeResult('unavailable', f'Unknown operation: {name}').to_dict()
        # An answer can take a while: wait outside the launch lock so sends and
        # stop are not held. The question still targets this exact session.
        return adapter.ask(session, question, executor).to_dict()
    except Exception as error:
        return NativeResult('error', str(error)).to_dict()


def control(root, reference, operation_name):
    """Compatibility for the old native-status tool; new callers use named operations."""
    if operation_name == 'usage':
        return operation(root, reference, 'get_usage')
    if operation_name in ('status', 'activity'):
        return NativeResult('unavailable', 'Native status is not implemented; use list for core launch state').to_dict()
    return NativeResult('unavailable', 'Use a named operation; no native side effect attempted').to_dict()


def deliver(root, reference, message_id, retry=False, check=False):
    directory = agents.directory(root, reference)
    # Recipient submission is serialized with generation changes. Inbox operations
    # take their own shorter lock, separate from read-position changes.
    with lock(directory / '.launch.lock'), lock(directory / '.delivery.lock'):
        message, tracking = message_record(directory, message_id)
        try:
            _, adapter, session, executor = context(root, directory.name)
        except Exception as error:
            receipt = {'state': 'error', 'ok': False, 'message_id': message_id,
                       'reason': f'Cannot load recipient adapter: {error}', 'at': now()}
            if tracking.get('submission_state', 'pending') in ('pending', 'failed'):
                update_message(directory, message_id, submission_state='failed',
                               attempts=[*tracking.get('attempts', []), receipt])
            return receipt
        if tracking.get('submission_state') == 'cancelled':
            return {**tracking['last_receipt'], 'message_id': message_id, 'reused': True, 'ok': False}
        if check:
            target = tracking.get('target_session') or message.get('target_session')
            if target and target != session.native_id:
                raise ValueError('Message targets a different native session; refusing reconciliation')
            try:
                result = adapter.get_delivery(session, message_id, executor)
                if result.state == 'native_queued':
                    changes = {'submission_state': 'accepted', 'native_receipt': result.value}
                    if (result.value or {}).get('transcript_confirmed'):
                        changes['transcript_confirmed_at'] = now()
                    update_message(directory, message_id, **changes)
                return {**result.to_dict(), 'message_id': message_id, 'ok': result.state not in ('error', 'unknown', 'unavailable')}
            except Exception as error:
                return {**NativeResult('error', f'Receipt check failed: {error}').to_dict(), 'ok': False}
        selected_mode = message.get('delivery', 'inbox-only')
        if selected_mode not in ('steer', 'follow-up'):
            raise ValueError('Native delivery requires steer or follow-up')
        target = tracking.get('target_session') or message.get('target_session')
        if target and target != session.native_id:
            raise ValueError('Message targets a different native session; refusing delivery')
        previous = tracking.get('attempts', [])
        state = tracking.get('submission_state', 'pending')
        if state in ('accepted', 'unknown', 'submitting') or (state == 'failed' and not retry):
            receipt = previous[-1] if previous else {'state': 'unknown', 'reason': 'No submission receipt recorded; no resend'}
            if state == 'accepted':
                receipt = {**receipt, 'state': 'native_queued', 'reason': 'Native acceptance recorded; no resend',
                           'value': tracking.get('native_receipt')}
            if state == 'submitting':
                receipt = {**receipt, 'state': 'unknown', 'reason': 'Previous submission was interrupted; acceptance unknown'}
                update_message(directory, message_id, submission_state='unknown')
            return {**receipt, 'reused': True, 'ok': receipt['state'] not in ('error', 'unavailable', 'unknown')}
        try:
            executor.check_session()
        except Exception as error:
            result = {'state': 'error', 'reason': f'Pre-submission session check failed: {error}', 'ok': False, 'message_id': message_id}
            update_message(directory, message_id, submission_state='failed', attempts=[*previous, result])
            return result
        attempt = {'state': 'unknown', 'reason': 'Submission in progress', 'message_id': message_id,
                   'native_session_id': session.native_id, 'mode': selected_mode, 'agent_id': session.agent_id,
                   'launch_id': session.launch_id, 'at': now()}
        # Persist BEFORE the native call: a crash cannot turn uncertain submission
        # into pending and silently replay it on restart.
        update_message(directory, message_id, submission_state='submitting', target_session=target or session.native_id,
                       attempts=[*previous, attempt])
        try:
            if not session.native_id:
                result = NativeResult('unavailable', 'No exact native session ID bound; retry after binding')
            else:
                result = adapter.send(session, Message(message['id'], message['text'], message.get('from', {}),
                                      delivery=selected_mode, framed=framed_text(root, message)), executor)
        except Exception as error:
            result = NativeResult('unknown', f'{error}; acceptance unknown; no automatic resend')
        receipt = {**attempt, **result.to_dict(), 'ok': result.state not in ('unavailable', 'error', 'unknown')}
        state = {'native_queued': 'accepted', 'ok': 'accepted', 'pending': 'pending',
                 'unavailable': 'failed', 'error': 'failed', 'unknown': 'unknown'}[result.state]
        update_message(directory, message_id, submission_state=state, attempts=[*previous, receipt],
                       native_receipt=result.value)
        return receipt
