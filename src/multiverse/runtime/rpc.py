"""Bounded core transport for adapter-owned native JSON-RPC control plans.

This connects only to a declared local Unix WebSocket endpoint. It never starts,
resumes, interrupts or stops a native server, and never answers native requests.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4

from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException

from ..harnesses.types import NativeResult


async def _exchange(connection, plan, request, max_requests, send):
    """Send a plan's requests in turn, feeding it each response, until it returns.

    Returns (True, what the plan returned), or (False, None) when it wants more
    than `max_requests`. Notifications that arrive before a response are skipped:
    approvals belong to the attached native TUI (even an error response could
    resolve a native permission request), and a snapshot response supersedes
    earlier events. `send(message, kind)` writes a request or notification.
    """
    for identifier in range(1, max_requests + 1):
        await send({'jsonrpc': '2.0', 'id': identifier, 'method': request.method, 'params': request.params}, 'request')
        while True:
            value = json.loads(await connection.recv())
            if not isinstance(value, dict):
                raise ValueError('Native RPC returned a non-object')
            if 'method' in value:
                continue
            if value.get('id') != identifier:
                raise ValueError('Native RPC response ID mismatch')
            break
        if 'result' in value and 'error' not in value:
            for notification in request.on_success:
                await send({'jsonrpc': '2.0', 'method': notification.method, 'params': notification.params}, 'notification')
        try:
            request = plan.send(value)
        except StopIteration as done:
            return True, done.value
    return False, None


async def _execute(spec, timeout, record_connection=None):
    plan = spec.plan()
    connection_id = str(uuid4())
    if record_connection:
        record_connection({'id': connection_id, 'role': 'control RPC', 'endpoint': spec.socket_path, 'state': 'opening'})
    try:
        async with asyncio.timeout(timeout):
            async with unix_connect(spec.socket_path, compression=None, open_timeout=timeout,
                                    close_timeout=0.2, ping_interval=None, max_size=1024 * 1024,
                                    max_queue=8) as connection:
                async def send(value, _kind):
                    payload = json.dumps(value, ensure_ascii=False)
                    if len(payload.encode()) > 1024 * 1024:
                        raise ValueError('Native RPC request exceeds 1 MiB')
                    await connection.send(payload)

                request = next(plan)
                if not 1 <= spec.max_requests <= 256:
                    raise ValueError('Native RPC request budget must be between 1 and 256')
                finished, result = await _exchange(connection, plan, request, spec.max_requests, send)
                if not finished:
                    raise ValueError(f'Native RPC plan exceeds {spec.max_requests} requests')
                if not isinstance(result, NativeResult):
                    raise ValueError('Native RPC plan did not return a NativeResult')
                return result
    except WebSocketException as error:
        raise RuntimeError(f'Native RPC transport failed: {error}; acceptance may be unknown') from error
    except TimeoutError as error:
        raise TimeoutError('Native RPC timed out; acceptance may be unknown') from error
    finally:
        plan.close()
        if record_connection:
            record_connection({'id': connection_id, 'role': 'control RPC', 'endpoint': spec.socket_path, 'state': 'closed'})


def execute(spec, timeout=10, record_connection=None):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # Shared synchronous tools may be called by a TUI event handler.
        # Keep the bounded protocol on its own loop in that case.
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, _execute(spec, timeout, record_connection)).result()
    return asyncio.run(_execute(spec, timeout, record_connection))


async def watch(spec, interpret, publish, timeout=5, *, record_connection=None):
    """Run an adapter's bounded snapshot plan, then consume passive notifications.

    The caller cancels this task at launch end. Connection loss or protocol errors
    invalidate the observation; no reconnect, server startup or request replies.
    """
    plan = spec.plan()
    connection_id = str(uuid4())
    if record_connection:
        record_connection({'id': connection_id, 'role': 'status stream', 'endpoint': spec.socket_path, 'state': 'opening'})
    try:
        async with unix_connect(spec.socket_path, compression=None, open_timeout=timeout,
                                close_timeout=0.2, ping_interval=20, ping_timeout=20,
                                max_size=1024 * 1024, max_queue=8) as connection:
            async with asyncio.timeout(timeout):
                async def send(value, kind):
                    payload = json.dumps(value)
                    if len(payload.encode()) > 1024 * 1024:
                        raise ValueError(f'Native RPC {kind} exceeds 1 MiB')
                    await connection.send(payload)

                if not 1 <= spec.max_requests <= 256:
                    raise ValueError('Native RPC request budget must be between 1 and 256')
                finished, initial = await _exchange(connection, plan, next(plan), spec.max_requests, send)
                if not finished:
                    raise ValueError('Native RPC snapshot exceeded request budget')
                if not isinstance(initial, NativeResult) or initial.state != 'ok':
                    return
                publish(initial.value)
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
        if record_connection:
            record_connection({'id': connection_id, 'role': 'status stream', 'endpoint': spec.socket_path, 'state': 'closed'})
        publish('unknown')


class LaunchProxy:
    """A runner-owned local relay into a shared service, with adapter transforms."""
    # Interactive replies can contain an entire resumed conversation, including
    # tool output and images. The small control-RPC budget is inappropriate here.
    MAX_MESSAGE_SIZE = 64 * 1024 * 1024
    MAX_QUEUE = 2
    POST_BIND_TIMEOUT = 3

    def __init__(self, spec, protocol, bind, record_error=lambda error: None,
                 publish_usage=lambda usage: None, record_lost=lambda reason: None,
                 record_refusal=lambda evidence: None, record_connection=None):
        import threading
        self.spec, self.protocol, self.bind = spec, protocol, bind
        self.record_error = record_error
        self.publish_usage = publish_usage
        self.record_lost = record_lost
        self.record_refusal = record_refusal
        self.record_connection = record_connection
        self.owner = None
        self.lost = None
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
        self.thread.join(5)
        if self.thread.is_alive():
            raise TimeoutError('Native launch relay is still closing')

    def _record_error(self, error):
        try:
            self.record_error(error)
        except (OSError, ValueError):
            pass  # Outdated launches cannot write diagnostics into a new runtime.

    def _lost(self, reason):
        if self.stopped.is_set() or self.lost is not None:
            return
        self.lost = reason
        try:
            self.record_lost(reason)
        except (OSError, ValueError):
            pass

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception as error:
            self.error = error
            self._lost(str(error))
        finally:
            self.ready.set()

    async def _serve(self):
        from websockets.asyncio.server import unix_serve
        async with unix_serve(self._connection, self.spec['socket_path'], compression=None,
                              close_timeout=.2, max_size=self.MAX_MESSAGE_SIZE, max_queue=self.MAX_QUEUE):
            self.ready.set()
            while not self.stopped.is_set():
                await asyncio.sleep(.1)

    async def _connection(self, client):
        if self.lost is not None:
            await client.close(code=1011, reason='Native relay lost; end this launch and resume the agent')
            return
        ended = None
        failure = None
        connection_id = str(uuid4())
        if self.record_connection:
            self.record_connection({'id': connection_id, 'role': 'terminal relay',
                                    'endpoint': self.spec['upstream_path'], 'state': 'opening'})
        try:
            async with unix_connect(self.spec['upstream_path'], compression=None, open_timeout=5,
                                    close_timeout=.2, max_size=self.MAX_MESSAGE_SIZE,
                                    max_queue=self.MAX_QUEUE) as upstream:
                async def forward(source, target, outgoing):
                    nonlocal ended
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
                                try:
                                    native_id = self.protocol.incoming(value)
                                except (ValueError, TypeError, KeyError) as error:
                                    if not getattr(self.protocol, 'bound', False):
                                        self.record_refusal({'source': 'adapter-validation', 'method': 'binding',
                                                             'code': None, 'message': str(error), 'native_id': None})
                                    raise
                                refusal = getattr(self.protocol, 'launch_refusal', None)
                                if refusal is not None:
                                    self.record_refusal(refusal)
                                if native_id is not None:
                                    self.bind(native_id)
                                    self.owner = client
                                    after_bind = getattr(self.protocol, 'after_bind', None)
                                    if after_bind is not None:
                                        try:
                                            spec = after_bind(self.spec['upstream_path'])
                                            if spec is not None:
                                                observed = await _execute(spec, self.POST_BIND_TIMEOUT, self.record_connection)
                                                if observed.state != 'ok':
                                                    raise ValueError(observed.reason)
                                        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
                                            self._record_error(f'Native session naming failed: {error}')
                                observe = getattr(self.protocol, 'usage', None)
                                if observe is not None:
                                    try:
                                        usage = observe(value)
                                        if usage is not None:
                                            self.publish_usage(usage)
                                    except (OSError, ValueError, TypeError, KeyError):
                                        pass  # Optional telemetry must not disrupt the TUI.
                            if value is not original:
                                encoded = json.dumps(value, ensure_ascii=False)
                                if len(encoded.encode()) > self.MAX_MESSAGE_SIZE:
                                    raise ValueError('Native relay message exceeds size limit')
                                payload = encoded.encode() if isinstance(payload, bytes) else encoded
                            await target.send(payload)
                    finally:
                        code = source.close_code
                        if ended is None:
                            ended = ('terminal' if outgoing else 'daemon', code, source.close_reason)
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
            failure = str(error)
            self._record_error(str(error))
            # A relay never fabricates answers to native approval requests.
            await client.close(code=1011, reason='Native launch relay failed; see runtime.json')
        finally:
            if self.record_connection:
                self.record_connection({'id': connection_id, 'role': 'terminal relay',
                                        'endpoint': self.spec['upstream_path'], 'state': 'closed'})
            # Only the bound TUI connection owns launch health. A secondary
            # client's failure must not strand a healthy primary connection.
            normal_exit = ended and ended[0] == 'terminal' and ended[1] in (1000, 1001)
            if (self.owner is client or self.owner is None) and not normal_exit:
                if failure or ended:
                    reason = (f'{ended[0]} connection closed (code {ended[1]}): '
                              f'{failure or ended[2] or "no reason supplied"}' if ended else failure)
                    self._lost(reason)
