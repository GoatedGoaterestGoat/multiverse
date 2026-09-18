"""Shared native controls. Core executes adapter specifications and owns receipts.

Durable send/inbox/ack never depend on these best-effort operations.
"""
from dataclasses import asdict
import os
import subprocess

from . import agents, harnesses
from .harnesses.types import Capability, NativeResult, CommandSpec, RpcSpec, SessionHandle, Message
from .store import inbox, lock, now, read, write
from .tmux import owned


def environment(command):
    return {k: v for k, v in os.environ.items() if not k.startswith('MV2_') and k not in command.unset_env}


def execute(command: CommandSpec, workspace, timeout=10):
    env = environment(command)
    # Never use a shell: message text and native IDs remain literal argv values.
    return subprocess.run(command.argv, cwd=workspace, env=env, text=True, capture_output=True, timeout=timeout)


def execute_rpc(spec, workspace, timeout=10):
    from .rpc import execute as run
    return run(spec, timeout)


def probe(adapter, workspace, ctx=None):
    result = {}
    for name, command in adapter.probes(ctx).items():
        try:
            if isinstance(command, RpcSpec):
                observed = execute_rpc(command, workspace, timeout=5)
                result[name] = Capability('supported' if observed.state == 'ok' else observed.state,
                                          observed.reason, mechanism='native-rpc')
                continue
            output = execute(command.command, workspace, timeout=5)
            result[name] = command.interpret(output)
        except FileNotFoundError as error:
            result[name] = Capability('unavailable', str(error))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result[name] = Capability('error', str(error))
    return result


class NativeExecutor:
    def __init__(self, workspace, properties=None, facts=None):
        self.workspace, self.properties, self.facts = workspace, properties, facts

    def probe(self, adapter, context):
        return probe(adapter, self.workspace, context)

    def check_session(self):
        if self.properties is None or self.facts is None:
            raise ValueError('No running session selected')
        owned(self.properties, self.facts)

    def command(self, specification, timeout=10):
        self.check_session()
        return execute(specification, self.workspace, timeout)

    def rpc(self, specification, timeout=10):
        self.check_session()
        return execute_rpc(specification, self.workspace, timeout)


def context(root, reference):
    directory = agents.directory(root, reference)
    properties = read(directory / 'properties.json')
    facts = agents.runtime(directory)
    return (directory, harnesses.load(root, properties['harness']),
            SessionHandle.from_records(properties, facts, directory),
            NativeExecutor(properties['workspace'], properties, facts))


def capabilities(root, reference, run_probe=False):
    _, adapter, session, executor = context(root, reference)
    return {name: asdict(value) for name, value in adapter.capabilities(session, executor if run_probe else None).items()}


def observation(root, properties, facts, operation='usage'):
    """Nonblocking snapshot: optional failures are data, never UI exceptions."""
    try:
        adapter = harnesses.load(root, properties['harness'])
        directory = root / 'state' / 'agents' / properties['id']
        session = SessionHandle.from_records(properties, facts, directory)
        return (adapter.get_usage(session) if operation == 'usage' else adapter.observe(session)).to_dict()
    except Exception as error:
        return NativeResult('error', str(error)).to_dict()


def operation(root, reference, name, question=''):
    directory = agents.directory(root, reference)
    with lock(directory / '.launch.lock'):
        try:
            _, adapter, session, executor = context(root, directory.name)
            if name == 'ask':
                if not question.strip():
                    raise ValueError('A side question is required')
                result = adapter.ask(session, question, executor)
            elif name == 'get_usage':
                result = adapter.get_usage(session, executor)
            elif name == 'observe':
                result = adapter.observe(session, executor)
                if result.state == 'ok':
                    result = NativeResult('ok', result.reason, {**(result.value or {}),
                        **agents.observe(executor.properties, executor.facts)})
            else:
                return NativeResult('unavailable', f'Unknown operation: {name}').to_dict()
            return result.to_dict()
        except Exception as error:
            return NativeResult('error', str(error)).to_dict()


def control(root, reference, operation_name):
    """Compatibility for the old native-status tool; new callers use named operations."""
    if operation_name in ('usage', 'activity'):
        return operation(root, reference, {'usage': 'get_usage', 'activity': 'observe'}[operation_name])
    return NativeResult('unavailable', 'Use a named operation; no native side effect attempted').to_dict()


def deliver(root, reference, message_id, retry=False, mode=None):
    directory = agents.directory(root, reference)
    # A resume cannot change generations while a queue operation is in flight.
    with lock(directory / '.launch.lock'), lock(directory / '.delivery.lock'):
        _, adapter, session, executor = context(root, directory.name)
        batch = inbox(directory, limit=100000)
        message = next((item for item in batch['messages'] if item['id'] == message_id), None)
        if message is None:
            raise ValueError('Message is not in this agent\'s unread inbox; refusing native delivery')
        receipts = directory / 'native-delivery.json'
        history = read(receipts) if receipts.exists() else {}
        previous = history.get(message_id, [])
        if previous and previous[-1]['state'] in ('native_queued', 'error') and not retry:
            return {**previous[-1], 'reused': True,
                    'retry_note': 'Use --retry to attempt again; acceptance may cause duplicate delivery'}
        mode = mode or message.get('delivery', 'steer')
        try:
            result = adapter.send(session, Message(message['id'], message['text'], message.get('from', {}),
                                                   message.get('reply_to'), message.get('delivery', 'steer')), mode, executor)
        except Exception as error:
            result = NativeResult('error', f'{error}; acceptance may be unknown; retry can duplicate')
        receipt = {**result.to_dict(), 'ok': result.state not in ('unavailable', 'error'), 'message_id': message_id, 'native_session_id': session.native_id,
                   'mode': mode, 'agent_id': session.agent_id, 'launch_id': session.launch_id, 'at': now()}
        history[message_id] = [*previous, receipt]
        write(receipts, history)
        # Native queue acceptance is not inbox acknowledgment.
        return receipt
