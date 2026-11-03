"""Bounded private stdio relay. The supervisor never imports or runs adapter code."""
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import time

from . import run_supervisor as supervisor


def encode(value):
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > supervisor.WIRE_BYTES:
        raise ValueError('A private protocol frame exceeds its size bound')
    return struct.pack('!Q', len(data)) + data


class Channel:
    def __init__(self, connection, check):
        self.connection, self.check = connection, check
        self.buffer = bytearray()
        self.final = None

    def write(self, value):
        pending = memoryview(encode(value))
        while pending:
            self.check()
            _, ready, _ = select.select([], [self.connection], [], .05)
            if ready:
                pending = pending[self.connection.send(pending[:65536]):]

    def read(self):
        from ..harnesses.types import RunFailure
        while True:
            self.check()
            if len(self.buffer) >= 8:
                size = struct.unpack('!Q', self.buffer[:8])[0]
                if size > supervisor.WIRE_BYTES:
                    raise RunFailure('failed', 'A private protocol response exceeds its size bound')
                if len(self.buffer) >= 8 + size:
                    value = json.loads(self.buffer[8:8 + size])
                    del self.buffer[:8 + size]
                    if isinstance(value, dict) and ('outcome' in value or 'failure' in value):
                        self.final = value
                    return value
            ready, _, _ = select.select([self.connection], [], [], .05)
            if ready:
                chunk = self.connection.recv(65536)
                if not chunk:
                    raise RunFailure('failed', 'The private protocol supervisor disconnected', confirmed=False)
                self.buffer.extend(chunk)

    def send(self, frame):
        from ..harnesses.types import RunFailure
        if not isinstance(frame, dict):
            raise RunFailure('invalid_request', 'A private native frame must be an object')
        self.write({'input': frame})

    def receive(self):
        from ..harnesses.types import RunFailure
        value = self.read()
        if 'event' in value and isinstance(value['event'], dict):
            return value['event']
        problem(value)
        raise RunFailure('failed', 'The private native process ended before its dialogue completed')


def problem(value):
    from ..harnesses.types import RunFailure
    if issue := value.get('failure'):
        if issue.get('gone'):
            from .runs import _gone
            raise _gone(issue['gone'])
        raise RunFailure(issue['kind'], issue['reason'], confirmed=issue.get('confirmed'))


def execute(executor, payload, dialogue):
    from ..harnesses.types import RunFailure
    connection, child = socket.socketpair()
    process, outcome, caught, reply = None, None, None, None
    try:
        source = str(Path(__file__).resolve().parents[2])
        boot = ('import sys;sys.path.insert(0,sys.argv.pop(1));'
                'from multiverse.runtime.run_supervisor import main;main()')
        process = subprocess.Popen([sys.executable, '-I', '-S', '-c', boot, source, str(child.fileno())],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   pass_fds=(child.fileno(),), start_new_session=True)
        child.close()
        connection.setblocking(False)
        channel = Channel(connection, executor.check)
        try:
            channel.write(payload)
            ready = channel.read()
            problem(ready)
            if ready != {'ready': True}:
                raise RunFailure('failed', 'The private protocol transport did not become ready')
            reply = dialogue(channel)
            channel.write({'finish': True})
        except BaseException as error:
            caught = error
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        deadline = time.monotonic() + supervisor.CLEANUP_SECONDS + 1
        def cleanup_check():
            if time.monotonic() >= deadline:
                raise RunFailure('failed', 'The private protocol cleanup was not confirmed', confirmed=False)
        channel.check = cleanup_check
        try:
            outcome = channel.final
            while outcome is None:
                value = channel.read()
                if 'event' not in value:
                    outcome = value
        except (OSError, RunFailure, ValueError):
            if isinstance(caught, RunFailure):
                caught.confirmed = False
            if caught is not None:
                raise caught
            raise
        if caught is not None:
            if isinstance(caught, RunFailure):
                caught.confirmed = 'outcome' in outcome or outcome.get('failure', {}).get('confirmed') is True
            raise caught
        problem(outcome)
        return reply
    finally:
        connection.close()
        child.close()
        if process is not None:
            try:
                process.wait(timeout=supervisor.CLEANUP_SECONDS + 1)
            except subprocess.TimeoutExpired:
                pass  # The supervisor retains cleanup authority and its journal.


def relay(connection, child, payload):
    """Supervisor side: forward objects, with no adapter or model-specific decisions."""
    incoming, outgoing, native, stdin = bytearray(), bytearray(encode({'ready': True})), bytearray(), bytearray()
    # A partial frame must finish before the final outcome, including on cancellation.
    # The supervisor flushes this same buffer after stopping the private producers.
    payload['_rpc_pending'] = outgoing
    totals = {'stdout': 0, 'stderr': 0, 'input': 0}
    for stream in (child.stdin, child.stdout, child.stderr):
        os.set_blocking(stream.fileno(), False)
    streams = {child.stdout.fileno(): 'stdout', child.stderr.fileno(): 'stderr'}
    while True:
        if time.monotonic() >= payload['deadline']:
            raise supervisor.Stopped('timeout', 'The run deadline passed')
        supervisor.scopes(payload['scopes'])
        readable, writable, _ = select.select([connection, *streams],
                    ([connection] if outgoing else []) + ([child.stdin] if stdin else []), [], supervisor.POLL)
        if connection in readable:
            chunk = connection.recv(65536)
            if not chunk:
                raise supervisor.Stopped('cancelled', 'The calling process ended')
            incoming.extend(chunk)
            while len(incoming) >= 8:
                size = struct.unpack('!Q', incoming[:8])[0]
                if size > supervisor.WIRE_BYTES:
                    raise supervisor.Stopped('invalid_request', 'A private input frame exceeds its bound')
                if len(incoming) < size + 8:
                    break
                value = json.loads(incoming[8:8 + size])
                del incoming[:8 + size]
                if value == {'finish': True}:
                    return
                if set(value) != {'input'} or not isinstance(value['input'], dict):
                    raise supervisor.Stopped('invalid_request', 'Invalid private protocol control frame')
                line = json.dumps(value['input'], ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
                totals['input'] += len(line)
                if totals['input'] > supervisor.WIRE_BYTES:
                    raise supervisor.Stopped('invalid_request', 'Private native input exceeds its run bound')
                stdin.extend(line)
        if connection in writable and outgoing:
            del outgoing[:connection.send(outgoing[:65536])]
        if child.stdin in writable and stdin:
            del stdin[:os.write(child.stdin.fileno(), stdin[:65536])]
        for descriptor in list(streams):
            if descriptor not in readable:
                continue
            name = streams[descriptor]
            chunk = os.read(descriptor, 65536)
            if not chunk:
                del streams[descriptor]
                continue
            totals[name] += len(chunk)
            limit = supervisor.WIRE_BYTES if name == 'stdout' else supervisor.ERROR_BYTES
            if totals[name] > limit:
                raise supervisor.Stopped('failed', f'Native {name} exceeded the private protocol bound')
            if name == 'stderr':
                continue  # Content is not needed for protocol decisions and is never logged.
            native.extend(chunk)
            while b'\n' in native:
                line, _, rest = native.partition(b'\n')
                native = bytearray(rest)
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise supervisor.Stopped('unavailable', 'Native protocol output is not an object')
                outgoing.extend(encode({'event': frame}))
                if len(outgoing) > supervisor.WIRE_BYTES:
                    raise supervisor.Stopped('failed', 'Native protocol output exceeds its pending bound')
        if supervisor._exited(child) and not streams and not outgoing:
            return
