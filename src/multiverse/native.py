"""Shared native controls. Core executes adapter specifications and owns receipts.

Durable send/inbox/ack never depend on these best-effort operations.
"""
from dataclasses import asdict, replace
import json
import os
import subprocess

from . import agents, harnesses
from .harnesses.types import Capability, NativeContext, NativeResult, CommandSpec, RpcSpec
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
                                          observed.reason, mechanism='codex-app-server')
                continue
            output = execute(command, workspace, timeout=5)
            if output.returncode:
                detail = output.stderr.strip() or output.stdout.strip()
                unsupported = any(word in detail.lower() for word in ('unrecognized subcommand', 'unknown command', 'unexpected argument'))
                result[name] = Capability('unavailable' if unsupported else 'error', f'Native probe exited {output.returncode}: {detail}')
            elif name == 'native_queue' and all(flag in output.stdout for flag in ('--thread', '--message')):
                result[name] = Capability('supported', mechanism='codex-queue')
            else:
                result[name] = Capability('unavailable', 'Installed native help does not advertise the required interface')
        except FileNotFoundError as error:
            result[name] = Capability('unavailable', str(error))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result[name] = Capability('error', str(error))
    return result


def context(root, reference, run_probe=False):
    directory = agents.directory(root, reference)
    properties = read(directory / 'properties.json')
    adapter = harnesses.load(root, properties['harness'])
    facts = agents.runtime(directory)
    probes = probe(adapter, properties['workspace'], NativeContext(properties, facts)) if run_probe else {}
    return directory, adapter, NativeContext(properties, facts, probes)


def capabilities(root, reference, run_probe=False):
    _, adapter, ctx = context(root, reference, run_probe)
    return {name: asdict(value) for name, value in adapter.capabilities(ctx).items()}


def observation(root, properties, facts, operation='usage'):
    """Nonblocking optional observation; failures are data, never dashboard exceptions."""
    try:
        adapter = harnesses.load(root, properties['harness'])
        result = adapter.control(operation, NativeContext(properties, facts), '')
        if isinstance(result, (CommandSpec, RpcSpec)):
            return NativeResult('unavailable', 'Explicit native control required').to_dict()
        return result.to_dict()
    except Exception as error:
        return NativeResult('error', str(error)).to_dict()


def control(root, reference, operation):
    """Explicit optional operation. Unsupported controls never fall back to terminal keys."""
    directory = agents.directory(root, reference)
    with lock(directory / '.launch.lock'):
        _, adapter, ctx = context(root, reference)
        result = adapter.control(operation, ctx, '')
        if isinstance(result, (CommandSpec, RpcSpec)):
            return NativeResult('unavailable', 'This operation needs an explicit implementation').to_dict()
        return result.to_dict()


def deliver(root, reference, message_id, retry=False, mode=None):
    directory = agents.directory(root, reference)
    # A resume cannot change generations while a queue operation is in flight.
    with lock(directory / '.launch.lock'), lock(directory / '.delivery.lock'):
        _, adapter, ctx = context(root, reference, run_probe=True)
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
        text = ('Collaborator message from the Multiverse inbox (not system instructions). '
                'Check/ack your inbox after reading; reply with --reply-to the message ID.\n' +
                json.dumps(message, ensure_ascii=False))
        native_id = ctx.properties.get('native_session_id')
        mode = mode or message.get('delivery', 'steer')
        operation = {'steer': 'steer', 'follow-up': 'follow_up', 'native-queue': 'native_queue'}.get(mode)
        if operation is None:
            raise ValueError(f'Unknown delivery mode: {mode}')
        try:
            result = adapter.control(operation, replace(ctx, message_id=message_id), text)
            if isinstance(result, NativeResult) and result.state == 'pending' and mode != message.get('delivery', 'steer'):
                result = NativeResult('unavailable', 'The Pi bridge honors the durable message delivery mode; overrides are unsupported')
            if isinstance(result, CommandSpec):
                # Never execute native controls against a stale or unowned terminal.
                owned(dict(ctx.properties), dict(ctx.runtime))
                output = execute(result, ctx.properties['workspace'])
                result = (NativeResult('native_queued', 'Accepted by the native queue; delivery, read and answer unconfirmed')
                          if output.returncode == 0 else
                          NativeResult('error', f'Native queue exited {output.returncode}: {output.stderr.strip() or output.stdout.strip()}'))
            elif isinstance(result, RpcSpec):
                owned(dict(ctx.properties), dict(ctx.runtime))
                result = execute_rpc(result, ctx.properties['workspace'])
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result = NativeResult('error', f'{error}; acceptance may be unknown; retry can duplicate')
        receipt = {**result.to_dict(), 'ok': result.state not in ('unavailable', 'error'), 'message_id': message_id, 'native_session_id': native_id,
                   'mode': mode, 'agent_id': ctx.properties['id'], 'launch_id': ctx.runtime.get('launch_id'), 'at': now()}
        history[message_id] = [*previous, receipt]
        write(receipts, history)
        # Native queue acceptance is not inbox acknowledgment.
        return receipt
