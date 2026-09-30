"""Bounded core transport for adapter-owned native JSON-RPC control plans.

This connects only to a declared local Unix WebSocket endpoint. It never starts,
resumes, interrupts or stops a native server, and never answers native requests.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException

from .harnesses.types import NativeResult


async def _execute(spec, timeout):
    plan = spec.plan()
    try:
        async with asyncio.timeout(timeout):
            async with unix_connect(spec.socket_path, compression=None, open_timeout=timeout,
                                    close_timeout=0.2, ping_interval=None, max_size=1024 * 1024,
                                    max_queue=8) as connection:
                async def send(value):
                    payload = json.dumps(value, ensure_ascii=False)
                    if len(payload.encode()) > 1024 * 1024:
                        raise ValueError('Native RPC request exceeds 1 MiB')
                    await connection.send(payload)

                request = next(plan)
                if not 1 <= spec.max_requests <= 256:
                    raise ValueError('Native RPC request budget must be between 1 and 256')
                for identifier in range(1, spec.max_requests + 1):
                    await send({'jsonrpc': '2.0', 'id': identifier, 'method': request.method, 'params': request.params})
                    while True:
                        value = json.loads(await connection.recv())
                        if not isinstance(value, dict):
                            raise ValueError('Native RPC returned a non-object')
                        if 'method' in value:
                            # Approval belongs to the attached native TUI. Even an
                            # error response could resolve a native permission request.
                            continue
                        if value.get('id') != identifier:
                            raise ValueError('Native RPC response ID mismatch')
                        break
                    if request.method == 'initialize' and 'result' in value:
                        await send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
                    try:
                        request = plan.send(value)
                    except StopIteration as done:
                        if not isinstance(done.value, NativeResult):
                            raise ValueError('Native RPC plan did not return a NativeResult')
                        return done.value
                raise ValueError(f'Native RPC plan exceeds {spec.max_requests} requests')
    except WebSocketException as error:
        raise RuntimeError(f'Native RPC transport failed: {error}; acceptance may be unknown') from error
    except TimeoutError as error:
        raise TimeoutError('Native RPC timed out; acceptance may be unknown') from error
    finally:
        plan.close()


def execute(spec, timeout=10):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # Shared synchronous tools may be called by a TUI event handler.
        # Keep the bounded protocol on its own loop in that case.
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, _execute(spec, timeout)).result()
    return asyncio.run(_execute(spec, timeout))


async def watch(spec, interpret, publish, timeout=5):
    """Run an adapter's bounded snapshot plan, then consume passive notifications.

    The caller cancels this task at launch end. Connection loss or protocol errors
    invalidate the observation; no reconnect, server startup or request replies.
    """
    plan = spec.plan()
    try:
        async with unix_connect(spec.socket_path, compression=None, open_timeout=timeout,
                                close_timeout=0.2, ping_interval=20, ping_timeout=20,
                                max_size=1024 * 1024, max_queue=8) as connection:
            latest = None
            async with asyncio.timeout(timeout):
                if not 1 <= spec.max_requests <= 256:
                    raise ValueError('Native RPC request budget must be between 1 and 256')
                request = next(plan)
                for identifier in range(1, spec.max_requests + 1):
                    payload = json.dumps({'jsonrpc': '2.0', 'id': identifier,
                                          'method': request.method, 'params': request.params})
                    if len(payload.encode()) > 1024 * 1024:
                        raise ValueError('Native RPC request exceeds 1 MiB')
                    await connection.send(payload)
                    while True:
                        value = json.loads(await connection.recv())
                        if not isinstance(value, dict):
                            raise ValueError('Native RPC returned a non-object')
                        if 'method' in value:
                            observed = interpret(value)
                            if observed is not None:
                                latest = observed
                            continue
                        if value.get('id') != identifier:
                            raise ValueError('Native RPC response ID mismatch')
                        break
                    if request.method == 'initialize' and 'result' in value:
                        await connection.send(json.dumps({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}}))
                        # Events preceding the snapshot request cannot override it.
                        latest = None
                    try:
                        request = plan.send(value)
                    except StopIteration as done:
                        initial = done.value
                        if not isinstance(initial, NativeResult) or initial.state != 'ok':
                            return
                        publish(initial.value if latest is None else latest)
                        break
                else:
                    raise ValueError('Native RPC snapshot exceeded request budget')
            async for payload in connection:
                value = json.loads(payload)
                if not isinstance(value, dict):
                    raise ValueError('Native RPC returned a non-object')
                observed = interpret(value)
                if observed is not None:
                    publish(observed)
    except (OSError, WebSocketException, TimeoutError, ValueError):
        pass  # Status is optional; native terminal execution remains authoritative.
    finally:
        plan.close()
        publish('unknown')
