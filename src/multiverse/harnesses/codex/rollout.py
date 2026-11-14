"""Read native plain/compressed rollouts without creating decompressed siblings."""
import io
from importlib import import_module
import os
from pathlib import Path
import stat
import sys
from threading import Lock

CHUNK = 64 << 10
MAX_LINE = 64 << 20
MAX_READ = 64 << 20
MAX_BLOCKS = 200000
MAX_BYTES = 512 << 20
MAX_INPUT = 512 << 20
MAX_ROWS = 200000
WINDOW_LOG = 26
IDLE_DECODERS = 4
_decompressors = []
_decoder_lock = Lock()


def _after_fork():
    global _decoder_lock
    _decoder_lock = Lock()  # A child must not inherit another thread's held pool lock.


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


class _PooledDecoder(io.BufferedReader):
    def __init__(self, source, codec):
        self._decompressor = None
        with _decoder_lock:
            context = _decompressors.pop() if _decompressors else None
        if context is None:
            context = codec.ZstdDecompressor(max_window_size=1 << WINDOW_LOG)
        stream = None
        try:
            stream = context.stream_reader(source, read_size=CHUNK, read_across_frames=True, closefd=False)
            super().__init__(stream, buffer_size=CHUNK)
        except BaseException:
            if stream is not None:
                stream.close()
            raise  # An incompletely constructed stream cannot safely return its context.
        self._decompressor = context

    def close(self):
        context, self._decompressor = self._decompressor, None
        if context is None:
            return
        super().close()  # On failure discard the context; it may still have an active reader.
        with _decoder_lock:
            if len(_decompressors) < IDLE_DECODERS:
                _decompressors.append(context)


class RolloutError(OSError):
    pass


class CompressionUnavailable(RolloutError):
    pass


def preferred(path):
    """An existing plain sibling wins, including one whose type/read must refuse."""
    path = Path(path)
    if path.suffix == '.zst':
        plain = path.with_suffix('')
        try:
            plain.lstat()
        except FileNotFoundError:
            pass
        else:
            return plain
    return path


def decoder():
    try:
        if sys.version_info >= (3, 14):
            zstd = import_module('compression.zstd')
            return (lambda source: zstd.ZstdFile(source, 'rb', options={
                zstd.DecompressionParameter.window_log_max: WINDOW_LOG})), (zstd.ZstdError, EOFError)
        zstandard = import_module('zstandard')
        return (lambda source: _PooledDecoder(source, zstandard)), (zstandard.ZstdError, EOFError)
    except ImportError as error:
        advice = ('Use a Python build with compression.zstd support' if sys.version_info >= (3, 14)
                  else 'Run uv sync --locked in the mv2 checkout, or reinstall mv2 to install zstandard')
        raise CompressionUnavailable('Compressed Codex rollouts cannot be read: Zstandard support is missing. ' + advice) from error


def frames(handle, check):
    """Check complete Zstandard containers; the codec validates their encoded data.

    A streaming python-zstandard reader does not itself reject premature EOF.
    Walking headers/block lengths proves completeness without buffering bodies
    or requiring the optional content-size/checksum fields (RFC 8878, section 3).
    """
    size, blocks, count = os.fstat(handle.fileno()).st_size, 0, 0
    if size > MAX_INPUT:
        raise RolloutError('The compressed Codex rollout exceeds its 512 MiB input limit')
    def exact(length):
        check()
        data = handle.read(length)
        if len(data) != length:
            raise RolloutError('The compressed Codex rollout is truncated')
        return data
    def skip(length):
        if handle.tell() + length > size:
            raise RolloutError('The compressed Codex rollout is truncated')
        handle.seek(length, os.SEEK_CUR)
    while handle.tell() < size:
        magic = int.from_bytes(exact(4), 'little')
        if 0x184D2A50 <= magic <= 0x184D2A5F:
            skip(int.from_bytes(exact(4), 'little'))
        elif magic == 0xFD2FB528:
            descriptor = exact(1)[0]
            if descriptor & 8:
                raise RolloutError('The compressed Codex rollout has a reserved frame flag')
            single = bool(descriptor & 32)
            window = 0
            if not single:
                value = exact(1)[0]
                base = 1 << (10 + (value >> 3))
                window = base + (base >> 3) * (value & 7)
            skip((0, 1, 2, 4)[descriptor & 3])
            content_bytes = ((1 if single else 0), 2, 4, 8)[descriptor >> 6]
            content_size = int.from_bytes(exact(content_bytes), 'little') + (256 if content_bytes == 2 else 0)
            if (content_size if single else window) > 1 << WINDOW_LOG:
                raise RolloutError('The compressed Codex rollout exceeds its 64 MiB decoder window limit')
            while True:
                blocks += 1
                if blocks > MAX_BLOCKS:
                    raise RolloutError('The compressed Codex rollout exceeds its frame/block scan budget')
                header = int.from_bytes(exact(3), 'little')
                kind, length = (header >> 1) & 3, header >> 3
                if kind == 3 or length > 128 << 10:
                    raise RolloutError('The compressed Codex rollout has an invalid block header')
                skip(1 if kind == 1 else length)
                if header & 1:
                    break
            if descriptor & 4:
                skip(4)
        else:
            raise RolloutError('The compressed Codex rollout has an invalid frame header')
        count += 1
        if count > MAX_BLOCKS:
            raise RolloutError('The compressed Codex rollout exceeds its frame scan budget')
    if not count:
        raise RolloutError('The compressed Codex rollout is empty')
    handle.seek(0)


class Source(io.RawIOBase):
    def __init__(self, raw, check):
        self.raw, self.check, self.encoded = raw, check, 0
    def readable(self):
        return True
    def seekable(self):
        return True
    def readinto(self, buffer):
        self.check()
        data = self.raw.read(min(len(buffer), CHUNK))
        self.encoded += len(data)
        if self.encoded > MAX_INPUT:
            raise RolloutError('The compressed Codex rollout exceeds its 512 MiB input budget')
        buffer[:len(data)] = data
        return len(data)
    def seek(self, offset, whence=0):
        return self.raw.seek(offset, whence)
    def tell(self):
        return self.raw.tell()


class Compressed:
    compressed = True
    def __init__(self, raw, path, check=None):
        self.raw, self.path, self.check = raw, Path(path), check or (lambda: None)
        self.position, self.decoded = 0, 0
        self.before = os.fstat(raw.fileno())
        self.make_stream, self.errors = decoder()
        self.source = Source(raw, lambda: self.check())
        self.stream = None
    def set_check(self, check):
        self.check = check
    def fileno(self):
        return self.raw.fileno()
    def __enter__(self):
        return self
    def __exit__(self, kind, *_):
        try:
            if kind is None:
                while self.read(CHUNK):
                    pass  # Even header-only proof must reject damaged later compressed blocks.
                if preferred(self.path) != self.path:
                    raise RolloutError('A plain Codex rollout appeared while reading its compressed sibling; retry')
            after = os.fstat(self.fileno())
            if kind is None and any(getattr(self.before, field) != getattr(after, field)
                                    for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
                raise RolloutError('The compressed Codex rollout changed while being read')
        finally:
            try:
                stream, self.stream = self.stream, None
                if stream is not None:
                    stream.close()
            finally:
                self.raw.close()
    def _read(self, method, size):
        self.check()
        if self.stream is None:
            frames(self.raw, self.check)
            self.stream = self.make_stream(self.source)
        try:
            data = getattr(self.stream, method)(size)
        except self.errors as error:
            raise RolloutError('The compressed Codex rollout cannot be decoded') from error
        self.decoded += len(data)
        if self.decoded > MAX_BYTES:
            raise RolloutError('The compressed Codex rollout exceeds its 512 MiB read budget')
        self.position += len(data)
        return data
    def read(self, size):
        if not 0 <= size <= MAX_READ + 1:
            raise RolloutError('A compressed Codex rollout read exceeds its byte limit')
        return self._read('read', size)
    def readline(self, size=-1):
        limit = min(size, MAX_LINE + 1) if size >= 0 else MAX_LINE + 1
        data = self._read('readline', limit)
        if len(data) > MAX_LINE:
            raise RolloutError('A compressed Codex rollout row exceeds 64 MiB')
        return data
    def __iter__(self):
        return self
    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line
    def seek(self, offset):
        if offset < 0:
            raise RolloutError('A compressed Codex rollout offset is invalid')
        if offset < self.position:
            self.stream.close()
            self.raw.seek(0)
            self.stream = None
            self.position = 0
        while self.position < offset:
            if not self.read(min(CHUNK, offset - self.position)):
                raise RolloutError('The compressed Codex rollout ended before the requested row')
        return self.position


def open_rollout(path, *, check=None):
    if check is not None:
        check()
    path = preferred(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    raw = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RolloutError('The Codex rollout is not a regular file')
        raw = os.fdopen(fd, 'rb')
        return Compressed(raw, path, check) if path.suffix == '.zst' else raw
    except BaseException:
        raw.close() if raw is not None else os.close(fd)
        raise
