"""Shared native controls. Core executes adapter specifications and owns receipts.

Durable message storage never depends on these best-effort operations.
"""
from dataclasses import asdict, replace
import json
from pathlib import Path
import stat
import socket
import os
import subprocess
import time
import re
from contextlib import contextmanager
import fcntl
import inspect

from .. import harnesses
from ..harnesses.types import (NativeResult, CommandSpec, SessionHandle, Message, NativeEndpoint,
                               BridgeUnavailable, NativeServiceHandle, ServiceIdentity, AttachmentSpec)
from ..harnesses.types import ObservedBinding, normalize_session_id
from ..store import lock, now, read
from ..terminal.tmux import owned, observe
from ..universe import agents
from ..universe.mailbox import end_startup_messages, inbox, message_record, update_message


def environment(command):
    return {k: v for k, v in os.environ.items() if not k.startswith('MV2_') and k not in command.unset_env}


def execute(command: CommandSpec, workspace, timeout=10):
    env = environment(command)
    # Never use a shell: message text and native IDs remain literal argv values.
    return subprocess.run(command.argv, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                          text=True, capture_output=True, timeout=timeout)


def start_shared_service(specification, workspace, log_path, unset_env, observe_versions=None):
    """Ask the native owner to start/reuse its daemon. Never stop or restart it."""
    try:
        return _start_shared_service(specification, workspace, log_path, unset_env, observe_versions)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        raise BridgeUnavailable(str(error)) from error


def _start_shared_service(specification, workspace, log_path, unset_env, observe_versions):
    if specification.lifetime != 'external':
        raise ValueError('Shared native services must remain externally owned')
    command = CommandSpec(specification.argv, unset_env)
    env = {**environment(command), **specification.environment}
    # Shared services outlive worktrees and temporary universes. Their adapter
    # can select a persistent cwd without moving the agent's native terminal.
    service_cwd = specification.cwd or workspace
    argv = command.argv
    if specification.cwd and '/' in argv[0] and not Path(argv[0]).is_absolute():
        argv = (str((Path(workspace) / argv[0]).resolve()), *argv[1:])
    result = subprocess.run(argv, cwd=service_cwd, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    log_path.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f'Shared native service startup failed: {result.stderr.strip()}; see {log_path}')
    info = json.loads(result.stdout)
    address = info[specification.socket_field]
    if not isinstance(address, str) or not Path(address).is_absolute() or not stat.S_ISSOCK(Path(address).stat().st_mode):
        raise ValueError('Shared native service did not return an absolute Unix socket')
    versions = [info.get(key) for key in specification.version_fields]
    if any(not isinstance(v, str) or not v for v in versions):
        raise ValueError('Native service returned invalid CLI/daemon version metadata')
    if observe_versions:
        observe_versions({'cli': versions[0], 'daemon': versions[1]})
    if specification.require_matching_versions and versions[0] != versions[1]:
        raise RuntimeError('Native CLI/daemon version mismatch; update or restart explicitly when sessions are safe')
    if specification.required_cli_flags:
        # Read-only CLI help detects old/partial installations without starting
        # another daemon or constructing a speculative native conversation.
        help_result = subprocess.run([argv[0], '--help'], cwd=service_cwd, env=env,
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        help_text = help_result.stdout + help_result.stderr
        missing = [flag for flag in specification.required_cli_flags
                   if not re.search(r'(?<!\S)' + re.escape(flag) + r'(?=[\s=<]|$)', help_text)]
        if help_result.returncode or missing:
            raise ValueError('Native CLI does not advertise required bridge flags: ' + ', '.join(missing or specification.required_cli_flags))
    return NativeEndpoint('unix-websocket', address)


def service_handle(specification, endpoint):
    """Persist explicit provider scope without inferring it from a relay."""
    if specification.lifetime != 'external':
        raise ValueError('Shared native services must remain externally owned')
    identity = specification.identity
    if identity is None:
        return None  # Compatibility: old service specs do not authorize recovery.
    if (not isinstance(identity, ServiceIdentity) or not identity.provider or not identity.scope
            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                   for k, v in identity.scope.items())):
        raise ValueError('Native service requires an exact provider and storage/account scope')
    return NativeServiceHandle(identity, endpoint)


def recovery_unavailable(session):
    attachment, service = session.attachment, session.service
    if (attachment is None or attachment.recovery != 'idle-existing-service'
            or attachment.work_owner != 'service' or attachment.readiness != 'binding'):
        return 'This adapter has not declared safe native attachment recovery'
    if (service is None or service.lifetime != 'external' or not service.identity.provider
            or not service.identity.scope or session.endpoint != service.endpoint or not session.native_id):
        return 'No exact externally owned native service and session binding for recovery'
    return None


def validate_recovery_command(command, session):
    reason = recovery_unavailable(session)
    if reason:
        raise ValueError(reason)
    if (command.shared_service is None or command.attachment != session.attachment
            or service_handle(command.shared_service, session.service.endpoint) != session.service
            or command.rpc_proxy is None or not command.rpc_proxy.binds_session
            or command.rpc_proxy.upstream_path not in ('', session.service.endpoint.address)):
        raise ValueError('Automatic recovery requires the same service scope and validated attachment; plain fallback is disabled')


def attachment_ready(adapter, session):
    if session.attachment:
        if session.attachment.readiness == 'binding':
            return bool(session.native_id and (session.native_relay or {}).get('state') == 'ready')
        if session.attachment.readiness == 'process':
            return True
        if session.attachment.readiness == 'legacy-binding':
            return bool(session.native_id and session.state == 'running')
        return False
    describe = getattr(adapter, 'attachment_ready', None)
    if describe is not None:
        return describe(session)
    # Compatibility for independent providers implementing the original protocol.
    return (not session.endpoint or bool(session.native_id and
            adapter.capabilities(session)['send_steer'].state == 'supported'))


def legacy_attachment(shared_service=False):
    """Compatibility for pre-contract relays; never declares safe recovery."""
    return AttachmentSpec(work_owner='unknown', readiness='legacy-binding', observe_status=bool(shared_service))


def launch_attachment(command):
    if command.attachment is None and command.rpc_proxy is not None:
        return legacy_attachment(command.shared_service)
    return command.attachment


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
        if self.facts.get('native_refusal') and not self.stopping:
            raise ValueError(self.facts['native_refusal']['message'])
        if self.facts.get('binding_conflict') and not self.stopping:
            raise ValueError(self.facts['binding_conflict'])
        if self.stopping and observe(self.properties, self.facts)['live'] == 'dormant':
            return  # Adapter validates exact native identity; no pane control.
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


@contextmanager
def _snapshot_lock(directory):
    # UI measurements must never wait behind a long native operation. A read
    # whose binding needs repair is unavailable until it can be reconciled.
    with (directory / '.launch.lock').open('a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Native session identity is updating; retry shortly') from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def adapter_hook(adapter, name):
    """Optional contracts must be declared, not synthesized by __getattr__."""
    try:
        inspect.getattr_static(adapter, name)
    except AttributeError:
        return None
    hook = getattr(adapter, name)
    return hook if callable(hook) else None


def _observed(adapter, session):
    observer = adapter_hook(adapter, 'observed_binding')
    observed = observer(session) if observer else None
    if observed is not None:
        if (not isinstance(observed, ObservedBinding) or not isinstance(observed.evidence, str)
                or not observed.evidence or len(observed.evidence) > 128):
            raise ValueError('Invalid native session observation')
        normalize_session_id(observed.native_id)
        home = agents.sessions.canonical_home(observed.home)
        if session.home is not None and home != session.home:
            raise ValueError('Observed native storage differs from this launch')
    return observed


def session_snapshot(root, directory, properties, facts, *, adapter=None, launch_locked=False):
    """Reconcile adapter evidence before using a native ID. No native probes.

    A provider owns the evidence checks. Core owns launch fencing, the CAS,
    storage scope, ownership and the durable transition.
    """
    if not properties.get('harness') or not properties.get('workspace'):
        # Legacy/minimal records have no observation contract. Resume supplies
        # its normal workspace fallback before opting into this path.
        return properties, facts
    adapter = adapter or harnesses.load(root, properties['harness'])
    if adapter_hook(adapter, 'observed_binding') is None and not (directory / 'sessions.jsonl').exists():
        return properties, facts
    session = SessionHandle.from_records(properties, facts, directory)
    observed = _observed(adapter, session)
    effective, _ = agents.sessions.effective(directory, properties)
    changed = observed is not None and normalize_session_id(observed.native_id) != session.native_id
    if effective == properties and not changed and not (observed and facts.get('binding_conflict')):
        return properties, facts
    if not launch_locked:
        with _snapshot_lock(directory):
            return session_snapshot(root, directory, properties, facts, adapter=adapter, launch_locked=True)
    current_facts = agents.runtime(directory)
    if current_facts.get('launch_id') != facts.get('launch_id'):
        raise ValueError('Launch changed while reading native session identity; retry')
    current = agents.session_properties(root, directory, launch_locked=True)
    observed = _observed(adapter, SessionHandle.from_records(current, current_facts, directory))
    if observed is not None:
        target = normalize_session_id(observed.native_id)
        if target != current.get('native_session_id'):
            current = agents._rebind_session(root, directory, current.get('native_session_id'), target, 'detected',
                                            current_facts.get('launch_id'), home=observed.home)
        elif current_facts.get('binding_conflict'):
            agents.update_runtime(directory, current_facts['launch_id'], binding_conflict=None)
    return current, agents.runtime(directory)


def connection_problem(root, properties, facts):
    """A provider diagnostic cannot erase a valid listing row."""
    try:
        adapter = harnesses.load(root, properties['harness'])
        hook = adapter_hook(adapter, 'connection_problem')
        result = hook(SessionHandle.from_records(properties, facts, root / 'state' / 'agents' / properties['id'])) if hook else None
        return result if isinstance(result, str) and result else None
    except Exception:
        return None


def context(root, reference, *, reconcile=False):
    directory = agents.directory(root, reference)
    properties = read(directory / 'properties.json')
    facts = agents.runtime(directory)
    adapter = harnesses.load(root, properties['harness'])
    if reconcile:
        properties, facts = session_snapshot(root, directory, properties, facts, adapter=adapter, launch_locked=True)
    return (directory, adapter,
            SessionHandle.from_records(properties, facts, directory),
            NativeExecutor(properties['workspace'], properties, facts))


def stop(root, reference):
    directory, adapter, session, executor = context(root, reference)
    executor.stopping = True
    live = observe(executor.properties, executor.facts)['live']
    if live in ('stopped', 'dormant'):
        session = replace(session, state=live)
    result = adapter.stop(session, executor)
    for message_id in (result.value or {}).get('cancelled_message_ids', []):
        try:
            message_record(directory, message_id)
        except ValueError:
            continue  # Native input from another client is not a core inbox record.
        update_message(directory, message_id, submission_state='cancelled', cancelled_at=now(),
                       last_receipt={'state': 'unavailable', 'reason': 'Cancelled by explicit agent stop'})
    if result.state != 'ok':
        raise RuntimeError(f'Native stop incomplete: {result.reason}')
    cancelled = end_startup_messages(directory, session.launch_id, 'Cancelled by explicit agent stop', cancelled=True)
    return replace(result, value={**(result.value or {}), 'cancelled_startup_messages': cancelled})


def capabilities(root, reference, run_probe=False):
    directory = agents.directory(root, reference)
    with lock(directory / '.launch.lock'):
        _, adapter, session, executor = context(root, reference, reconcile=True)
    result = adapter.capabilities(session, executor if run_probe else None)
    # The same runtime snapshot the session came from, never a newer launch's.
    result.update(_plugin_capabilities(adapter, session, executor.facts, result))
    return {name: asdict(value) for name, value in result.items()}


def _plugin_info(adapter, session, facts):
    from ..plugins import harness as plugin_harness
    return plugin_harness, plugin_harness.session_info(session, getattr(adapter, 'family', ''), facts)


def _plugin_capabilities(adapter, session, facts, taken):
    """Capabilities plugins on in the agent's universe add; core's names always win. Never raises."""
    from ..harnesses.types import Capability
    try:
        plugin_harness, info = _plugin_info(adapter, session, facts)
        return {name: Capability(state, reason or '', mechanism)
                for name, (state, reason, mechanism) in plugin_harness.capabilities(info, set(taken)).items()}
    except Exception:
        return {}


def _adjusted_usage(adapter, session, facts, result):
    """An adapter's usage result after plugins' adjustments, only when it is ok. A plugin failure leaves it as measured."""
    if result.get('state') != 'ok' or not isinstance(result.get('value'), dict):
        return result
    try:
        plugin_harness, info = _plugin_info(adapter, session, facts)
        return {**result, 'value': plugin_harness.adjust_usage(info, result['value'])}
    except Exception:
        return result


def usage_snapshot(root, properties, facts, *, reconciled=False):
    """Nonblocking snapshot; listing can reuse its own already reconciled records."""
    try:
        adapter = harnesses.load(root, properties['harness'])
        directory = root / 'state' / 'agents' / properties['id']
        if not reconciled:
            properties, facts = session_snapshot(root, directory, properties, facts, adapter=adapter)
        if facts.get('binding_conflict'):
            return NativeResult('unavailable', facts['binding_conflict']).to_dict()
        session = SessionHandle.from_records(properties, facts, directory)
        result = adapter.get_usage(session).to_dict()
    except Exception as error:
        return NativeResult('error', str(error)).to_dict()
    return _adjusted_usage(adapter, session, facts, result)


def operation(root, reference, name, question=''):
    directory = agents.directory(root, reference)
    try:
        if name == 'ask' and not question.strip():
            raise ValueError('A side question is required')
        with lock(directory / '.launch.lock'):
            _, adapter, session, executor = context(root, directory.name, reconcile=True)
            if executor.facts.get('binding_conflict'):
                return NativeResult('unavailable', executor.facts['binding_conflict']).to_dict()
            if name == 'get_usage':
                result = adapter.get_usage(session, executor).to_dict()
                facts = executor.facts  # the launch the measurement belongs to
            elif name != 'ask':
                return NativeResult('unavailable', f'Unknown operation: {name}').to_dict()
        if name == 'get_usage':
            # Plugins adjust it outside the launch lock, so they never hold sends or stop.
            return _adjusted_usage(adapter, session, facts, result)
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


def flush_startup(root, directory, launch_id, error=None):
    # Same lock order as send. Setting ready and taking the batch is atomic with
    # new sends; hold send.lock through submission to preserve batch ordering.
    with lock(directory / '.send.lock'):
        with lock(directory / '.launch.lock'):
            facts = agents.runtime(directory)
            if facts.get('launch_id') != launch_id:
                error = 'Launch changed before delivery; use deliver AGENT --retry'
            elif facts.get('ending_at'):
                error = 'Launch stopped before delivery'
            if error:
                end_startup_messages(directory, launch_id, error, cancelled=bool(facts.get('ending_at')))
                if facts.get('launch_id') == launch_id:
                    agents.update_runtime(directory, launch_id, startup_delivery='failed', delivery_error=error)
                return
            agents.update_runtime(directory, launch_id, startup_delivery='ready')
            batch = [message['id'] for message in inbox(directory, all_messages=True, limit=None)['messages']
                     if message['tracking'].get('deferred_launch_id') == launch_id
                     and message['tracking']['submission_state'] == 'pending']
        for message_id in batch:
            deliver(root, directory.name, message_id)
    # Optional, so outside the send and launch locks, after the startup batch.
    from .lifecycle import note_ready
    note_ready(root, directory, launch_id)


def _session_changes(directory, properties, target):
    """The owned lineage from a message's target to the current conversation.

    An arbitrary matching ID is insufficient: the journal must still agree with
    this agent's current harness, canonical home and authoritative pointer.
    """
    effective, rows = agents.sessions.effective(directory, properties)
    if (effective != properties or not properties.get('native_session_home')
            or not rows or rows[-1]['to'] != properties.get('native_session_id')):
        return None
    # Returning to an earlier owned conversation can repeat an ID. Use its
    # latest departure, not an earlier lap through the lineage.
    starts = [index for index, row in enumerate(rows) if row['from'] == target]
    return rows[starts[-1]:] if starts else None


def retarget_message(directory, properties, message, tracking):
    """Caller holds launch.lock. Move only definitely unaccepted agent input."""
    target = tracking.get('target_session') or message.get('target_session')
    current = properties.get('native_session_id')
    if not target or target == current:
        return tracking
    changes = _session_changes(directory, properties, target)
    if not changes:
        raise ValueError('Message targets a different native session outside this agent\'s lineage; refusing delivery')
    if tracking.get('submission_state', 'pending') not in ('pending', 'failed'):
        raise ValueError('Message may already have been submitted; refusing to move it to another conversation')
    change = {'from': target, 'to': current, 'at': now(),
              'transitions': [{key: row[key] for key in ('id', 'reason')} for row in changes]}
    tracking = {**tracking, 'target_session': current,
                'session_changes': [*tracking.get('session_changes', []), change]}
    update_message(directory, message['id'], target_session=current, session_changes=tracking['session_changes'])
    return tracking


def _launch_only_delivery(adapter, session, delivery):
    """A declared missing delivery path, distinct from a broken connection."""
    # Connected sessions do not need an extra capability/process observation.
    if session.endpoint and session.native_id:
        return None
    capabilities = getattr(adapter, 'capabilities', None)
    if capabilities is None:
        return None
    capability = capabilities(session).get('send_follow_up' if delivery == 'follow-up' else 'send_steer')
    return capability if capability and capability.mechanism == 'launch-only' else None


def deliver(root, reference, message_id, retry=False, check=False):
    directory = agents.directory(root, reference)
    # Recipient submission is serialized with generation changes. Inbox operations
    # take their own shorter lock, separate from read-position changes.
    with lock(directory / '.launch.lock'), lock(directory / '.delivery.lock'):
        message, tracking = message_record(directory, message_id)
        try:
            _, adapter, session, executor = context(root, directory.name, reconcile=True)
        except Exception as error:
            receipt = {'state': 'error', 'ok': False, 'message_id': message_id,
                       'reason': f'Cannot load recipient adapter: {error}', 'at': now()}
            if tracking.get('submission_state', 'pending') in ('pending', 'failed'):
                update_message(directory, message_id, submission_state='failed',
                               attempts=[*tracking.get('attempts', []), receipt])
            return receipt
        if tracking.get('submission_state') == 'cancelled':
            return {**tracking['last_receipt'], 'message_id': message_id, 'reused': True, 'ok': False}
        if check and tracking.get('delivery_method') == 'resume_prompt' and tracking.get('submission_state') != 'failed':
            if tracking.get('submission_state') == 'unknown' and tracking.get('attempts'):
                return {**tracking['attempts'][-1], 'ok': False}
            return {'state': 'unknown', 'ok': False, 'reason': 'Interactive resume prompt has no correlatable native receipt; no resend',
                    'value': {'transcript_confirmed': None}}
        deferred = tracking.get('deferred_launch_id')
        if deferred and tracking.get('submission_state') == 'pending':
            if (deferred != session.launch_id or observe(executor.properties, executor.facts)['live'] != 'alive'
                    or executor.facts.get('startup_delivery') == 'failed'):
                reason = executor.facts.get('reason') if deferred == session.launch_id else None
                end_startup_messages(directory, deferred, (reason or 'Launch ended before delivery') + '; use deliver AGENT --retry')
                _, tracking = message_record(directory, message_id)
            elif check:
                return {'state': 'pending', 'ok': True, 'reason': 'Saved for native delivery once this launch is ready'}
        if check:
            if tracking.get('submission_state') == 'failed':
                return {**tracking['attempts'][-1], 'ok': False}
            target = tracking.get('target_session') or message.get('target_session')
            receipt_session = session
            if target and target != session.native_id:
                if not _session_changes(directory, executor.properties, target):
                    raise ValueError('Message targets a different native session; refusing reconciliation')
                receipt_session = replace(session, native_id=target)
            try:
                result = adapter.get_delivery(receipt_session, message_id, executor)
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
        previous = tracking.get('attempts', [])
        state = tracking.get('submission_state', 'pending')
        if state in ('accepted', 'unknown', 'submitting') or (state == 'failed' and not retry):
            receipt = previous[-1] if previous else {'state': 'unknown', 'reason': 'No submission receipt recorded; no resend'}
            if state == 'accepted':
                receipt = {**receipt, 'state': 'ok' if tracking.get('delivery_method') == 'resume_prompt' else 'native_queued',
                           'reason': 'Submission already recorded; no resend',
                           'value': tracking.get('native_receipt')}
            if state == 'submitting':
                receipt = {**receipt, 'state': 'unknown', 'reason': 'Previous submission was interrupted; acceptance unknown'}
                update_message(directory, message_id, submission_state='unknown')
            return {**receipt, 'reused': True, 'ok': receipt['state'] not in ('error', 'unavailable', 'unknown')}
        if executor.facts.get('native_refusal'):
            receipt = {'state': 'error', 'ok': False, 'reason': executor.facts['native_refusal']['message']}
            update_message(directory, message_id, submission_state='failed', attempts=[*previous, receipt])
            return receipt
        tracking = retarget_message(directory, executor.properties, message, tracking)
        target = tracking.get('target_session') or message.get('target_session')
        relay_lost = (session.native_relay or {}).get('state') == 'lost'
        if not relay_lost and executor.facts.get('startup_delivery') == 'waiting' and observe(executor.properties, executor.facts)['live'] == 'alive':
            update_message(directory, message_id, submission_state='pending', deferred_launch_id=session.launch_id,
                           delivery_method='native', target_session=target or session.native_id)
            return {'state': 'pending', 'ok': True, 'reason': 'Saved for native delivery once this launch is ready'}
        try:
            executor.check_session()
        except Exception as error:
            result = {'state': 'error', 'reason': f'Pre-submission session check failed: {error}', 'ok': False, 'message_id': message_id}
            update_message(directory, message_id, submission_state='failed', attempts=[*previous, result])
            return result
        launch_only = _launch_only_delivery(adapter, session, selected_mode)
        if launch_only is not None:
            receipt = {'state': 'unavailable', 'ok': False, 'reason': launch_only.reason,
                       'value': {'launch_only': True}}
            update_message(directory, message_id, submission_state='failed', attempts=[*previous, receipt])
            return receipt
        attempt = {'state': 'unknown', 'reason': 'Submission in progress', 'message_id': message_id,
                   'native_session_id': session.native_id, 'mode': selected_mode, 'agent_id': session.agent_id,
                   'launch_id': session.launch_id, 'at': now()}
        # Persist BEFORE the native call: a crash cannot turn uncertain submission
        # into pending and silently replay it on restart.
        update_message(directory, message_id, submission_state='submitting', target_session=target or session.native_id,
                       delivery_method='native', attempts=[*previous, attempt])
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


def send_or_wake(root, reference, message_id, retry=False):
    """Caller holds .send.lock. Release .launch.lock before start takes it."""
    # Waking a stopped agent launches it, and launching uses this module: the one
    # import that has to wait until the call.
    from . import lifecycle
    directory = agents.directory(root, reference)
    with lock(directory / '.launch.lock'):
        message, tracking = message_record(directory, message_id)
        _, adapter, session, executor = context(root, reference, reconcile=True)
        live = observe(executor.properties, executor.facts)['live']
        state = tracking.get('submission_state', 'pending')
        eligible = state == 'pending' or (retry and state == 'failed')
        wake = eligible and live in ('stopped', 'dormant') and message.get('delivery') in ('steer', 'follow-up')
        capability = adapter.capabilities(session).get('wake') if wake else None
        if wake and (capability is None or capability.state != 'supported'):
            launch_only = _launch_only_delivery(adapter, session, message['delivery']) is not None
            receipt = {'state': 'unavailable', 'ok': False, 'reason': 'This harness cannot wake with an interactive resume prompt',
                       'value': {'launch_only': launch_only, 'wake_refused': True}}
            update_message(directory, message_id, submission_state='failed', attempts=[*tracking.get('attempts', []), receipt])
            return receipt
    if not wake:
        return deliver(root, reference, message_id, retry=retry)
    try:
        previous_socket = (executor.facts.get('tmux') or {}).get('socket')
        launched = lifecycle.start(root, resume=reference, resume_message_id=message_id, socket_path=previous_socket)
        _, tracking = message_record(directory, message_id)
        if tracking.get('submission_state') in ('accepted', 'failed', 'unknown'):
            receipt = tracking['attempts'][-1]
        else:
            receipt = {'state': 'pending', 'ok': True, 'reason': 'Resuming with this message as the interactive starting prompt',
                       'value': {'launch_id': launched['runtime']['launch_id'], 'transcript_confirmed': None}}
        return {**receipt, 'notices': launched['notices']} if launched.get('notices') else receipt
    except Exception as error:
        _, tracking = message_record(directory, message_id)
        receipt = {'state': 'error', 'ok': False, 'reason': f'Could not wake agent: {error}',
                   'value': {'wake_refused': True}}
        # If startup was registered, uncertainty belongs to the runner. Never
        # turn a possibly submitted command-line prompt into a retryable failure.
        if tracking.get('submission_state', 'pending') in ('pending', 'failed'):
            update_message(directory, message_id, submission_state='failed', attempts=[*tracking.get('attempts', []), receipt])
        return receipt
