"""What an image block costs Claude, from its pixel size.

Ported unchanged from Multiverse v1 (vendor/compaction/tokenize.py): Anthropic
bills an image at about width x height / 750 tokens after scaling its longest
side down to 1568 pixels. The size is read from the base64 data's PNG, JPEG or
WebP header; an image whose size cannot be read counts as a typical screenshot.
"""
import base64
import binascii
import struct

DIVISOR = 750
MAX_SIDE = 1568
FALLBACK = 1600


def tokens(block) -> int:
    """The tokens one `{"type": "image", "source": ...}` block costs."""
    if not isinstance(block, dict) or not isinstance(block.get('source'), dict):
        return 0
    data = block['source'].get('data')
    if isinstance(data, str) and data:
        size = dimensions(data)
        if size is not None:
            width, height = size
            if width <= 0 or height <= 0:
                return FALLBACK
            longest = max(width, height)
            if longest > MAX_SIDE:
                scale = MAX_SIDE / longest
                width, height = int(round(width * scale)), int(round(height * scale))
            return max(1, (width * height) // DIVISOR)
    return FALLBACK


def _prefix(data: str, characters: int) -> bytes:
    prefix = data[:max(4, characters)]
    prefix = prefix[:len(prefix) - len(prefix) % 4]
    if not prefix:
        return b''
    try:
        return base64.b64decode(prefix, validate=False)
    except (ValueError, binascii.Error):
        return b''


def dimensions(data: str) -> tuple[int, int] | None:
    """(width, height) from a base64 image's header: PNG, JPEG or WebP, else None."""
    header = _prefix(data, 6000)
    if len(header) < 24:
        return None
    if header[:8] == b'\x89PNG\r\n\x1a\n':
        try:
            return struct.unpack('>II', header[16:24])
        except struct.error:
            return None
    if header[:2] == b'\xff\xd8':
        return _jpeg(header)
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return _webp(header)
    return None


JPEG_FRAMES = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
JPEG_STANDALONE = frozenset({0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0x01})


def _jpeg(buffer: bytes) -> tuple[int, int] | None:
    index, size = 2, len(buffer)
    while index < size - 8:
        if buffer[index] != 0xFF:
            index += 1
            continue
        while index < size and buffer[index] == 0xFF:
            index += 1
        if index >= size:
            return None
        marker = buffer[index]
        index += 1
        if marker in JPEG_FRAMES:
            if index + 7 > size:
                return None
            height, width = struct.unpack('>HH', buffer[index + 3:index + 7])
            return width, height
        if marker in JPEG_STANDALONE:
            continue
        if index + 2 > size:
            return None
        length = struct.unpack('>H', buffer[index:index + 2])[0]
        if length < 2:
            return None
        index += length
    return None


def _webp(buffer: bytes) -> tuple[int, int] | None:
    if len(buffer) < 30:
        return None
    chunk = buffer[12:16]
    if chunk == b'VP8X':
        width = buffer[24] | buffer[25] << 8 | buffer[26] << 16
        height = buffer[27] | buffer[28] << 8 | buffer[29] << 16
        return width + 1, height + 1
    if chunk == b'VP8 ':
        return struct.unpack('<H', buffer[26:28])[0] & 0x3FFF, struct.unpack('<H', buffer[28:30])[0] & 0x3FFF
    if chunk == b'VP8L':
        b0, b1, b2, b3 = buffer[21], buffer[22], buffer[23], buffer[24]
        return ((b1 & 0x3F) << 8 | b0) + 1, ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
    return None
