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
                            # The snapshot response supersedes earlier events.
                            continue
                        if value.get('id') != identifier:
                            raise ValueError('Native RPC response ID mismatch')
                        break
                    if request.method == 'initialize' and 'result' in value:
                        await connection.send(json.dumps({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}}))
                    try:
                        request = plan.send(value)
                    except StopIteration as done:
                        initial = done.value
                        if not isinstance(initial, NativeResult) or initial.state != 'ok':
                            return
                        publish(initial.value)
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


class LaunchProxy:
    """A runner-owned local relay into a shared service, with adapter transforms."""
    def __init__(self, spec, protocol, bind, record_error=lambda error: None):
        import threading
        self.spec, self.protocol, self.bind = spec, protocol, bind
        self.record_error = record_error
        self.stopped, self.ready = threading.Event(), threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name='native-launch-proxy', daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5):
            self.close()
            raise RuntimeError('Native launch relay did not start')
        if self.error:
            raise RuntimeError(f'Native launch relay failed: {self.error}')

    def close(self):
        self.stopped.set()
        self.thread.join()

    def _record_error(self, error):
        try:
            self.record_error(error)
        except (OSError, ValueError):
            pass  # Outdated launches cannot write diagnostics into a new runtime.

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception as error:
            self.error = error
        finally:
            self.ready.set()

    async def _serve(self):
        from websockets.asyncio.server import unix_serve
        async with unix_serve(self._connection, self.spec['socket_path'], compression=None,
                              close_timeout=.2, max_size=1024 * 1024, max_queue=8):
            self.ready.set()
            while not self.stopped.is_set():
                await asyncio.sleep(.1)

    async def _connection(self, client):
        try:
            async with unix_connect(self.spec['upstream_path'], compression=None, open_timeout=5,
                                    close_timeout=.2, max_size=1024 * 1024, max_queue=8) as upstream:
                async def forward(source, target, outgoing):
                    try:
                        async for payload in source:
                            try:
                                value = json.loads(payload)
                            except (ValueError, UnicodeError):
                                if isinstance(payload, bytes):
                                    await target.send(payload)
                                    continue
                                raise ValueError('Native relay received malformed JSON')
                            if not isinstance(value, dict):
                                raise ValueError('Native relay received a non-object')
                            original = value
                            if outgoing:
                                try:
                                    value = self.protocol.outgoing(value)
                                except (ValueError, TypeError, KeyError) as error:
                                    self._record_error(str(error))
                                    await client.send(json.dumps({'id': value.get('id'), 'error': {
                                        'code': -32602, 'message': f'Native launch preparation failed: {error}'}}))
                                    continue
                            else:
                                native_id = self.protocol.incoming(value)
                                if native_id is not None:
                                    self.bind(native_id)
                            if value is not original:
                                encoded = json.dumps(value, ensure_ascii=False)
                                if len(encoded.encode()) > 1024 * 1024:
                                    raise ValueError('Native relay frame exceeds 1 MiB')
                                payload = encoded.encode() if isinstance(payload, bytes) else encoded
                            await target.send(payload)
                    finally:
                        code = source.close_code
                        if code is not None:
                            await target.close(code=code if code != 1006 else 1011,
                                               reason=source.close_reason or '')
                tasks = [asyncio.create_task(forward(client, upstream, True)),
                         asyncio.create_task(forward(upstream, client, False))]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, ValueError, TypeError, WebSocketException) as error:
            self._record_error(str(error))
            # A relay never fabricates answers to native approval requests.
            await client.close(code=1011, reason='Native launch relay failed; see runtime.json')
